/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The control plane's refusals that come from the license or from a tenant's
 * quota, in words an administrator can act on.
 *
 * An Enterprise control plane answers 403 with an object `detail` in four
 * cases (`error` is the code):
 *
 *   feature_not_licensed            the license does not include `feature`
 *   feature_not_enabled_for_tenant  the tenant has `feature` switched off
 *   licence_limit_reached           the license allows `maximum` of `limit`; there are `current`
 *   licence_not_valid               the installation has no license in force
 *
 * and both editions answer 429 when a tenant's own quota is full, with the
 * same fields: `error: tenant_quota_reached`, `limit` (the quota's name, which
 * is the name the license gives the same limit), `maximum` and `current`. A
 * path that creates on behalf of someone else (provisioning, an import) can
 * refuse with either shape under its own status, so neither is tied to one.
 *
 * The API client (`gatewayApi/base.ts`) is the one place an error answer
 * becomes text: it asks `licenceRefusalOfBody` and `quotaRefusalText` there,
 * so every page that shows the error's message shows these sentences. A page
 * that wants the facts and not the sentence reads `licenceRefusal(error)`.
 *
 * The codes and field names are the API's and are spelled as it spells them;
 * the text shown says "license", as the rest of the console does.
 */

import { getNumber, getRecord, getString, isRecord } from './narrow';

export const LICENCE_REFUSAL_CODES = [
    'feature_not_licensed',
    'feature_not_enabled_for_tenant',
    'licence_limit_reached',
    'licence_not_valid',
] as const;

export type LicenceRefusalCode = (typeof LICENCE_REFUSAL_CODES)[number];

export interface LicenceRefusal {
    code: LicenceRefusalCode;
    /** The feature flag the refusal names (`policies`, `knowledge_base`, ...). */
    feature?: string;
    /** The limit the refusal names (`max_llm_models`, `max_tenants`, ...). */
    limit?: string;
    maximum?: number;
    current?: number;
}

/** The feature flags of a license, as the console names them. */
export const LICENCE_FEATURE_NAMES: Readonly<Record<string, string>> = {
    mcp_servers: 'MCP Servers',
    mcp_server_deployment: 'MCP Deployment',
    mcp_registry: 'MCP Registry',
    virtual_mcp_servers: 'Virtual MCP Servers',
    models: 'Providers & Models',
    model_routing: 'Model Routing',
    ai_model_deployment: 'Model Deployment',
    model_traffic_import: 'Model Traffic Import',
    agent_skills: 'Skills',
    knowledge_base: 'Knowledgebase (RAG)',
    a2a: 'Agents (Cards & Identities)',
    guardrails: 'Guardrails',
    policies: 'Policy as Code',
    compliance: 'Compliance',
};

/** The count limits of a license: what each one counts, in the plural. */
// `max_tenants` (the one limit of the installation, which only a console of
// more than one tenant can meet) is not listed: its noun is its key spelled
// out, which `licenceLimitNoun` gives for any limit without an entry.
export const LICENCE_LIMIT_NOUNS: Readonly<Record<string, string>> = {
    max_admin_users: 'administrators',
    max_end_users: 'end users',
    max_end_user_groups: 'end-user groups',
    max_llm_models: 'enabled models',
    max_mcp_servers: 'enabled MCP servers',
    max_mcp_registries: 'MCP registries',
    max_kb_collections: 'Knowledge Base collections',
    max_agent_skills: 'skills',
    max_agent_cards: 'agent cards',
    max_agent_identities: 'agent identities',
};

/** A name for a flag or limit the console has no name for: the key, with spaces. */
const spelledOut = (key: string): string => key.replace(/^max_/, '').replace(/[_-]/g, ' ');

export const licenceFeatureName = (feature: string): string => LICENCE_FEATURE_NAMES[feature] ?? spelledOut(feature);
export const licenceLimitNoun = (limit: string): string => LICENCE_LIMIT_NOUNS[limit] ?? spelledOut(limit);

const isCode = (value: string | undefined): value is LicenceRefusalCode =>
    value !== undefined && (LICENCE_REFUSAL_CODES as readonly string[]).includes(value);

/** The license refusal in a response body (`{detail: {error, ...}}`); null when it is none. */
export function licenceRefusalOfBody(body: unknown): LicenceRefusal | null {
    const detail = getRecord(body, 'detail');
    const code = getString(detail, 'error');
    if (!isCode(code)) return null;
    const refusal: LicenceRefusal = { code };
    const feature = getString(detail, 'feature');
    const limit = getString(detail, 'limit');
    const maximum = getNumber(detail, 'maximum');
    const current = getNumber(detail, 'current');
    if (feature) refusal.feature = feature;
    if (limit) refusal.limit = limit;
    if (maximum !== undefined) refusal.maximum = maximum;
    if (current !== undefined) refusal.current = current;
    return refusal;
}

/** The license refusal a caught value carries: on the API client's error (`details`) or a body. */
export function licenceRefusal(value: unknown): LicenceRefusal | null {
    if (!isRecord(value)) return null;
    return licenceRefusalOfBody(getRecord(value, 'details')) ?? licenceRefusalOfBody(value);
}

/**
 * The sentence for a license refusal. `featureName` is the name to call the
 * feature by where the caller has one (a screen names itself); without it
 * the feature is named by its flag.
 */
export function licenceRefusalText(refusal: LicenceRefusal, featureName?: string): string {
    const feature = featureName ?? (refusal.feature ? licenceFeatureName(refusal.feature) : 'This feature');
    switch (refusal.code) {
        case 'feature_not_licensed':
            return `${feature} is not in the license of this installation. Contact Brutor AI to add it to your license.`;
        case 'feature_not_enabled_for_tenant':
            return `${feature} is not enabled for this tenant. A system administrator can enable it in the tenant's features.`;
        case 'licence_not_valid':
            return 'This installation has no valid license: it has expired or was not accepted. Contact Brutor AI to renew your license.';
        case 'licence_limit_reached': {
            const noun = refusal.limit ? licenceLimitNoun(refusal.limit) : 'of these';
            const scope = refusal.limit === 'max_tenants' ? 'in this installation' : 'per tenant';
            const allowed =
                refusal.maximum === undefined
                    ? `The license limit for ${noun} ${scope} is reached`
                    : `The license allows ${refusal.maximum} ${noun} ${scope}`;
            const have = refusal.current === undefined ? '' : `, and there are ${refusal.current}`;
            return `${allowed}${have}. Nothing was changed. Remove or disable one, or contact Brutor AI to raise the limit of your license.`;
        }
    }
}

/** The `error` of the answer to a tenant at its own quota. */
export const TENANT_QUOTA_REACHED = 'tenant_quota_reached';

/**
 * The sentence for a tenant quota that is full (`detail.error` is
 * `tenant_quota_reached`); null for any other answer. The facts are read
 * from the fields; the server's sentence is not taken apart.
 */
export function quotaRefusalText(body: unknown): string | null {
    const detail = getRecord(body, 'detail');
    if (getString(detail, 'error') !== TENANT_QUOTA_REACHED) return null;
    const limit = getString(detail, 'limit');
    const maximum = getNumber(detail, 'maximum');
    const current = getNumber(detail, 'current');
    const what =
        limit === undefined || maximum === undefined
            ? "This tenant's quota is reached"
            : `This tenant's quota allows ${maximum} ${licenceLimitNoun(limit)}${current === undefined ? '' : `, and there are ${current}`}`;
    return `${what}. Nothing was changed. Remove or disable one, or ask a system administrator to raise the tenant's quota.`;
}

// --- Telling the screen ----------------------------------------------------

type Listener = (refusal: LicenceRefusal) => void;
const listeners = new Set<Listener>();

/**
 * Be told of every license refusal the API client receives. A screen of a
 * licensed feature listens, so that it can say the feature is not in the
 * license (or not enabled for the tenant) whichever of its requests was
 * refused. Returns the function that stops listening.
 */
export function onLicenceRefusal(listener: Listener): () => void {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

/** Called by the API client with each license refusal it receives. */
export function reportLicenceRefusal(refusal: LicenceRefusal): void {
    for (const listener of [...listeners]) listener(refusal);
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The identity policy: what credential a caller of a resource group must
 * present, and what the console reads of it.
 *
 * A group's effective policy is resolved from the development default, the
 * tenant default, every explicit policy down the group's hierarchy, the
 * production floor of an AI System in a production stage, and the high-risk
 * floor, field by field, the stricter value winning. The Control Plane
 * resolves it; the console shows the result, where each field comes from,
 * and the floor a group's own policy may not go below.
 *
 * Every answer of the identity routes is read here, through `narrow.ts`, and
 * nowhere else. The shapes below are the generated Control Plane types; where
 * the Control Plane answers a member as a free dictionary (a resolved policy,
 * a source) or as a free string (a credential's kind and level), the shape
 * reads it as the policy or the vocabulary it is. An answer that does not
 * have them is refused (an error the screen shows), never read as a weaker
 * policy.
 */

import type { Answered, ApiResponse } from './api/controlPlaneTypes';
import type { components } from './generated/control-plane-api';
import { getArray, getBoolean, getNumber, getRecord, getString, isRecord } from './narrow';

type Schemas = components['schemas'];

// ---------------------------------------------------------------------------
// Permissions
// ---------------------------------------------------------------------------

/** Read a group's identity policy and an AI System's credentials. */
export const IDENTITY_POLICY_READ = 'identity_policy:read';
/** Write a group's identity policy and the tenant identity default. */
export const IDENTITY_POLICY_MANAGE = 'identity_policy:manage';

// ---------------------------------------------------------------------------
// Vocabulary
// ---------------------------------------------------------------------------

/** The credential levels, weakest first. */
export const ASSURANCE_LEVELS = ['key', 'bound_key', 'workload', 'workload_pop'] as const satisfies readonly Schemas['IdentityPolicyModel']['min_assurance'][];
export type AssuranceLevel = (typeof ASSURANCE_LEVELS)[number];

export const POLICY_MODES = ['audit', 'enforce'] as const satisfies readonly Schemas['IdentityPolicyModel']['mode'][];
export type PolicyMode = (typeof POLICY_MODES)[number];

export const CREDENTIAL_KINDS = [
    'api_key',
    'workload_token',
    'x509_svid',
    'brutor_token',
    'human_session',
    'platform_service',
    'anonymous',
] as const;
export type CredentialKind = (typeof CREDENTIAL_KINDS)[number];

export const SOURCE_KINDS = ['default', 'tenant_default', 'explicit', 'lifecycle', 'risk_floor', 'intersection'] as const;
export type PolicySourceKind = (typeof SOURCE_KINDS)[number];

/** The fields of a policy, in the order the Control Plane names them. */
export const POLICY_FIELDS = [
    'mode',
    'min_assurance',
    'allow_user_keys',
    'allowed_issuers',
    'max_token_lifetime_s',
    'max_key_ttl_days',
] as const satisfies readonly (keyof Schemas['IdentityPolicyModel'])[];
export type PolicyField = (typeof POLICY_FIELDS)[number];

export const LEVEL_LABELS: Record<AssuranceLevel, string> = {
    key: 'Key',
    bound_key: 'Bound key',
    workload: 'Workload',
    workload_pop: 'Workload + proof of possession',
};

export const KIND_LABELS: Record<CredentialKind, string> = {
    api_key: 'API key',
    workload_token: 'Workload token',
    x509_svid: 'X.509 SVID',
    brutor_token: 'Brutor token',
    human_session: 'Person (session)',
    platform_service: 'Platform service',
    anonymous: 'Anonymous',
};

export const FIELD_LABELS: Record<PolicyField, string> = {
    mode: 'Mode',
    min_assurance: 'Minimum credential level',
    allow_user_keys: 'Personal API keys',
    allowed_issuers: 'Identity providers',
    max_token_lifetime_s: 'Longest workload token',
    max_key_ttl_days: 'Longest API key lifetime',
};

// ---------------------------------------------------------------------------
// Choosing how agents authenticate
// ---------------------------------------------------------------------------

/** The docs guide that compares the ways an agent can authenticate. */
export const CHOOSING_IDENTITY_PATH = '/build-an-agent/identity/choosing/';
export const CHOOSING_IDENTITY_URL = `https://docs.brutor.ai${CHOOSING_IDENTITY_PATH}`;

/** One option an administrator chooses between: what it proves, and what a copy of the credential can do. */
export interface TradeOff {
    option: string;
    label: string;
    proves: string;
    copied: string;
}

/** The credential levels, weakest first. */
export const LEVEL_TRADE_OFFS: readonly TradeOff[] = [
    { option: 'key', label: 'Key', proves: 'Only that the caller holds the key.', copied: 'Works from anywhere until it is revoked.' },
    {
        option: 'bound_key',
        label: 'Bound key',
        proves: 'The call came from an allowed network with an unexpired key, not which workload made it.',
        copied: 'Works from inside the allowed networks until the key expires.',
    },
    {
        option: 'workload',
        label: 'Workload',
        proves: 'An identity provider you trust vouches for this agent\'s workload.',
        copied: 'Works until the token expires (minutes).',
    },
    {
        option: 'workload_pop',
        label: 'Workload + proof of possession',
        proves: 'The caller holds the workload\'s private key.',
        copied: 'Useless without the private key.',
    },
];

/**
 * The levels a policy is offered: those a credential can reach. No
 * credential reaches `bound_key` yet (an API key bound to networks is not
 * available), so a policy at it would refuse every API key while reading as
 * though one could meet it. A stored policy that holds it still shows it.
 */
export const OFFERED_LEVELS: readonly AssuranceLevel[] = ASSURANCE_LEVELS.filter((level) => level !== 'bound_key');

/** The levels to offer beside a policy whose level is `current`: the offered ones, and `current` itself. */
export function levelsToOffer(current?: string): AssuranceLevel[] {
    return ASSURANCE_LEVELS.filter((level) => OFFERED_LEVELS.includes(level) || level === current);
}

/** An API key against workload identity, in the API key dialog. */
export const KEY_TRADE_OFFS: readonly TradeOff[] = [
    { ...LEVEL_TRADE_OFFS[0], label: 'API key (level key)' },
    { ...LEVEL_TRADE_OFFS[2], label: 'Workload identity, instead of a key (level workload)' },
];

export const KEY_PRODUCTION_NOTE = 'An AI System in production requires workload identity: the gateway refuses API keys for it.';

/** What applies where nothing else contributes: audit, level key. */
export const DEVELOPMENT_DEFAULT: IdentityPolicy = {
    mode: 'audit',
    min_assurance: 'key',
    allow_user_keys: true,
    allowed_issuers: null,
    max_token_lifetime_s: 3600,
    max_key_ttl_days: null,
};

/** The personal-key ceiling, in days, when no tenant default sets one. */
export const DEFAULT_USER_KEY_TTL_DAYS = 90;

/** The token lifetime a policy may require, in seconds, and the key lifetime ceiling, in days. */
export const TOKEN_LIFETIME_RANGE = { min: 60, max: 3600 } as const;
export const KEY_TTL_RANGE = { min: 1, max: 3650 } as const;

export function levelRank(level: AssuranceLevel): number {
    return ASSURANCE_LEVELS.indexOf(level);
}

const isLevel = (value: unknown): value is AssuranceLevel => (ASSURANCE_LEVELS as readonly unknown[]).includes(value);
const isMode = (value: unknown): value is PolicyMode => (POLICY_MODES as readonly unknown[]).includes(value);
const isKind = (value: unknown): value is CredentialKind => (CREDENTIAL_KINDS as readonly unknown[]).includes(value);
const isSourceKind = (value: unknown): value is PolicySourceKind => (SOURCE_KINDS as readonly unknown[]).includes(value);

// ---------------------------------------------------------------------------
// Shapes
// ---------------------------------------------------------------------------

/** A complete policy, stored on a group or resolved: the Control Plane's model, all six members and no other. */
export type IdentityPolicy = Schemas['IdentityPolicyModel'];

/** The tenant identity default: a policy and the personal-key ceiling. */
export type IdentityPolicyDefault = Schemas['IdentityPolicyDefaultModel'];

/**
 * Where a field of the effective policy comes from. The Control Plane
 * answers it as a free dictionary; these are the members it writes.
 */
export interface PolicySource {
    kind: PolicySourceKind;
    group_id?: string;
    group_name?: string;
    stage?: string;
    risk_tier?: string;
    lowered_by_exception?: boolean;
}

type PolicyAnswer = ApiResponse<'/v1/admin/resource-groups/{group_id}/identity-policy', 'get'>;

/**
 * `GET` and `PUT /v1/admin/resource-groups/{group_id}/identity-policy`: the
 * Control Plane's answer, with the members it answers as free dictionaries
 * read as policies and sources.
 */
export type IdentityPolicyResponse = Omit<PolicyAnswer, 'policy' | 'effective' | 'inherited_floor' | 'sources'> & {
    /** The group's own stored policy. */
    policy: IdentityPolicy | null;
    effective: IdentityPolicy;
    /** What the group inherits: its own policy may not be looser. */
    inherited_floor: IdentityPolicy;
    sources: Record<PolicyField, PolicySource>;
};

type CredentialsAnswer = ApiResponse<'/v1/admin/ai-systems/{ai_system_id}/identity/credentials', 'get'>;

/**
 * One credential an AI System has seen (`GET …/identity/credentials`): the
 * Control Plane's item, with its kind and level read as the vocabulary's.
 * `credential_ref` is null for a credential that has no reference.
 */
export type IdentityCredential = Omit<CredentialsAnswer['items'][number], 'credential_kind' | 'credential_assurance'> & {
    credential_kind: CredentialKind;
    /** null for kinds outside the ladder (a person's session, the platform service, anonymous). */
    credential_assurance: AssuranceLevel | null;
};

export type IdentityCredentialList = Omit<CredentialsAnswer, 'items'> & { items: IdentityCredential[] };

/** A credential that keeps the lifecycle gate's workload identity requirement unmet: the Control Plane's, with its level read. */
export type OffendingCredential = Omit<Answered<Schemas['OffendingCredential']>, 'credential_assurance'> & {
    credential_assurance: AssuranceLevel | null;
};

// ---------------------------------------------------------------------------
// Reading the answers
// ---------------------------------------------------------------------------

const optionalString = (value: unknown, key: string): string | null | undefined => {
    if (!isRecord(value) || value[key] === null || value[key] === undefined) return null;
    return getString(value, key);
};

/** A complete policy, or null when `value` is not one. */
export function parseIdentityPolicy(value: unknown): IdentityPolicy | null {
    if (!isRecord(value)) return null;
    const mode = value.mode;
    const level = value.min_assurance;
    const allowUserKeys = getBoolean(value, 'allow_user_keys');
    const lifetime = getNumber(value, 'max_token_lifetime_s');
    const issuers = value.allowed_issuers;
    const ttl = value.max_key_ttl_days;
    if (!isMode(mode) || !isLevel(level) || allowUserKeys === undefined || lifetime === undefined) return null;
    if (issuers !== null && !(Array.isArray(issuers) && issuers.every((issuer) => typeof issuer === 'string'))) return null;
    if (ttl !== null && getNumber(value, 'max_key_ttl_days') === undefined) return null;
    return {
        mode,
        min_assurance: level,
        allow_user_keys: allowUserKeys,
        allowed_issuers: Array.isArray(issuers) ? issuers.filter((issuer): issuer is string => typeof issuer === 'string') : null,
        max_token_lifetime_s: lifetime,
        max_key_ttl_days: ttl === null ? null : (ttl as number),
    };
}

function parseSource(value: unknown): PolicySource | null {
    if (!isRecord(value) || !isSourceKind(value.kind)) return null;
    const source: PolicySource = { kind: value.kind };
    for (const key of ['group_id', 'group_name', 'stage', 'risk_tier'] as const) {
        const text = getString(value, key);
        if (text !== undefined) source[key] = text;
    }
    const lowered = getBoolean(value, 'lowered_by_exception');
    if (lowered !== undefined) source.lowered_by_exception = lowered;
    return source;
}

function unreadable(what: string): Error {
    return new Error(`The Control Plane's answer about ${what} could not be read.`);
}

/** The answer of the identity policy route; throws when it does not have its shape. */
export function parseIdentityPolicyResponse(value: unknown): IdentityPolicyResponse {
    const what = 'the identity policy';
    const groupId = getString(value, 'group_id');
    const effective = parseIdentityPolicy(getRecord(value, 'effective'));
    const floor = parseIdentityPolicy(getRecord(value, 'inherited_floor'));
    const stored = isRecord(value) ? value.policy : undefined;
    const policy = stored === null ? null : parseIdentityPolicy(stored);
    const sourcesIn = getRecord(value, 'sources');
    if (!isRecord(value) || groupId === undefined || !effective || !floor || (stored !== null && !policy) || !sourcesIn) throw unreadable(what);
    const sources: Partial<Record<PolicyField, PolicySource>> = {};
    for (const field of POLICY_FIELDS) {
        const source = parseSource(sourcesIn[field]);
        if (!source) throw unreadable(what);
        sources[field] = source;
    }
    return {
        group_id: groupId,
        group_name: getString(value, 'group_name') ?? groupId,
        is_ai_system: getBoolean(value, 'is_ai_system') ?? false,
        lifecycle_stage: getString(value, 'lifecycle_stage') ?? null,
        eu_ai_act_risk_tier: getString(value, 'eu_ai_act_risk_tier') ?? null,
        attributed_to_ai_system: getBoolean(value, 'attributed_to_ai_system') ?? false,
        policy,
        effective,
        inherited_floor: floor,
        sources: sources as Record<PolicyField, PolicySource>,
        max_user_key_ttl_days: getNumber(value, 'max_user_key_ttl_days') ?? 90,
    };
}

function parseCredential(value: unknown): IdentityCredential | null {
    const ref = optionalString(value, 'credential_ref');
    const kind = isRecord(value) ? value.credential_kind : undefined;
    const level = isRecord(value) ? value.credential_assurance : undefined;
    const calls = getNumber(value, 'calls');
    if (ref === undefined || !isKind(kind) || !(level === null || level === undefined || isLevel(level)) || calls === undefined) return null;
    const strings = ['first_seen', 'last_seen', 'api_key_id', 'api_key_name', 'agent_id'] as const;
    const read = Object.fromEntries(strings.map((key) => [key, optionalString(value, key)])) as Record<(typeof strings)[number], string | null | undefined>;
    if (strings.some((key) => read[key] === undefined)) return null;
    return {
        credential_ref: ref,
        credential_kind: kind,
        credential_assurance: level ?? null,
        calls,
        below_policy_calls: getNumber(value, 'below_policy_calls') ?? 0,
        refused_calls: getNumber(value, 'refused_calls') ?? 0,
        first_seen: read.first_seen ?? null,
        last_seen: read.last_seen ?? null,
        api_key_id: read.api_key_id ?? null,
        api_key_name: read.api_key_name ?? null,
        agent_id: read.agent_id ?? null,
    };
}

/** The answer of the credentials route; throws when it does not have its shape. */
export function parseIdentityCredentials(value: unknown): IdentityCredentialList {
    const items = getArray(value, 'items');
    if (!items) throw unreadable('the credentials of this AI System');
    const parsed = items.map(parseCredential);
    if (parsed.some((item) => item === null)) throw unreadable('the credentials of this AI System');
    const list = parsed as IdentityCredential[];
    return { items: list, total: getNumber(value, 'total') ?? list.length };
}

/**
 * The offending credentials a gate requirement carries (the workload
 * identity requirement, when unmet), or null when it carries none. An entry
 * that cannot be read is left out; the total is the Control Plane's.
 */
export function offendingCredentialsOf(requirement: unknown): { items: OffendingCredential[]; total: number } | null {
    const listed = getArray(requirement, 'offending_credentials');
    if (!listed) return null;
    const items = listed.flatMap((entry): OffendingCredential[] => {
        const ref = optionalString(entry, 'credential_ref');
        const kind = getString(entry, 'credential_kind');
        const level = isRecord(entry) ? entry.credential_assurance : undefined;
        const calls = getNumber(entry, 'calls');
        const lastSeen = optionalString(entry, 'last_seen');
        if (ref === undefined || kind === undefined || !(level === null || level === undefined || isLevel(level)) || calls === undefined || lastSeen === undefined) return [];
        return [{ credential_ref: ref, credential_kind: kind, credential_assurance: level ?? null, calls, last_seen: lastSeen }];
    });
    return { items, total: getNumber(requirement, 'offending_total') ?? items.length };
}

/**
 * A credential's identity in a list: the Control Plane counts a credential
 * per reference, kind and level, and a reference may be null.
 */
export function credentialKey(credential: Pick<OffendingCredential, 'credential_ref' | 'credential_kind' | 'credential_assurance'>): string {
    return `${credential.credential_kind}|${credential.credential_assurance ?? ''}|${credential.credential_ref ?? ''}`;
}

/** The tenant identity default of a tenant record, or null when none is set. Throws when it is set but cannot be read. */
export function identityPolicyDefaultOf(tenant: unknown): IdentityPolicyDefault | null {
    if (!isRecord(tenant) || tenant.identity_policy_default === null || tenant.identity_policy_default === undefined) return null;
    const stored = tenant.identity_policy_default;
    const policy = parseIdentityPolicy(stored);
    const ceiling = getNumber(stored, 'max_user_key_ttl_days');
    if (!policy || ceiling === undefined) throw new Error('The tenant identity default could not be read.');
    return { ...policy, max_user_key_ttl_days: ceiling };
}

// ---------------------------------------------------------------------------
// Saying it
// ---------------------------------------------------------------------------

/** Where a field comes from, as a sentence fragment: "inherited from Lending (lifecycle: active)". */
export function sourceText(source: PolicySource, acting?: { group_id: string }): string {
    let text: string;
    switch (source.kind) {
        case 'default':
            text = 'the development default';
            break;
        case 'tenant_default':
            text = 'the tenant default';
            break;
        case 'intersection':
            text = 'the intersection of the issuer lists of the hierarchy';
            break;
        default: {
            const what =
                source.kind === 'explicit'
                    ? 'explicit policy'
                    : source.kind === 'lifecycle'
                      ? `lifecycle: ${source.stage ?? 'production'}`
                      : `high-risk floor: ${source.risk_tier ?? 'high'}`;
            const who = acting && source.group_id === acting.group_id ? 'this group' : `inherited from ${source.group_name ?? source.group_id ?? 'an ancestor'}`;
            text = `${who} (${what})`;
        }
    }
    return source.lowered_by_exception ? `${text}, lowered by a gate exception` : text;
}

/** A field's value as the console shows it. */
export function fieldValueText(field: PolicyField, value: unknown): string {
    // A value of a shape the field does not take is shown as the JSON it is.
    const raw = (): string => (typeof value === 'string' ? value : JSON.stringify(value) ?? 'nothing');
    if (field === 'min_assurance' && isLevel(value)) return LEVEL_LABELS[value];
    if (field === 'allow_user_keys') return value === true ? 'accepted' : value === false ? 'refused' : raw();
    if (field === 'allowed_issuers') return Array.isArray(value) ? (value.length ? value.map((v) => (typeof v === 'string' ? v : JSON.stringify(v))).join(', ') : 'none') : value === null ? 'any' : raw();
    if (field === 'max_token_lifetime_s') return typeof value === 'number' ? `${value} s` : raw();
    if (field === 'max_key_ttl_days') return value === null ? 'no ceiling' : typeof value === 'number' ? `${value} days` : raw();
    return raw();
}

/**
 * An entry of a contract diff about the AI System's identity policy
 * (`kind: "identity_policy"`), in words: the field, its value before and
 * after. A loosened field is in the diff's `widened` list, a tightened one in
 * `narrowed`; an explicit policy set or removed that moves no field (it
 * equals the default) is in `changed`, with `field: "policy"`. null for any
 * other entry.
 */
export function identityDiffText(entry: { kind: string; field?: string; from?: unknown; to?: unknown }): string | null {
    if (entry.kind !== 'identity_policy') return null;
    const field = POLICY_FIELDS.find((name) => name === entry.field);
    if (field) return `Identity policy · ${FIELD_LABELS[field]}: ${fieldValueText(field, entry.from)} → ${fieldValueText(field, entry.to)}`;
    if (entry.field === 'policy') {
        return `Identity policy: ${entry.to === null || entry.to === undefined ? 'the explicit policy was removed' : 'an explicit policy was set'}, the same as the default; no field moved`;
    }
    return null;
}

// ---------------------------------------------------------------------------
// Checking a policy before it is sent
// ---------------------------------------------------------------------------

/** What the Control Plane would refuse in a policy's values, per field (empty when nothing). */
export function validatePolicy(policy: IdentityPolicy): Partial<Record<PolicyField, string>> {
    const problems: Partial<Record<PolicyField, string>> = {};
    if (policy.allowed_issuers !== null) {
        if (policy.allowed_issuers.length === 0) problems.allowed_issuers = 'Choose at least one identity provider, or accept any.';
        else if (new Set(policy.allowed_issuers).size !== policy.allowed_issuers.length) problems.allowed_issuers = 'An identity provider is listed twice.';
        else if (policy.allowed_issuers.length > 50) problems.allowed_issuers = 'At most 50 identity providers.';
    }
    const lifetime = policy.max_token_lifetime_s;
    if (!Number.isInteger(lifetime) || lifetime < TOKEN_LIFETIME_RANGE.min || lifetime > TOKEN_LIFETIME_RANGE.max) {
        problems.max_token_lifetime_s = `Between ${TOKEN_LIFETIME_RANGE.min} and ${TOKEN_LIFETIME_RANGE.max} seconds.`;
    }
    const ttl = policy.max_key_ttl_days;
    if (ttl !== null && (!Number.isInteger(ttl) || ttl < KEY_TTL_RANGE.min || ttl > KEY_TTL_RANGE.max)) {
        problems.max_key_ttl_days = `Between ${KEY_TTL_RANGE.min} and ${KEY_TTL_RANGE.max} days, or empty for no ceiling.`;
    }
    return problems;
}

/**
 * The fields in which `policy` is looser than `floor`, in policy order: what
 * the Control Plane refuses in a group's own policy.
 */
export function fieldsBelowFloor(policy: IdentityPolicy, floor: IdentityPolicy): PolicyField[] {
    const looser: Record<PolicyField, boolean> = {
        mode: floor.mode === 'enforce' && policy.mode === 'audit',
        min_assurance: levelRank(policy.min_assurance) < levelRank(floor.min_assurance),
        allow_user_keys: policy.allow_user_keys && !floor.allow_user_keys,
        allowed_issuers:
            floor.allowed_issuers !== null &&
            (policy.allowed_issuers === null || policy.allowed_issuers.some((issuer) => !floor.allowed_issuers?.includes(issuer))),
        max_token_lifetime_s: policy.max_token_lifetime_s > floor.max_token_lifetime_s,
        max_key_ttl_days: floor.max_key_ttl_days !== null && (policy.max_key_ttl_days === null || policy.max_key_ttl_days > floor.max_key_ttl_days),
    };
    return POLICY_FIELDS.filter((field) => looser[field]);
}

// ---------------------------------------------------------------------------
// Refusals
// ---------------------------------------------------------------------------

/** One field the floor refused in a policy write. */
export interface FloorProblem {
    field: string;
    requested: unknown;
    floor: unknown;
    source: PolicySource | null;
}

export interface PolicyRefusal {
    code: 'identity_policy_below_floor' | 'identity_policy_unknown_issuer' | 'permission_denied';
    message: string;
    problems: FloorProblem[];
    issuers?: string[];
}

/** The Control Plane's coded refusal (`{detail: {code, message, …}}`) carried by a caught error, or undefined. */
function codedDetail(error: unknown): Record<string, unknown> | undefined {
    const detail = getRecord(getRecord(error, 'details'), 'detail');
    return getString(detail, 'code') === undefined ? undefined : detail;
}

/** The refusal of a policy write (group policy or tenant default), or null when the error is not one. */
export function policyRefusal(error: unknown): PolicyRefusal | null {
    const detail = codedDetail(error);
    const code = getString(detail, 'code');
    if (code !== 'identity_policy_below_floor' && code !== 'identity_policy_unknown_issuer' && code !== 'permission_denied') return null;
    const problems = (getArray(detail, 'problems') ?? []).flatMap((problem): FloorProblem[] => {
        const field = getString(problem, 'field');
        if (field === undefined || !isRecord(problem)) return [];
        return [{ field, requested: problem.requested, floor: problem.floor, source: parseSource(problem.source) }];
    });
    const refusal: PolicyRefusal = { code, message: getString(detail, 'message') ?? code, problems };
    const issuers = getArray(detail, 'issuers');
    if (issuers) refusal.issuers = issuers.filter((issuer): issuer is string => typeof issuer === 'string');
    return refusal;
}

export const KEY_MINT_REFUSALS = ['key_refused_by_identity_policy', 'user_key_not_allowed', 'key_expiry_required', 'key_expiry_exceeds_ceiling'] as const;
export type KeyMintRefusalCode = (typeof KEY_MINT_REFUSALS)[number];

/** A key create or edit refused by the identity policy: its code and the console's sentence, or null when the error is not one. */
export function keyMintRefusal(error: unknown, groupId?: string): { code: KeyMintRefusalCode; text: string } | null {
    const detail = codedDetail(error);
    const code = getString(detail, 'code');
    if (!(KEY_MINT_REFUSALS as readonly (string | undefined)[]).includes(code)) return null;
    const known = code as KeyMintRefusalCode;
    const message = getString(detail, 'message') ?? known;
    const effective = parseIdentityPolicy(getRecord(detail, 'effective_policy'));
    if (!effective) return { code: known, text: message };
    const source = parseSource(getRecord(getRecord(detail, 'sources'), 'min_assurance'));
    const acting = groupId ? { group_id: groupId } : undefined;
    switch (known) {
        case 'key_refused_by_identity_policy':
            return {
                code: known,
                text:
                    `The identity policy of this group requires credential level ${effective.min_assurance}` +
                    (source ? ` (${sourceText(source, acting)})` : '') +
                    '; an API key is level key, so the gateway would refuse it. ' +
                    'Authenticate the agent with workload identity, or develop in a separate AI System.',
            };
        case 'user_key_not_allowed':
            return {
                code: known,
                text: 'The identity policy of this group does not accept personal API keys. Create a shared key, or authenticate the agent with workload identity.',
            };
        case 'key_expiry_required':
            return {
                code: known,
                text:
                    effective.max_key_ttl_days !== null
                        ? `This key needs an expiry date: the identity policy allows keys to live at most ${effective.max_key_ttl_days} days.`
                        : 'This key needs an expiry date: a personal key always has one.',
            };
        case 'key_expiry_exceeds_ceiling':
            return {
                code: known,
                text:
                    (effective.max_key_ttl_days !== null
                        ? `The expiry is later than the identity policy allows (at most ${effective.max_key_ttl_days} days from now). `
                        : 'The expiry is later than the identity policy allows. ') + message,
            };
    }
}

// ---------------------------------------------------------------------------
// An API key under the policy
// ---------------------------------------------------------------------------

export type KeyStanding =
    | { level: 'key'; standing: 'met' }
    | { level: 'key'; standing: 'refused_after_promotion' }
    | { level: 'key'; standing: 'below_policy' | 'refused'; required: AssuranceLevel };

/**
 * What the gateway does with an API key of this group under its effective
 * policy. An API key is level `key`. A key the policy accepts today in a
 * group an AI System governs is still refused once that system is in
 * production: a production AI System requires workload identity.
 */
export function keyStanding(policy: IdentityPolicyResponse, scope: string): KeyStanding {
    const { effective } = policy;
    const belowLevel = levelRank(effective.min_assurance) > levelRank('key');
    const personalRefused = scope === 'user' && !effective.allow_user_keys;
    if (belowLevel || personalRefused) {
        return { level: 'key', standing: effective.mode === 'enforce' ? 'refused' : 'below_policy', required: effective.min_assurance };
    }
    if (policy.attributed_to_ai_system) return { level: 'key', standing: 'refused_after_promotion' };
    return { level: 'key', standing: 'met' };
}

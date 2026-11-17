/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The refused credential of a disabled, expired or revoked agent, as a
 * Proxy Logs row states it.
 *
 * The gateway records the refusal as a row of the surface it happened on
 * (`llm`, `mcp`, `a2a`, `skill`; `oauth` only where the route is of no
 * governed surface), subtype `agent_authz_block`, status 401. One row can
 * stand for many refusals: a replayed credential is recorded at most once a
 * minute. The route, the count and the kind of credential are in the row's
 * `custom_metadata` (`brutor-gateway-core/src/services/refused_agent_credential.rs`).
 *
 * The agent gate's own denials carry the same subtype; what tells a refused
 * credential apart is `refused_credential`. Nothing here reads `proxy_type`.
 */

import { getNumber, getString } from './narrow';

export const AGENT_AUTHZ_BLOCK = 'agent_authz_block';

export interface RefusedCredential {
    /** `api_key` or `workload_token`; a kind the console does not know is kept as it is. */
    credential: string;
    /** The route, with every name in the path as `{}`; null where the row states none. */
    route: string | null;
    /** How many refusals the row stands for; null where the row does not say. */
    refusals: number | null;
}

export function refusedCredentialOf(
    log: { proxy_subtype?: string | null; custom_metadata?: unknown },
): RefusedCredential | null {
    if (log.proxy_subtype !== AGENT_AUTHZ_BLOCK) return null;
    const credential = getString(log.custom_metadata, 'refused_credential');
    if (!credential) return null;
    return {
        credential,
        route: getString(log.custom_metadata, 'refused_route') ?? null,
        refusals: getNumber(log.custom_metadata, 'refusals') ?? null,
    };
}

/** The kind of credential in words: "API key", "workload token". */
export function refusedCredentialKind(credential: string): string {
    if (credential === 'api_key') return 'API key';
    if (credential === 'workload_token') return 'workload token';
    return `credential (${credential})`;
}

function count(refusals: number): string {
    return `${refusals} refusal${refusals === 1 ? '' : 's'}`;
}

/** One sentence or two: which credential, on which route, how many refusals. */
export function refusedCredentialSentence(refused: RefusedCredential): string {
    const where = refused.route ? `, on ${refused.route}` : '';
    const stands = refused.refusals === null ? '' : ` This row stands for ${count(refused.refusals)}.`;
    return `The gateway refused the ${refusedCredentialKind(refused.credential)} of an agent that is disabled, expired or revoked${where}.${stands}`;
}

/** The short mark for the list: "API key refused ×3". */
export function refusedCredentialMark(refused: RefusedCredential): string {
    const times = refused.refusals !== null && refused.refusals > 1 ? ` ×${refused.refusals}` : '';
    return `${refusedCredentialKind(refused.credential)} refused${times}`;
}

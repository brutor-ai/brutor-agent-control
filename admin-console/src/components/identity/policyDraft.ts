/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * An identity policy as a form holds it (numbers as typed), and the tenant's
 * OIDC identity providers a policy may name.
 */

import { getString, isRecord } from '../../lib/narrow';
import type { AssuranceLevel, IdentityPolicy, PolicyMode } from '../../lib/identityPolicy';

export interface PolicyDraft {
    mode: PolicyMode;
    min_assurance: AssuranceLevel;
    allow_user_keys: boolean;
    allowed_issuers: string[] | null;
    max_token_lifetime_s: string;
    max_key_ttl_days: string;
}

export const toDraft = (policy: IdentityPolicy): PolicyDraft => ({
    mode: policy.mode,
    min_assurance: policy.min_assurance,
    allow_user_keys: policy.allow_user_keys,
    allowed_issuers: policy.allowed_issuers,
    max_token_lifetime_s: String(policy.max_token_lifetime_s),
    max_key_ttl_days: policy.max_key_ttl_days === null ? '' : String(policy.max_key_ttl_days),
});

/** A number as typed: blank is not a number (the check refuses it). */
const typed = (text: string): number => (text.trim() === '' ? NaN : Number(text));

export const fromDraft = (draft: PolicyDraft): IdentityPolicy => ({
    mode: draft.mode,
    min_assurance: draft.min_assurance,
    allow_user_keys: draft.allow_user_keys,
    allowed_issuers: draft.allowed_issuers,
    max_token_lifetime_s: typed(draft.max_token_lifetime_s),
    max_key_ttl_days: draft.max_key_ttl_days.trim() === '' ? null : typed(draft.max_key_ttl_days),
});

export interface Issuer {
    id: string;
    name: string;
}

/** The tenant's OIDC identity providers, from the IdP list (an object keyed by id). */
export function oidcIssuers(answer: unknown): Issuer[] {
    const idps = isRecord(answer) && isRecord(answer.idps) ? answer.idps : {};
    return Object.entries(idps).flatMap(([key, value]): Issuer[] => {
        if (getString(value, 'protocol') !== 'oidc') return [];
        const id = getString(value, 'id') ?? key;
        return [{ id, name: getString(value, 'name') ?? id }];
    });
}

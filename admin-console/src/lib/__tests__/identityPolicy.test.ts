/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console's reading of the identity policy answers: the policy, where
 * each field of the effective policy comes from, the credentials an AI
 * System has seen, the offending credentials of the lifecycle gate, and the
 * refusals of a policy write and of a key mint. Everything the Control Plane
 * sends is read through `narrow.ts`; a malformed answer is refused, never
 * shown as a weaker policy.
 */

import { describe, expect, it } from 'vitest';

import { GatewayAPIError } from '../gatewayApi/base';
import {
    IDENTITY_POLICY_MANAGE,
    IDENTITY_POLICY_READ,
    POLICY_FIELDS,
    fieldsBelowFloor,
    identityPolicyDefaultOf,
    keyMintRefusal,
    keyStanding,
    offendingCredentialsOf,
    parseIdentityCredentials,
    parseIdentityPolicy,
    parseIdentityPolicyResponse,
    policyRefusal,
    sourceText,
    validatePolicy,
    type IdentityPolicy,
    type IdentityPolicyResponse,
} from '../identityPolicy';

const DEVELOPMENT: IdentityPolicy = {
    mode: 'audit',
    min_assurance: 'key',
    allow_user_keys: true,
    allowed_issuers: null,
    max_token_lifetime_s: 3600,
    max_key_ttl_days: null,
};

const PRODUCTION: IdentityPolicy = { ...DEVELOPMENT, mode: 'enforce', min_assurance: 'workload', allow_user_keys: false };

function answer(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return {
        group_id: 'rg-1',
        group_name: 'Loan Pre-screening',
        is_ai_system: true,
        lifecycle_stage: 'approved',
        eu_ai_act_risk_tier: 'limited',
        attributed_to_ai_system: true,
        policy: null,
        effective: DEVELOPMENT,
        inherited_floor: DEVELOPMENT,
        sources: Object.fromEntries(POLICY_FIELDS.map((field) => [field, { kind: 'default' }])),
        max_user_key_ttl_days: 90,
        ...overrides,
    };
}

/** A refusal as the client throws it: the whole answer is the error's details. */
function refused(status: number, detail: unknown): GatewayAPIError {
    return new GatewayAPIError('refused', status, undefined, { detail });
}

describe('the permission strings', () => {
    it('are the Control Plane\'s', () => {
        expect(IDENTITY_POLICY_READ).toBe('identity_policy:read');
        expect(IDENTITY_POLICY_MANAGE).toBe('identity_policy:manage');
    });
});

describe('parseIdentityPolicy', () => {
    it('reads a complete policy', () => {
        expect(parseIdentityPolicy(PRODUCTION)).toEqual(PRODUCTION);
        expect(parseIdentityPolicy({ ...PRODUCTION, allowed_issuers: ['idp-2', 'idp-1'], max_key_ttl_days: 30 })).toEqual({
            ...PRODUCTION,
            allowed_issuers: ['idp-2', 'idp-1'],
            max_key_ttl_days: 30,
        });
    });

    it('refuses a policy with a member missing, unknown or of the wrong type', () => {
        const { mode: _mode, ...noMode } = PRODUCTION;
        void _mode;
        expect(parseIdentityPolicy(noMode)).toBeNull();
        expect(parseIdentityPolicy({ ...PRODUCTION, min_assurance: 'strong' })).toBeNull();
        expect(parseIdentityPolicy({ ...PRODUCTION, mode: 'block' })).toBeNull();
        expect(parseIdentityPolicy({ ...PRODUCTION, allow_user_keys: 'false' })).toBeNull();
        expect(parseIdentityPolicy({ ...PRODUCTION, allowed_issuers: [1] })).toBeNull();
        expect(parseIdentityPolicy({ ...PRODUCTION, max_token_lifetime_s: '60' })).toBeNull();
        expect(parseIdentityPolicy({ ...PRODUCTION, max_key_ttl_days: 'never' })).toBeNull();
        expect(parseIdentityPolicy(null)).toBeNull();
        expect(parseIdentityPolicy([])).toBeNull();
    });
});

describe('parseIdentityPolicyResponse', () => {
    it('reads the answer, sources included', () => {
        const read = parseIdentityPolicyResponse(
            answer({
                lifecycle_stage: 'active',
                effective: PRODUCTION,
                sources: {
                    ...Object.fromEntries(POLICY_FIELDS.map((field) => [field, { kind: 'default' }])),
                    mode: { kind: 'lifecycle', group_id: 'rg-1', group_name: 'Loan Pre-screening', stage: 'active' },
                },
            }),
        );
        expect(read.effective).toEqual(PRODUCTION);
        expect(read.policy).toBeNull();
        expect(read.sources.mode).toEqual({ kind: 'lifecycle', group_id: 'rg-1', group_name: 'Loan Pre-screening', stage: 'active' });
        expect(read.sources.min_assurance).toEqual({ kind: 'default' });
        expect(read.max_user_key_ttl_days).toBe(90);
        expect(read.attributed_to_ai_system).toBe(true);
    });

    it('refuses an answer whose effective policy or floor cannot be read: nothing weaker is shown', () => {
        expect(() => parseIdentityPolicyResponse(answer({ effective: { mode: 'enforce' } }))).toThrow(/identity policy/i);
        expect(() => parseIdentityPolicyResponse(answer({ inherited_floor: null }))).toThrow(/identity policy/i);
        expect(() => parseIdentityPolicyResponse(answer({ sources: { mode: { kind: 'default' } } }))).toThrow(/identity policy/i);
        expect(() => parseIdentityPolicyResponse(answer({ policy: { mode: 'audit' } }))).toThrow(/identity policy/i);
        expect(() => parseIdentityPolicyResponse('nope')).toThrow(/identity policy/i);
    });
});

describe('sourceText', () => {
    const response = parseIdentityPolicyResponse(answer());

    it('says where a field is inherited from, by the group\'s name and the kind of contribution', () => {
        expect(sourceText({ kind: 'lifecycle', group_id: 'rg-0', group_name: 'Lending', stage: 'active' }, response)).toBe(
            'inherited from Lending (lifecycle: active)',
        );
        expect(sourceText({ kind: 'risk_floor', group_id: 'rg-0', group_name: 'Lending', risk_tier: 'high' }, response)).toBe(
            'inherited from Lending (high-risk floor: high)',
        );
        expect(sourceText({ kind: 'explicit', group_id: 'rg-0', group_name: 'Lending' }, response)).toBe('inherited from Lending (explicit policy)');
    });

    it('names this group as itself, and the contributions that belong to no group', () => {
        expect(sourceText({ kind: 'explicit', group_id: 'rg-1', group_name: 'Loan Pre-screening' }, response)).toBe('this group (explicit policy)');
        expect(sourceText({ kind: 'default' }, response)).toBe('the development default');
        expect(sourceText({ kind: 'tenant_default' }, response)).toBe('the tenant default');
        expect(sourceText({ kind: 'intersection' }, response)).toBe('the intersection of the issuer lists of the hierarchy');
    });

    it('says when a gate exception lowered the production floor', () => {
        expect(
            sourceText({ kind: 'lifecycle', group_id: 'rg-1', group_name: 'Loan Pre-screening', stage: 'active', lowered_by_exception: true }, response),
        ).toBe('this group (lifecycle: active), lowered by a gate exception');
    });
});

describe('validatePolicy', () => {
    it('accepts what the Control Plane accepts', () => {
        expect(validatePolicy(PRODUCTION)).toEqual({});
        expect(validatePolicy({ ...PRODUCTION, max_token_lifetime_s: 60, max_key_ttl_days: 3650, allowed_issuers: ['idp-1'] })).toEqual({});
    });

    it('names each field out of range', () => {
        expect(validatePolicy({ ...PRODUCTION, max_token_lifetime_s: 59, max_key_ttl_days: 0, allowed_issuers: [] })).toEqual({
            allowed_issuers: 'Choose at least one identity provider, or accept any.',
            max_token_lifetime_s: 'Between 60 and 3600 seconds.',
            max_key_ttl_days: 'Between 1 and 3650 days, or empty for no ceiling.',
        });
        expect(validatePolicy({ ...PRODUCTION, max_token_lifetime_s: 3601.5 }).max_token_lifetime_s).toBe('Between 60 and 3600 seconds.');
        expect(validatePolicy({ ...PRODUCTION, allowed_issuers: ['a', 'a'] }).allowed_issuers).toBe('An identity provider is listed twice.');
    });
});

describe('fieldsBelowFloor', () => {
    it('names the fields in which a policy is looser than the floor, in policy order', () => {
        expect(fieldsBelowFloor(DEVELOPMENT, PRODUCTION)).toEqual(['mode', 'min_assurance', 'allow_user_keys']);
        expect(fieldsBelowFloor(PRODUCTION, DEVELOPMENT)).toEqual([]);
        expect(fieldsBelowFloor(PRODUCTION, PRODUCTION)).toEqual([]);
    });

    it('holds the numbers and the issuers to the floor', () => {
        const floor: IdentityPolicy = { ...PRODUCTION, allowed_issuers: ['idp-1', 'idp-2'], max_token_lifetime_s: 600, max_key_ttl_days: 30 };
        expect(fieldsBelowFloor({ ...floor, allowed_issuers: null, max_token_lifetime_s: 601, max_key_ttl_days: null }, floor)).toEqual([
            'allowed_issuers',
            'max_token_lifetime_s',
            'max_key_ttl_days',
        ]);
        expect(fieldsBelowFloor({ ...floor, allowed_issuers: ['idp-3'] }, floor)).toEqual(['allowed_issuers']);
        expect(fieldsBelowFloor({ ...floor, allowed_issuers: ['idp-2'], max_token_lifetime_s: 60, max_key_ttl_days: 7 }, floor)).toEqual([]);
    });
});

describe('policyRefusal', () => {
    it('reads the floor refusal per field', () => {
        const error = refused(422, {
            message: 'The policy relaxes what this group inherits: min_assurance',
            code: 'identity_policy_below_floor',
            problems: [
                { field: 'min_assurance', requested: 'key', floor: 'workload', source: { kind: 'lifecycle', group_id: 'rg-0', group_name: 'Lending', stage: 'active' } },
                { field: 'allow_user_keys', requested: true, floor: false, source: { kind: 'lifecycle', group_id: 'rg-0', group_name: 'Lending', stage: 'active' } },
            ],
        });
        const refusal = policyRefusal(error);
        expect(refusal?.code).toBe('identity_policy_below_floor');
        expect(refusal?.message).toBe('The policy relaxes what this group inherits: min_assurance');
        expect(refusal?.problems).toEqual([
            { field: 'min_assurance', requested: 'key', floor: 'workload', source: { kind: 'lifecycle', group_id: 'rg-0', group_name: 'Lending', stage: 'active' } },
            { field: 'allow_user_keys', requested: true, floor: false, source: { kind: 'lifecycle', group_id: 'rg-0', group_name: 'Lending', stage: 'active' } },
        ]);
    });

    it('reads the unknown-issuer refusal and the permission refusal', () => {
        expect(policyRefusal(refused(422, { message: 'Unknown issuer', code: 'identity_policy_unknown_issuer', issuers: ['idp-9'] }))).toEqual({
            code: 'identity_policy_unknown_issuer',
            message: 'Unknown issuer',
            problems: [],
            issuers: ['idp-9'],
        });
        expect(policyRefusal(refused(403, { message: 'Needs a permission', code: 'permission_denied', permission: 'identity_policy:manage' }))?.code).toBe(
            'permission_denied',
        );
    });

    it('is null for anything else', () => {
        expect(policyRefusal(refused(422, [{ loc: ['body', 'policy', 'mode'], msg: 'bad' }]))).toBeNull();
        expect(policyRefusal(new Error('network'))).toBeNull();
        expect(policyRefusal(refused(409, { message: 'x', code: 'something_else' }))).toBeNull();
    });
});

describe('keyMintRefusal', () => {
    const body = (code: string, effective: IdentityPolicy = PRODUCTION) => ({
        message: `server sentence for ${code}`,
        code,
        effective_policy: effective,
        sources: { min_assurance: { kind: 'lifecycle', group_id: 'rg-0', group_name: 'Lending', stage: 'active' } },
    });

    it('says what the policy requires for each refusal code', () => {
        expect(keyMintRefusal(refused(422, body('key_refused_by_identity_policy')))).toEqual({
            code: 'key_refused_by_identity_policy',
            text:
                'The identity policy of this group requires credential level workload (inherited from Lending (lifecycle: active)); ' +
                'an API key is level key, so the gateway would refuse it. Authenticate the agent with workload identity, or develop in a separate AI System.',
        });
        expect(keyMintRefusal(refused(422, body('user_key_not_allowed')))?.text).toBe(
            'The identity policy of this group does not accept personal API keys. Create a shared key, or authenticate the agent with workload identity.',
        );
        expect(keyMintRefusal(refused(422, body('key_expiry_required', { ...PRODUCTION, max_key_ttl_days: 30 })))?.text).toBe(
            'This key needs an expiry date: the identity policy allows keys to live at most 30 days.',
        );
        expect(keyMintRefusal(refused(422, body('key_expiry_required')))?.text).toBe(
            'This key needs an expiry date: a personal key always has one.',
        );
        expect(keyMintRefusal(refused(422, body('key_expiry_exceeds_ceiling', { ...PRODUCTION, max_key_ttl_days: 30 })))?.text).toBe(
            'The expiry is later than the identity policy allows (at most 30 days from now). server sentence for key_expiry_exceeds_ceiling',
        );
    });

    it('falls back to the server\'s sentence when the effective policy cannot be read, and is null for other refusals', () => {
        expect(keyMintRefusal(refused(422, { message: 'Refused by policy', code: 'key_refused_by_identity_policy' }))?.text).toBe('Refused by policy');
        expect(keyMintRefusal(refused(422, { message: 'x', code: 'name_taken' }))).toBeNull();
        expect(keyMintRefusal(new Error('network'))).toBeNull();
    });
});

describe('keyStanding', () => {
    const at = (overrides: Record<string, unknown>): IdentityPolicyResponse => parseIdentityPolicyResponse(answer(overrides));

    it('every key is level key; a development AI System will refuse it after promotion', () => {
        expect(keyStanding(at({}), 'shared')).toEqual({ level: 'key', standing: 'refused_after_promotion' });
    });

    it('is refused now when the policy enforces a level above key', () => {
        expect(keyStanding(at({ lifecycle_stage: 'active', effective: PRODUCTION }), 'shared')).toEqual({ level: 'key', standing: 'refused', required: 'workload' });
    });

    it('is refused now when it is a personal key the enforced policy does not accept', () => {
        const effective = { ...DEVELOPMENT, mode: 'enforce', allow_user_keys: false };
        expect(keyStanding(at({ effective }), 'user')).toEqual({ level: 'key', standing: 'refused', required: 'key' });
        expect(keyStanding(at({ effective, attributed_to_ai_system: false }), 'shared')).toEqual({ level: 'key', standing: 'met' });
    });

    it('is served but recorded below policy under audit', () => {
        expect(keyStanding(at({ effective: { ...DEVELOPMENT, min_assurance: 'workload' } }), 'shared')).toEqual({
            level: 'key',
            standing: 'below_policy',
            required: 'workload',
        });
    });

    it('is met in a group no AI System governs', () => {
        expect(keyStanding(at({ attributed_to_ai_system: false, is_ai_system: false }), 'shared')).toEqual({ level: 'key', standing: 'met' });
    });
});

describe('parseIdentityCredentials', () => {
    it('reads each credential, keeping a level that is null', () => {
        const list = parseIdentityCredentials({
            items: [
                {
                    credential_ref: 'api_key:k-1', credential_kind: 'api_key', credential_assurance: 'key',
                    calls: 12, below_policy_calls: 12, refused_calls: 0,
                    first_seen: '2026-11-10T08:00:00Z', last_seen: '2026-11-17T08:00:00Z',
                    api_key_id: 'k-1', api_key_name: 'dev key', agent_id: null,
                },
                {
                    credential_ref: 'session:admin:12', credential_kind: 'human_session', credential_assurance: null,
                    calls: 1, below_policy_calls: 0, refused_calls: 0,
                    first_seen: '2026-11-17T08:00:00Z', last_seen: '2026-11-17T08:00:00Z',
                    api_key_id: null, api_key_name: null, agent_id: null,
                },
            ],
            total: 2,
        });
        expect(list.total).toBe(2);
        expect(list.items[0]).toMatchObject({ credential_ref: 'api_key:k-1', credential_assurance: 'key', api_key_name: 'dev key', calls: 12 });
        expect(list.items[1]).toMatchObject({ credential_kind: 'human_session', credential_assurance: null });
    });

    it('refuses an answer that is not a list of credentials', () => {
        expect(() => parseIdentityCredentials({ items: 'x', total: 1 })).toThrow(/credentials/i);
        expect(() => parseIdentityCredentials({ items: [{ credential_ref: 7, credential_kind: 'api_key', calls: 1 }], total: 1 })).toThrow(/credentials/i);
        expect(() => parseIdentityCredentials({ items: [{ credential_kind: 'api_key' }], total: 1 })).toThrow(/credentials/i);
    });
});

describe('a credential without a reference', () => {
    // The Control Plane answers `credential_ref` null for a credential that
    // has none: it is a credential all the same, never a reason to refuse the list.
    it('is read in the credentials list', () => {
        const list = parseIdentityCredentials({
            items: [{ credential_ref: null, credential_kind: 'anonymous', credential_assurance: null, calls: 2, below_policy_calls: 2, refused_calls: 0, first_seen: null, last_seen: null, api_key_id: null, api_key_name: null, agent_id: null }],
            total: 1,
        });
        expect(list.items).toHaveLength(1);
        expect(list.items[0].credential_ref).toBeNull();
    });

    it('is read among the offending credentials', () => {
        expect(offendingCredentialsOf({ offending_credentials: [{ credential_ref: null, credential_kind: 'anonymous', credential_assurance: null, calls: 2, last_seen: null }], offending_total: 1 })).toEqual({
            items: [{ credential_ref: null, credential_kind: 'anonymous', credential_assurance: null, calls: 2, last_seen: null }],
            total: 1,
        });
    });
});

describe('offendingCredentialsOf', () => {
    it('reads the offending credentials of the workload identity requirement', () => {
        const requirement = {
            key: 'workload_identity_enforced',
            label: 'Machine callers authenticate with workload identity',
            satisfied: false,
            offending_credentials: [
                { credential_ref: 'api_key:k-1', credential_kind: 'api_key', credential_assurance: 'key', calls: 40, last_seen: '2026-11-17T08:00:00Z' },
            ],
            offending_total: 3,
        };
        expect(offendingCredentialsOf(requirement)).toEqual({
            items: [{ credential_ref: 'api_key:k-1', credential_kind: 'api_key', credential_assurance: 'key', calls: 40, last_seen: '2026-11-17T08:00:00Z' }],
            total: 3,
        });
    });

    it('is null when the requirement carries none', () => {
        expect(offendingCredentialsOf({ key: 'owner_set', satisfied: false })).toBeNull();
        expect(offendingCredentialsOf({ key: 'workload_identity_enforced', satisfied: true, offending_credentials: null })).toBeNull();
    });

    it('counts the listed ones when the total is missing, and skips an entry it cannot read', () => {
        expect(
            offendingCredentialsOf({
                offending_credentials: [
                    { credential_ref: 'api_key:k-1', credential_kind: 'api_key', credential_assurance: 'key', calls: 1, last_seen: null },
                    { nonsense: true },
                ],
            }),
        ).toEqual({ items: [{ credential_ref: 'api_key:k-1', credential_kind: 'api_key', credential_assurance: 'key', calls: 1, last_seen: null }], total: 1 });
    });
});

describe('identityPolicyDefaultOf', () => {
    it('reads the tenant default, or null', () => {
        expect(identityPolicyDefaultOf({ identity_policy_default: { ...PRODUCTION, max_user_key_ttl_days: 30 } })).toEqual({
            ...PRODUCTION,
            max_user_key_ttl_days: 30,
        });
        expect(identityPolicyDefaultOf({ identity_policy_default: null })).toBeNull();
        expect(identityPolicyDefaultOf({})).toBeNull();
    });

    it('refuses a stored default it cannot read', () => {
        expect(() => identityPolicyDefaultOf({ identity_policy_default: { ...PRODUCTION } })).toThrow(/tenant identity default/i);
    });
});

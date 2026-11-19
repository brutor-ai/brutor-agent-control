/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The identity client module: which request each method makes, and that
 * each answer is read (a malformed one is refused, not passed on).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { IdentityApi } from '../identity';
import { POLICY_FIELDS, type IdentityPolicy } from '../../identityPolicy';

class Client extends IdentityApi {}

function client(response: unknown) {
    const c = Object.create(Client.prototype) as Client;
    const request = vi.fn().mockResolvedValue(response);
    (c as unknown as { request: typeof request }).request = request;
    return { c, request };
}

const POLICY: IdentityPolicy = {
    mode: 'enforce',
    min_assurance: 'workload',
    allow_user_keys: false,
    allowed_issuers: null,
    max_token_lifetime_s: 900,
    max_key_ttl_days: 30,
};

const ANSWER = {
    group_id: 'rg/1',
    group_name: 'Loans',
    is_ai_system: true,
    lifecycle_stage: 'approved',
    eu_ai_act_risk_tier: null,
    attributed_to_ai_system: true,
    policy: POLICY,
    effective: POLICY,
    inherited_floor: { ...POLICY, mode: 'audit', min_assurance: 'key', allow_user_keys: true, max_token_lifetime_s: 3600, max_key_ttl_days: null },
    sources: Object.fromEntries(POLICY_FIELDS.map((field) => [field, { kind: 'explicit', group_id: 'rg/1', group_name: 'Loans' }])),
    max_user_key_ttl_days: 90,
};

describe('IdentityApi', () => {
    beforeEach(() => vi.clearAllMocks());

    it('reads a group\'s identity policy, with its id escaped', async () => {
        const { c, request } = client(ANSWER);
        const read = await c.fetchIdentityPolicy('rg/1');
        expect(request).toHaveBeenCalledWith('/v1/admin/resource-groups/rg%2F1/identity-policy');
        expect(read.policy).toEqual(POLICY);
        expect(read.sources.mode.kind).toBe('explicit');
    });

    it('refuses an answer it cannot read', async () => {
        const { c } = client({ group_id: 'rg-1' });
        await expect(c.fetchIdentityPolicy('rg-1')).rejects.toThrow(/identity policy/i);
    });

    it('writes the policy wrapped, so null can be said', async () => {
        const { c, request } = client(ANSWER);
        await c.putIdentityPolicy('rg/1', POLICY);
        expect(request).toHaveBeenLastCalledWith('/v1/admin/resource-groups/rg%2F1/identity-policy', {
            method: 'PUT',
            body: JSON.stringify({ policy: POLICY }),
        });
        await c.putIdentityPolicy('rg/1', null);
        expect(request).toHaveBeenLastCalledWith('/v1/admin/resource-groups/rg%2F1/identity-policy', {
            method: 'PUT',
            body: JSON.stringify({ policy: null }),
        });
    });

    it('lists an AI System\'s credentials over a window, newest first as the server orders them', async () => {
        const { c, request } = client({ items: [], total: 0 });
        await expect(c.fetchIdentityCredentials('sys/1')).resolves.toEqual({ items: [], total: 0 });
        expect(request).toHaveBeenLastCalledWith('/v1/admin/ai-systems/sys%2F1/identity/credentials?days=30&limit=100&offset=0');
        await c.fetchIdentityCredentials('sys-1', { days: 7, limit: 20, offset: 40 });
        expect(request).toHaveBeenLastCalledWith('/v1/admin/ai-systems/sys-1/identity/credentials?days=7&limit=20&offset=40');
    });

    it('sets the tenant identity default alone, and clears it with null', async () => {
        const { c, request } = client({ id: 't-1', identity_policy_default: null });
        const value = { ...POLICY, max_user_key_ttl_days: 30 };
        await c.updateTenantIdentityPolicyDefault(value);
        expect(request).toHaveBeenLastCalledWith('/v1/admin/tenants/current', {
            method: 'PATCH',
            body: JSON.stringify({ identity_policy_default: value }),
        });
        await expect(c.updateTenantIdentityPolicyDefault(null)).resolves.toBeNull();
        expect(request).toHaveBeenLastCalledWith('/v1/admin/tenants/current', {
            method: 'PATCH',
            body: JSON.stringify({ identity_policy_default: null }),
        });
    });
});

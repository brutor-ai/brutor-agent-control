/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The tenant an administrator without one acts on.
 *
 * A tenant administrator's requests are answered for its own tenant. An
 * administrator of no tenant (a system administrator) has to name the
 * tenant in `?tenant_id=` on every tenant-scoped route, or the Control
 * Plane answers 400. While such an administrator has a tenant chosen, the
 * API client names it on every admin request that does not name one.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { actingTenant, setActingTenant, withTenantId } from '../base';
import { GatewayApiClient } from '../../gatewayApiClient';

afterEach(() => {
    setActingTenant(null);
    vi.restoreAllMocks();
});

describe('withTenantId', () => {
    it('names the tenant on an admin route, with or without a query', () => {
        expect(withTenantId('/v1/admin/approvals', 'acme')).toBe('/v1/admin/approvals?tenant_id=acme');
        expect(withTenantId('/v1/admin/approvals?status_filter=pending', 'acme')).toBe('/v1/admin/approvals?status_filter=pending&tenant_id=acme');
        expect(withTenantId('/v1/admin/system/caches', 'a b&c')).toBe('/v1/admin/system/caches?tenant_id=a%20b%26c');
    });

    it('leaves a request that names a tenant itself, and one outside the admin API', () => {
        expect(withTenantId('/v1/admin/licence?tenant_id=globex', 'acme')).toBe('/v1/admin/licence?tenant_id=globex');
        expect(withTenantId('/v1/admin/x?a=1&tenant_id=globex', 'acme')).toBe('/v1/admin/x?a=1&tenant_id=globex');
        expect(withTenantId('/health', 'acme')).toBe('/health');
    });

    it('changes nothing while no tenant is chosen', () => {
        expect(withTenantId('/v1/admin/approvals', null)).toBe('/v1/admin/approvals');
    });
});

describe('the API client', () => {
    const requested = async (endpoint: string, options: { ofNoTenant?: boolean } = {}): Promise<string> => {
        const fetched = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
        );
        await new GatewayApiClient('http://control-plane.test', () => {}).request(endpoint, options);
        return fetched.mock.calls[0][0] as string;
    };

    it('names no tenant on a request that is about the installation, such as the list of tenants to choose from', async () => {
        setActingTenant('acme');
        expect(await requested('/v1/admin/tenants', { ofNoTenant: true })).toBe('http://control-plane.test/v1/admin/tenants');
    });

    it('names the chosen tenant on its admin requests, on every instance', async () => {
        setActingTenant('acme');
        expect(actingTenant()).toBe('acme');
        expect(await requested('/v1/admin/approvals?page_size=100')).toBe('http://control-plane.test/v1/admin/approvals?page_size=100&tenant_id=acme');
    });

    it('names none once the choice is cleared', async () => {
        setActingTenant('acme');
        setActingTenant(null);
        expect(await requested('/v1/admin/approvals')).toBe('http://control-plane.test/v1/admin/approvals');
    });
});

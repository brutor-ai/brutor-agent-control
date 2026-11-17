/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A control plane that would answer a system administrator, if asked: the
 * `fetch` double of `app/__tests__/communityPrincipals.test.tsx` and of its
 * Enterprise half under `enterprise/src/host/` (the comparisons with the
 * console that has the Enterprise extension).
 */

import { vi } from 'vitest';

export function jwt(claims: Record<string, unknown>): string {
    const part = (value: object) =>
        btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ ...claims, exp: 4102444800 })}.sig`;
}

export const TENANT_ADMIN = {
    id: 'ta-1',
    admin_type: 'tenant',
    username: 'admin',
    email: 'admin@acme.test',
    role_id: 'role-tenant-admin',
    role_name: 'Tenant Admin',
    tenant_id: 'acme',
    permissions: ['resourcegroup:read', 'system:read'],
};
/**
 * The `admin` of the control plane's answer to a system administrator's
 * sign-in, field for field (`SystemAdmin.to_dict()` in
 * `app/database/models/admin.py`). It has no `role_id` and no `role_name`:
 * a system administrator has no tenant role. What makes it hold every
 * permission there is `system:admin` among its `permissions`.
 */
export const SYSTEM_ADMIN = {
    id: 'sa-1',
    admin_type: 'system',
    username: 'sysadmin',
    email: 'root@platform.test',
    full_name: 'Platform Root',
    is_active: true,
    is_locked: false,
    roles: ['super_admin'],
    permissions: ['system:admin'],
    last_login: null,
    created_at: '2026-11-15T09:00:00+00:00',
    totp_enabled: false,
};
export const TOKEN = jwt({ sub: 'x', typ: 'admin' });

/** What the control plane answers (each test sets what it needs) and what it was asked. */
export const plane: {
    me: Record<string, unknown>;
    tenantLogin: Record<string, unknown>;
    secondFactor: Record<string, unknown>;
    requests: string[];
    /** The requests that carried a session token. */
    withToken: string[];
} = { me: TENANT_ADMIN, tenantLogin: {}, secondFactor: {}, requests: [], withToken: [] };

function stubFetch(): void {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
        const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        const url = new URL(href);
        const method = (init?.method ?? 'GET').toUpperCase();
        plane.requests.push(`${method} ${url.pathname}${url.search}`);
        if ((init?.headers as Record<string, string> | undefined)?.Authorization) {
            plane.withToken.push(`${method} ${url.pathname}${url.search}`);
        }
        let payload: unknown = {};
        if (url.pathname === '/v1/admin/users/tenant/login') payload = plane.tenantLogin;
        else if (url.pathname === '/v1/admin/users/system/login') payload = { access_token: TOKEN, admin: SYSTEM_ADMIN };
        else if (url.pathname === '/v1/admin/users/login/2fa') payload = plane.secondFactor;
        else if (url.pathname === '/v1/admin/users/me') payload = plane.me;
        else if (url.pathname === '/v1/admin/tenants/current') payload = { id: 'acme', display_name: 'Acme Corp', features: {} };
        else if (url.pathname === '/v1/admin/resource-groups/hierarchy-tree') payload = { tree: [] };
        return Promise.resolve(
            new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } }),
        );
    });
}

/** How every case starts: a tenant administrator's control plane, nothing stored, the console quiet. For a `beforeEach`. */
export function resetControlPlane(): void {
    plane.me = TENANT_ADMIN;
    plane.tenantLogin = { access_token: TOKEN, admin: TENANT_ADMIN, tenant_id: 'acme', tenant_display_name: 'Acme Corp' };
    plane.secondFactor = {};
    plane.requests = [];
    plane.withToken = [];
    localStorage.clear();
    stubFetch();
    for (const method of ['log', 'warn', 'error', 'info', 'debug'] as const) {
        vi.spyOn(console, method).mockImplementation(() => {});
    }
}

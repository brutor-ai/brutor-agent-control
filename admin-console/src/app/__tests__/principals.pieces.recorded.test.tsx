/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The pieces that answer differently for the two principals, each under
 * the real auth provider with a restored session: the
 * profile page, the questions the auth context answers, and the tenant
 * feature flags.
 *
 * A recorder, like `principals.recorded.test.tsx` beside it: with the
 * built-in slot items provided the record must not change. The harness
 * provides those items around the auth provider.
 */

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../../components/users/TwoFactorSetup', () => ({
    TwoFactorSetup: () => <div>TwoFactorSetup</div>,
}));

// The console's own slot items: what the Community console provides. The
// system administrator's pieces are recorded beside the Enterprise extension
// (`enterprise/src/host/app/__tests__/principals.pieces.recorded.test.tsx`).
import { BUILTIN_SLOTS } from '../builtinSlots';
import { SlotProvider } from '../SlotProvider';
import TenantAdminProfile from '../../components/users/TenantAdminProfile';
import { TenantFeaturesProvider, useTenantFeatures } from '../../context/TenantFeaturesContext';
import { AuthProvider, useAuth } from '../../hooks/useAuth';

function jwt(claims: Record<string, unknown>): string {
    const part = (value: object) =>
        btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ ...claims, exp: 4102444800 })}.sig`;
}

const ADMINS = {
    tenant: {
        id: 'ta-1',
        admin_type: 'tenant',
        username: 'admin',
        email: 'admin@acme.test',
        full_name: 'Acme Admin',
        role_id: 'role-tenant-admin',
        role_name: 'Tenant Admin',
        tenant_id: 'acme',
        permissions: ['resourcegroup:read', 'system:read'],
    },
    system: {
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
    },
} as const;

type Principal = keyof typeof ADMINS;

let requests: string[] = [];

/** A stored session of `principal`, and a control plane that confirms it. */
function restore(principal: Principal): void {
    const admin = ADMINS[principal];
    localStorage.setItem('gateway_auth_token', jwt({ sub: admin.id, typ: 'admin', admin_type: principal }));
    localStorage.setItem('gateway_user', JSON.stringify(admin));
    localStorage.setItem('gateway_admin_type', principal);
    if (principal === 'tenant') localStorage.setItem('gateway_tenant_id', 'acme');
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
        const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        const url = new URL(href);
        requests.push(`${(init?.method ?? 'GET').toUpperCase()} ${url.pathname}${url.search}`);
        const payload: unknown =
            url.pathname === '/v1/admin/users/me'
                ? admin
                : url.pathname === '/v1/admin/tenants/current'
                  ? { id: 'acme', display_name: 'Acme Corp', features: { models: false, a2a: true } }
                  : {};
        return Promise.resolve(
            new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } }),
        );
    });
}

function mount(ui: React.ReactNode) {
    return render(
        <SlotProvider slots={BUILTIN_SLOTS}>
            <AuthProvider gatewayUrl="http://cp.test">{ui}</AuthProvider>
        </SlotProvider>,
    );
}

async function settled(): Promise<void> {
    for (let i = 0; i < 5; i++) {
        await act(async () => {
            await new Promise((resolve) => setTimeout(resolve, 0));
        });
    }
}

const PERMISSIONS = ['resourcegroup:read', 'system:read', 'platform:catalog:manage', 'tenant:delete', 'no:such'];

/** What the auth context answers, as text. */
const AuthProbe: React.FC = () => {
    const { user, isAuthenticated, isLoading, isTenantAdmin, getCurrentTenantId, hasPermission, hasRole } = useAuth();
    if (isLoading) return <div data-testid="auth">loading</div>;
    const answers = {
        isAuthenticated,
        admin_type: user?.admin_type ?? null,
        tenant_id: user?.tenant_id ?? null,
        isTenantAdmin: isTenantAdmin(),
        getCurrentTenantId: getCurrentTenantId(),
        hasRoleTenantAdmin: hasRole('Tenant Admin'),
        hasPermission: Object.fromEntries(PERMISSIONS.map((p) => [p, hasPermission(p)])),
    };
    return <div data-testid="auth">{JSON.stringify(answers)}</div>;
};

const FeaturesProbe: React.FC = () => {
    const { features, isFeatureEnabled, displayName, loaded } = useTenantFeatures();
    if (!loaded) return <div data-testid="features">loading</div>;
    const answers = { features, displayName, models: isFeatureEnabled('models'), a2a: isFeatureEnabled('a2a'), other: isFeatureEnabled('other') };
    return <div data-testid="features">{JSON.stringify(answers)}</div>;
};

beforeEach(() => {
    requests = [];
    localStorage.clear();
    for (const method of ['log', 'warn', 'error', 'info', 'debug'] as const) {
        vi.spyOn(console, method).mockImplementation(() => {});
    }
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

const restoredAs = (principal: 'tenant' | 'system') => {
    it('the auth context answers', async () => {
        restore(principal);
        mount(<AuthProbe />);
        await waitFor(() => expect(screen.getByTestId('auth')).not.toHaveTextContent('loading'));
        expect(JSON.parse(screen.getByTestId('auth').textContent ?? '')).toMatchSnapshot();
    });

    it('the tenant feature flags', async () => {
        restore(principal);
        mount(
            <TenantFeaturesProvider>
                <FeaturesProbe />
            </TenantFeaturesProvider>,
        );
        await settled();
        await waitFor(() => expect(screen.getByTestId('features')).not.toHaveTextContent('loading'));
        await settled();
        expect({
            answers: JSON.parse(screen.getByTestId('features').textContent ?? '') as unknown,
            requests: [...requests].sort(),
        }).toMatchSnapshot();
    });

    it('the profile page, on Profile and on Security', async () => {
        restore(principal);
        const { container } = mount(<TenantAdminProfile />);
        await screen.findByText(ADMINS[principal].email);
        await settled();
        expect(container.innerHTML).toMatchSnapshot('profile');
        fireEvent.click(screen.getByRole('button', { name: 'Security' }));
        expect(container.innerHTML).toMatchSnapshot('security');
    });
};

describe.each(['tenant'] as const)('restored as the %s administrator', (principal) => {
    restoredAs(principal);
});

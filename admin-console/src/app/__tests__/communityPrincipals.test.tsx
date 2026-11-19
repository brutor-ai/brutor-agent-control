/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console without a system administrator.
 *
 * The Community Edition has one kind of administrator: the installation's,
 * a tenant administrator. Everything the console does for a system
 * administrator is contributed (slot `app.principals`, the Enterprise
 * screens and API module). This file holds the Community half to that:
 * composed without the Enterprise pieces (`test/communityComposition.ts`)
 * there is no way to sign in as a system administrator, a session that
 * claims to be one is no session, no screen or entry of one exists, and the
 * shared catalogs are for whoever holds `platform:catalog:manage`.
 *
 * The last blocks are about today's one build, where everything is mounted:
 * the catalog rule there, and that no Community module reaches a
 * system-administrator module except through the registries.
 */

import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
vi.mock('reactflow/dist/style.css', () => ({}));

function stub(name: string) {
    const Stub: React.FC = () => <div data-testid="screen">{name}</div>;
    Stub.displayName = name;
    return { default: Stub };
}
vi.mock('../../components/monitoring/MissionControlDashboard', () => stub('MissionControlDashboard'));
vi.mock('../../components/llm/catalog/LLMCatalogAdminTab', () => stub('LLMCatalogAdminTab'));
vi.mock('../../components/mcp/catalog/MCPCatalogAdminTab', () => stub('MCPCatalogAdminTab'));
vi.mock('../../components/users/TenantAdminProfile', () => stub('TenantAdminProfile'));

import { AppShell } from '../../App';
import { AuthProvider, useAuth } from '../../hooks/useAuth';
import { BUILTIN_API_MODULES } from '../../lib/gatewayApi/modules';
import { GatewayApiClient } from '../../lib/gatewayApiClient';
import {
    CommunityApiClient,
    communityComposition,
    communityNavigation,
    communityScreens,
} from '../../test/communityComposition';
import { plane, resetControlPlane, SYSTEM_ADMIN, TENANT_ADMIN, TOKEN } from '../../test/controlPlane';
import { NOT_SOURCE_DIRS, enterpriseUnderTest, importedPaths, scanRoots, sourceFile, sourceName } from '../../test/sourceRoots';
import { resolveScreen } from '../screens';

// ---------------------------------------------------------------------------
// A control plane that would answer a system administrator, if asked
// ---------------------------------------------------------------------------

const CATALOG = 'platform:catalog:manage';

function store(adminType: string | null, user: Record<string, unknown>): void {
    localStorage.setItem('gateway_auth_token', TOKEN);
    localStorage.setItem('gateway_user', JSON.stringify(user));
    if (adminType !== null) localStorage.setItem('gateway_admin_type', adminType);
    if (typeof user.tenant_id === 'string') localStorage.setItem('gateway_tenant_id', user.tenant_id);
}

const SESSION_KEYS = ['gateway_auth_token', 'gateway_user', 'gateway_admin_type', 'gateway_tenant_id'];
const storedSession = () => SESSION_KEYS.filter((key) => localStorage.getItem(key) !== null);

async function settled(): Promise<void> {
    for (let i = 0; i < 5; i++) {
        await act(async () => {
            await new Promise((resolve) => setTimeout(resolve, 0));
        });
    }
}

const signInForm = () => screen.findByPlaceholderText('Enter your username');
const shell = () => document.querySelector('.app-shell');
const set = (placeholder: string, value: string) =>
    fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });

function submitPassword(): void {
    set('Enter your username', 'admin');
    set('Enter your password', 'secret');
    fireEvent.click(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ }));
}

const community = communityComposition();
// Everything this tree mounts: in the Community tree, the Community
// composition. The comparisons with the console that has the Enterprise
// extension are in `enterprise/src/host/app/__tests__/communityPrincipals.test.tsx`.
const everything = community;

function mountCommunity() {
    return render(<AppShell composition={community} />);
}

beforeEach(resetControlPlane);

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------

describe('the Community composition', () => {

    it('keeps the two shared catalogs, as the whole Platform section', () => {
        const platform = communityNavigation().find((section) => section.label === 'Platform');
        expect(platform?.items.map((item) => [item.id, item.requiresPermission, item.requiresPrincipal])).toEqual([
            ['llm-catalog-admin', CATALOG, undefined],
            ['mcp-catalog-admin', CATALOG, undefined],
        ]);
        expect(resolveScreen(communityScreens(), 'llm-catalog-admin')).toBeDefined();
        expect(resolveScreen(communityScreens(), 'mcp-catalog-admin')).toBeDefined();
    });

    it('no entry of it needs a contributed principal', () => {
        const needs = communityNavigation().flatMap((s) => s.items).filter((item) => item.requiresPrincipal !== undefined);
        expect(needs.map((item) => item.id)).toEqual([]);
    });
});

describe('signing in, in the Community composition', () => {
    it('the form offers no choice of administrator and nothing of a system administrator', async () => {
        const { container } = mountCommunity();
        await signInForm();
        expect(screen.queryByText('Administrator Type')).toBeNull();
        expect(screen.queryByRole('button', { name: 'System Admin' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Tenant Admin' })).toBeNull();
        expect(container.textContent).not.toMatch(/system admin|sysadmin|platform-wide|all tenants/i);
        // The administrator's form is all there: a user name and a password.
        // Which tenant is not asked (the field is a
        // contributed sign-in item's, slot `app.sign-in`).
        expect(screen.queryByPlaceholderText('Enter tenant ID')).toBeNull();
        expect([...container.querySelectorAll('input')].map((input) => input.id)).toEqual(['username', 'password']);
        expect(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ })).toBeInTheDocument();
        // No credentials are printed: there is no stock password.
        expect(container.textContent).not.toMatch(/Development Credentials|Admin123|Administrator:/);
        expect(container.textContent).not.toMatch(/multi-tenant edition/i);
    });

    it('a tenant administrator signs in, on the tenant sign-in route only', async () => {
        mountCommunity();
        await signInForm();
        submitPassword();
        await waitFor(() => expect(shell()).not.toBeNull());
        await settled();
        expect(screen.getByTestId('screen')).toHaveTextContent('MissionControlDashboard');
        expect(plane.requests.filter((r) => r.startsWith('POST'))).toEqual(['POST /v1/admin/users/tenant/login']);
        expect(localStorage.getItem('gateway_admin_type')).toBe('tenant');
    });

    it('an answer for another kind of administrator is not a session', async () => {
        plane.tenantLogin = { access_token: TOKEN, admin: SYSTEM_ADMIN };
        mountCommunity();
        await signInForm();
        submitPassword();
        expect(await screen.findByText('Sign-in refused: unknown administrator type')).toBeInTheDocument();
        await settled();
        expect(shell()).toBeNull();
        expect(storedSession()).toEqual([]);
    });

    it.each([
        ['names another kind in the admin', { token: TOKEN, admin: SYSTEM_ADMIN }],
        ['names another kind beside the admin', { token: TOKEN, admin_type: 'system', admin: { username: 'x' } }],
        ['names a kind that is not a string', { token: TOKEN, admin: { username: 'x', admin_type: 7 } }],
    ])('a second-factor answer that %s is not a session', async (_what, answer) => {
        plane.tenantLogin = { requires_2fa: true, temp_token: 'tmp', tenant_id: 'acme' };
        plane.secondFactor = answer;
        mountCommunity();
        await signInForm();
        submitPassword();
        set(await screen.findByPlaceholderText('000000').then(() => '000000'), '123456');
        fireEvent.click(screen.getByRole('button', { name: 'Verify and sign in' }));
        expect(await screen.findByText('Sign-in refused: unknown administrator type')).toBeInTheDocument();
        await settled();
        expect(shell()).toBeNull();
        expect(storedSession()).toEqual([]);
    });
});

describe('a stored session, in the Community composition', () => {
    it('of the tenant administrator is restored', async () => {
        store('tenant', TENANT_ADMIN);
        mountCommunity();
        await waitFor(() => expect(shell()).not.toBeNull());
        await settled();
        expect(screen.queryByText('System Admin')).toBeNull();
    });

    it.each<[string, string | null, Record<string, unknown>]>([
        ['stored as a system administrator’s, and confirmed as one', 'system', SYSTEM_ADMIN],
        ['stored as a system administrator’s, whatever the control plane says', 'system', TENANT_ADMIN],
        ['stored as the tenant administrator’s, that the control plane says is a system administrator’s', 'tenant', SYSTEM_ADMIN],
        ['of a kind nobody knows', 'root', { ...TENANT_ADMIN, admin_type: 'root' }],
        ['whose kind the control plane states as something that is not a string', 'tenant', { ...TENANT_ADMIN, admin_type: ['tenant'] }],
        ['whose kind nothing states', null, { ...TENANT_ADMIN, admin_type: undefined }],
    ])('%s, is not signed in and is cleared', async (_what, storedType, answered) => {
        store(storedType, answered);
        plane.me = answered;
        mountCommunity();
        await signInForm();
        await settled();
        expect(shell()).toBeNull();
        expect(storedSession()).toEqual([]);
        // Nothing was asked on its behalf after the check.
        expect(plane.requests).toEqual(['GET /v1/admin/users/me']);
    });
});

describe('the auth context, without a provider of principals', () => {
    type Auth = ReturnType<typeof useAuth>;
    let auth: Auth | null = null;
    const Capture: React.FC = () => {
        auth = useAuth();
        return <div data-testid="ready">{auth.isLoading ? 'loading' : 'ready'}</div>;
    };

    async function mountAuth(): Promise<Auth> {
        render(
            <AuthProvider gatewayUrl="http://cp.test">
                <Capture />
            </AuthProvider>,
        );
        await waitFor(() => expect(screen.getByTestId('ready')).toHaveTextContent('ready'));
        if (!auth) throw new Error('no auth context');
        return auth;
    }

    it('cannot sign anyone in as a system administrator, and asks the control plane nothing', async () => {
        const { login } = await mountAuth();
        await expect(login('sysadmin', 'secret', 'system')).rejects.toThrow('Sign-in refused: unknown administrator type');
        expect(plane.requests).toEqual([]);
        expect(storedSession()).toEqual([]);
        expect(auth?.user).toBeNull();
    });

    it('does not complete a second factor as a system administrator', async () => {
        const { complete2FALogin } = await mountAuth();
        expect(() => complete2FALogin({ token: TOKEN, admin: SYSTEM_ADMIN }, 'system')).toThrow(
            'Sign-in refused: unknown administrator type',
        );
        expect(storedSession()).toEqual([]);
    });

    it('the token of a stored session it refuses is not used again', async () => {
        store('system', SYSTEM_ADMIN);
        plane.me = SYSTEM_ADMIN;
        const { gatewayClient } = await mountAuth();
        expect(auth?.isAuthenticated).toBe(false);
        expect(plane.withToken).toEqual(['GET /v1/admin/users/me']);

        await gatewayClient.request('/v1/admin/anything');
        expect(plane.requests).toEqual(['GET /v1/admin/users/me', 'GET /v1/admin/anything']);
        expect(plane.withToken).toEqual(['GET /v1/admin/users/me']);
    });

    it('a tenant administrator with the system administrator’s marks holds only what its session lists', async () => {
        const admin = { ...TENANT_ADMIN, roles: ['super_admin'] };
        store('tenant', admin);
        plane.me = admin;
        const context = await mountAuth();
        expect(context.isAuthenticated).toBe(true);
        expect(context.principal).toBeNull();
        expect(context.hasPermission('resourcegroup:read')).toBe(true);
        expect(context.hasPermission('tenant:delete')).toBe(false);
        expect(context.hasPermission(CATALOG)).toBe(false);
        expect('isSystemAdmin' in context).toBe(false);
    });
});

describe('the sidebar of the installation’s administrator, in the Community composition', () => {
    async function signedInWith(permissions: string[]): Promise<void> {
        const admin = { ...TENANT_ADMIN, permissions };
        store('tenant', admin);
        plane.me = admin;
        mountCommunity();
        await waitFor(() => expect(shell()).not.toBeNull());
        await settled();
    }
    const entry = (name: string) => screen.queryByRole('button', { name });

    it('without platform:catalog:manage has no Platform section', async () => {
        await signedInWith(['resourcegroup:read', 'system:read']);
        expect(screen.queryByText('Platform')).toBeNull();
        for (const name of ['LLM Catalog', 'MCP Catalog', 'Platform Overview', 'Tenants', 'My Profile']) {
            expect({ name, entry: entry(name) }).toEqual({ name, entry: null });
        }
    });

    it('with platform:catalog:manage has the two catalogs, and they open', async () => {
        await signedInWith(['resourcegroup:read', CATALOG]);
        expect(screen.getByText('Platform')).toBeInTheDocument();
        for (const name of ['Platform Overview', 'Tenants', 'My Profile']) expect({ name, entry: entry(name) }).toEqual({ name, entry: null });

        fireEvent.click(screen.getByRole('button', { name: 'LLM Catalog' }));
        await settled();
        expect(screen.getByTestId('screen')).toHaveTextContent('LLMCatalogAdminTab');
        fireEvent.click(screen.getByRole('button', { name: 'MCP Catalog' }));
        await settled();
        expect(screen.getByTestId('screen')).toHaveTextContent('MCPCatalogAdminTab');
    });
});

describe('the catalog entries where everything is mounted', () => {
    // Today's one build. A system administrator's view is in the record
    // (principals.recorded.test.tsx); here, the tenant administrator's.
    async function tenantAdminWith(permissions: string[]): Promise<void> {
        const admin = { ...TENANT_ADMIN, permissions };
        store('tenant', admin);
        plane.me = admin;
        render(<AppShell composition={everything} />);
        await waitFor(() => expect(shell()).not.toBeNull());
        await settled();
    }

    it('are hidden from a tenant administrator who does not hold platform:catalog:manage', async () => {
        await tenantAdminWith(['resourcegroup:read', 'system:read', 'llm:write', 'mcp:write']);
        expect(screen.queryByText('Platform')).toBeNull();
        expect(screen.queryByRole('button', { name: 'LLM Catalog' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'MCP Catalog' })).toBeNull();
    });

    it('are shown to one who does, without the system administrator’s entries', async () => {
        await tenantAdminWith(['resourcegroup:read', CATALOG]);
        expect(screen.getByRole('button', { name: 'LLM Catalog' })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'MCP Catalog' })).toBeInTheDocument();
        for (const name of ['Platform Overview', 'Tenants', 'My Profile']) {
            expect({ name, entry: screen.queryByRole('button', { name }) }).toEqual({ name, entry: null });
        }
    });
});

describe('the API client of the Community modules alone', () => {

    it('signs in on the tenant route whatever kind of administrator the caller names', async () => {
        const client = new CommunityApiClient('http://cp.test') as CommunityApiClient & Pick<GatewayApiClient, 'login'>;
        const asked = { username: 'sysadmin', password: 'secret', adminType: 'system' };
        const result = await client.login(asked);
        expect(plane.requests).toEqual(['POST /v1/admin/users/tenant/login']);
        expect(result.admin_type).toBe('tenant');
    });

    it('no Community module of the client names the system sign-in route', () => {
        const dir = path.resolve(__dirname, '../../lib/gatewayApi');
        const community = new Set<string>(BUILTIN_API_MODULES.filter((m) => m.feature === null).map((m) => `${m.name}.ts`));
        const naming = readdirSync(dir)
            .filter((file) => file === 'base.ts' || community.has(file))
            .filter((file) => /admin\/users\/system|system\/login/.test(readFileSync(path.join(dir, file), 'utf8')));
        expect(community.size).toBe(30);
        expect(naming).toEqual([]);
    });
});

describe('no Community module reaches a system-administrator module', () => {
    // Both editions' trees are read, and files are named as `sourceName`
    // names them: a module of another Enterprise feature may not reach the
    // system administrator's either.
    const allSources = (): string[] => scanRoots((root) => sources(root));

    /** The system administrator's modules: files, and directories (ending in `/`). */
    const SYSTEM_ADMIN_MODULES = [
        'enterprise/src/features/multi_tenant/components/tenant/',
        'enterprise/src/features/multi_tenant/components/monitoring/SystemPlatformDashboard',
        'enterprise/src/features/multi_tenant/components/configuration/PlatformSettingsTab',
        'enterprise/src/features/multi_tenant/lib/platformTypes',
        'enterprise/src/features/multi_tenant/lib/gatewayApi/multiTenant',
        'enterprise/src/features/multi_tenant/hooks/gateway/multiTenant',
    ];

    /**
     * The registries: the only files outside the system administrator's own
     * that may import one, each for what it registers. The screens, slot
     * items, API modules and hook modules of the Enterprise Edition are
     * registered in the Enterprise tree.
     */
    const REGISTRIES: Record<string, string[]> = {
        // the principal (slot `app.principals`)
        'enterprise/src/slots.tsx': ['enterprise/src/features/multi_tenant/components/tenant/systemAdminPrincipal'],
        // the screens tagged multi_tenant
        'enterprise/src/screens.tsx': [
            'enterprise/src/features/multi_tenant/components/configuration/PlatformSettingsTab',
            'enterprise/src/features/multi_tenant/components/monitoring/SystemPlatformDashboard',
            'enterprise/src/features/multi_tenant/components/tenant/SystemAdminsTab',
            'enterprise/src/features/multi_tenant/components/tenant/TenantManagementTab',
        ],
        // the API module and the hook module tagged multi_tenant
        'enterprise/src/apiModules.ts': ['enterprise/src/features/multi_tenant/lib/gatewayApi/multiTenant'],
        'enterprise/src/hooks/modules.ts': ['enterprise/src/features/multi_tenant/hooks/gateway/multiTenant'],
        // the Enterprise facade calls every hook module statically (hooks cannot be called from a list)
        'enterprise/src/hooks/useEnterpriseGateway.ts': ['enterprise/src/features/multi_tenant/hooks/gateway/multiTenant'],
    };

    /** Test helpers, per root: not modules of the console. */
    const TEST_HELPERS = new Set(['test', 'enterprise/src/test']);

    const isSystemAdminModule = (file: string) =>
        SYSTEM_ADMIN_MODULES.some((m) => (m.endsWith('/') ? file.startsWith(m) : file === m));

    function sources(dir: string): string[] {
        return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) {
                return entry.name === '__tests__' || NOT_SOURCE_DIRS.has(entry.name) || TEST_HELPERS.has(sourceName(full))
                    ? []
                    : sources(full);
            }
            return /\.tsx?$/.test(entry.name) && !/\.(test|d)\.tsx?$/.test(entry.name) ? [full] : [];
        });
    }

    /** The modules of the trees that `text`, a file at `file`, imports (without extension). */
    function imports(file: string, text: string): string[] {
        return importedPaths(file, text).map(sourceName);
    }

    function reaches(): Record<string, string[]> {
        const found: Record<string, string[]> = {};
        for (const file of allSources()) {
            const rel = sourceName(file);
            if (isSystemAdminModule(rel.replace(/\.tsx?$/, ''))) continue;
            const hits = imports(file, readFileSync(file, 'utf8')).filter(isSystemAdminModule);
            if (hits.length) found[rel] = [...new Set(hits)].sort();
        }
        return found;
    }

    it('the scan sees every kind of import', () => {
        const file = sourceFile('components/x/Y.tsx');
        expect(imports(file, "import A from '../tenant/TenantForm';\nconst B = import('../../lib/platformTypes');\nimport './z.css';")).toEqual([
            'components/tenant/TenantForm',
            'lib/platformTypes',
            'components/x/z.css',
        ]);
        // …and one written in the Enterprise tree, relative or through the alias of the Community tree.
        const moved = sourceFile('enterprise/src/features/multi_tenant/components/x/Y.tsx');
        expect(imports(moved, "import A from '../tenant/TenantForm';\nimport B from '@community/lib/narrow';")).toEqual([
            'enterprise/src/features/multi_tenant/components/tenant/TenantForm',
            'lib/narrow',
        ]);
        expect(isSystemAdminModule('enterprise/src/features/multi_tenant/components/tenant/TenantForm')).toBe(true);
        expect(isSystemAdminModule('components/tenants')).toBe(false);
        expect(allSources().length).toBeGreaterThan(500);
    });

    it('only the registries import one, each what it registers', () => {
        // A tree without `enterprise/` has no system-administrator module, and no registry of them.
        expect(reaches()).toEqual(enterpriseUnderTest() ? REGISTRIES : {});
    });

    it.each([
        ['isSystemAdmin', /\bisSystemAdmin\b/],
        ['requiresSystemAdmin', /\brequiresSystemAdmin\b/],
        ["a comparison with the 'system' administrator type", /admin_?[tT]ype\s*[!=]==?\s*['"]system['"]/],
        ["the 'system_admin' role id", /['"]system_admin['"]\s*[)\];,}]|===?\s*['"]system_admin['"]/],
        ['the system sign-in route', /admin\/users\/system\/login/],
    ])('no Community source has %s', (_what, pattern) => {
        const hits = allSources()
            .map((file) => sourceName(file))
            .filter((rel) => !isSystemAdminModule(rel.replace(/\.tsx?$/, '')))
            .filter((rel) => !rel.startsWith('lib/generated/'))
            .filter((rel) =>
                readFileSync(sourceFile(rel), 'utf8')
                    .split('\n')
                    .some((line) => !/^\s*(\/\/|\*|\/\*)/.test(line) && pattern.test(line)),
            );
        expect(hits).toEqual([]);
    });
});

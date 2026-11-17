/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useGateway — how failures reach the operator, and how users are resolved.
 *
 * Almost every method on this hook is a pass-through; the parts with real
 * behaviour are the ones below, and each has a way of going wrong that is
 * invisible in normal use:
 *
 *   - **401/403 is not a banner.** An expired session must propagate to the
 *     auth layer, which redirects to login. Recording it as a console error
 *     banner as well produces the "your session expired" toast stacked behind
 *     a login screen, and — worse — trains operators to dismiss auth errors.
 *   - **Every other failure must arrive tagged.** Banners filter on
 *     `context.resource`, so an error added without a context is an error no
 *     surface can render. `useErrorHandler`'s single-object call form
 *     carries the context; this asserts the caller side.
 *   - **The proxy URL is not the admin URL.** `llmProxyUrl` is advertised in
 *     the connect modal; building it off the admin host would hand operators
 *     a 404 against an endpoint the modal tells them to use.
 *   - **User lookups degrade to the id.** A half-loaded user list must render
 *     an id, never `undefined`, in an audit column.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
    fetchServers: vi.fn(),
    fetchVirtualServers: vi.fn(),
    fetchStats: vi.fn(),
    fetchTenantAdminUsers: vi.fn(),
    checkAllMCPServerHealth: vi.fn(),
    getRegistryConfigs: vi.fn(),
    auth: { isAuthenticated: true, isLoading: false },
}));

vi.mock('../useAuth', () => ({
    useAuth: () => ({
        isAuthenticated: mocks.auth.isAuthenticated,
        isLoading: mocks.auth.isLoading,
        gatewayClient: {
            fetchServers: mocks.fetchServers,
            fetchVirtualServers: mocks.fetchVirtualServers,
            fetchStats: mocks.fetchStats,
            fetchTenantAdminUsers: mocks.fetchTenantAdminUsers,
            checkAllMCPServerHealth: mocks.checkAllMCPServerHealth,
            getRegistryConfigs: mocks.getRegistryConfigs,
        },
    }),
}));

import { useGateway } from '../useGateway';

/** An HTTP failure shaped the way the API client raises them. */
function httpError(status: number, message = `HTTP ${status}`) {
    return Object.assign(new Error(message), { status });
}

beforeEach(() => {
    vi.clearAllMocks();
    mocks.auth.isAuthenticated = true;
    mocks.auth.isLoading = false;
    mocks.getRegistryConfigs.mockResolvedValue({ registries: {} });
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
    vi.restoreAllMocks();
});

describe('useGateway — auth gating', () => {
    it('stays in initial loading while auth is still resolving', () => {
        mocks.auth.isLoading = true;
        const { result } = renderHook(() => useGateway());

        expect(result.current.initialLoading).toBe(true);
    });

    it('settles once auth resolves, authenticated or not', async () => {
        const authed = renderHook(() => useGateway());
        await waitFor(() => expect(authed.result.current.initialLoading).toBe(false));
        expect(authed.result.current.isAuthenticated).toBe(true);

        mocks.auth.isAuthenticated = false;
        const anon = renderHook(() => useGateway());
        await waitFor(() => expect(anon.result.current.initialLoading).toBe(false));
        expect(anon.result.current.isAuthenticated).toBe(false);
    });

    it('does not fetch users for an unauthenticated caller', async () => {
        mocks.auth.isAuthenticated = false;
        const { result } = renderHook(() => useGateway());

        let users: unknown;
        await act(async () => {
            users = await result.current.fetchUsers();
        });

        expect(users).toEqual([]);
        expect(mocks.fetchTenantAdminUsers).not.toHaveBeenCalled();
    });

    it('does not fetch users when auth is disabled entirely', async () => {
        const { result } = renderHook(() => useGateway({ enableAuth: false }));

        await act(async () => {
            await result.current.fetchUsers();
        });

        expect(mocks.fetchTenantAdminUsers).not.toHaveBeenCalled();
    });
});

describe('useGateway — failure surfacing', () => {
    it.each([
        ['servers', () => mocks.fetchServers, (r: ReturnType<typeof useGateway>) => r.fetchServers],
        [
            'virtual servers',
            () => mocks.fetchVirtualServers,
            (r: ReturnType<typeof useGateway>) => r.fetchVirtualServers,
        ],
        ['stats', () => mocks.fetchStats, (r: ReturnType<typeof useGateway>) => r.fetchStats],
    ])('rethrows a 401 on %s without raising an error banner', async (_label, mock, pick) => {
        mock().mockRejectedValue(httpError(401));
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await expect(pick(result.current)()).rejects.toThrow();
        });

        // The auth layer owns this one — a banner here double-reports it.
        expect(result.current.errors).toEqual([]);
        expect(result.current.hasErrors).toBe(false);
    });

    it('rethrows a 403 without raising an error banner', async () => {
        mocks.fetchServers.mockRejectedValue(httpError(403));
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await expect(result.current.fetchServers()).rejects.toThrow();
        });

        expect(result.current.errors).toEqual([]);
    });

    it('raises a tagged banner for a server error, and still rethrows', async () => {
        mocks.fetchServers.mockRejectedValue(httpError(500, 'upstream exploded'));
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await expect(result.current.fetchServers()).rejects.toThrow('upstream exploded');
        });

        await waitFor(() => expect(result.current.errors).toHaveLength(1));
        const [err] = result.current.errors;
        expect(err.message).toBe('upstream exploded');
        expect(err.code).toBe('FETCH_SERVERS_ERROR');
        // Without this context no banner surface can filter the error in.
        expect(err.context).toMatchObject({
            operation: 'fetch_servers',
            resource: 'servers',
        });
        expect(err.severity).toBe('medium');
    });

    it('tags virtual-server and stats failures with their own resource', async () => {
        mocks.fetchVirtualServers.mockRejectedValue(new Error('vs down'));
        mocks.fetchStats.mockRejectedValue(new Error('stats down'));
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await expect(result.current.fetchVirtualServers()).rejects.toThrow();
            await expect(result.current.fetchStats()).rejects.toThrow();
        });

        await waitFor(() => expect(result.current.errors).toHaveLength(2));
        const codes = result.current.errors.map((e) => e.code);
        expect(codes).toContain('FETCH_VIRTUAL_SERVERS_ERROR');
        expect(codes).toContain('FETCH_STATS_ERROR');
        const resources = result.current.errors.map((e) => e.context?.resource);
        expect(resources).toContain('virtual_servers');
        expect(resources).toContain('stats');
    });

    it('surfaces a bulk health-check failure and rethrows for the caller', async () => {
        mocks.checkAllMCPServerHealth.mockRejectedValue(new Error('proxy unreachable'));
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await expect(result.current.triggerAllHealthChecks()).rejects.toThrow(
                'proxy unreachable'
            );
        });

        await waitFor(() => expect(result.current.errors).toHaveLength(1));
        expect(result.current.errors[0].code).toBe('TRIGGER_ALL_HEALTH_CHECKS_ERROR');
    });

    it('passes a successful bulk health check straight through', async () => {
        mocks.checkAllMCPServerHealth.mockResolvedValue({ checked: 4, healthy: 3 });
        const { result } = renderHook(() => useGateway());

        let outcome: unknown;
        await act(async () => {
            outcome = await result.current.triggerAllHealthChecks();
        });

        expect(outcome).toEqual({ checked: 4, healthy: 3 });
        expect(result.current.errors).toEqual([]);
    });

    it('a failed user load is low severity and resolves rather than throwing', async () => {
        mocks.fetchTenantAdminUsers.mockRejectedValue(new Error('users endpoint down'));
        const { result } = renderHook(() => useGateway());

        let users: unknown;
        await act(async () => {
            users = await result.current.fetchUsers();
        });

        // Users decorate other views; a failure must not take a tab down.
        expect(users).toEqual([]);
        await waitFor(() => expect(result.current.errors).toHaveLength(1));
        expect(result.current.errors[0].severity).toBe('low');
        expect(result.current.errors[0].code).toBe('FETCH_USERS_ERROR');
        // Loaded-but-empty, so the view stops spinning.
        expect(result.current.usersLoaded).toBe(true);
        expect(result.current.loadingUsers).toBe(false);
    });

    it('lets an operator dismiss and clear the banners it raised', async () => {
        mocks.fetchStats.mockRejectedValue(new Error('stats down'));
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await expect(result.current.fetchStats()).rejects.toThrow();
            await expect(result.current.fetchStats()).rejects.toThrow();
        });
        await waitFor(() => expect(result.current.errors).toHaveLength(2));

        act(() => result.current.dismissError(result.current.errors[0].id));
        expect(result.current.errors).toHaveLength(1);

        act(() => result.current.clearErrors());
        expect(result.current.errors).toEqual([]);
    });
});

describe('useGateway — user resolution', () => {
    const USERS = [
        {
            id: 'u-1',
            username: 'aoliver',
            email: 'a@acme.test',
            full_name: 'Alex Oliver',
            roles: ['tenant_admin'],
            is_active: true,
        },
        {
            id: 'u-2',
            username: 'bsingh',
            email: 'b@acme.test',
            roles: [],
            is_active: true,
        },
    ];

    it('loads users and exposes them once', async () => {
        mocks.fetchTenantAdminUsers.mockResolvedValue({ users: USERS });
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await result.current.fetchUsers();
        });

        expect(result.current.users).toHaveLength(2);
        expect(result.current.usersLoaded).toBe(true);
    });

    it('tolerates a response with no users array', async () => {
        mocks.fetchTenantAdminUsers.mockResolvedValue({});
        const { result } = renderHook(() => useGateway());

        let users: unknown;
        await act(async () => {
            users = await result.current.fetchUsers();
        });

        expect(users).toEqual([]);
    });

    it('resolves a username, a display name and roles from a loaded list', async () => {
        mocks.fetchTenantAdminUsers.mockResolvedValue({ users: USERS });
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await result.current.fetchUsers();
        });

        expect(result.current.getUsernameById('u-1')).toBe('aoliver');
        expect(result.current.getUserDisplayName('u-1')).toBe('Alex Oliver');
        expect(result.current.getUserRoles('u-1')).toEqual(['tenant_admin']);
    });

    it('falls back to the username when there is no full name', async () => {
        mocks.fetchTenantAdminUsers.mockResolvedValue({ users: USERS });
        const { result } = renderHook(() => useGateway());

        await act(async () => {
            await result.current.fetchUsers();
        });

        expect(result.current.getUserDisplayName('u-2')).toBe('bsingh');
    });

    it('renders the raw id rather than undefined for an unknown user', () => {
        const { result } = renderHook(() => useGateway());

        // An audit column showing "undefined" is worse than one showing an id.
        expect(result.current.getUsernameById('u-missing')).toBe('u-missing');
        expect(result.current.getUserDisplayName('u-missing')).toBe('u-missing');
        expect(result.current.getUserRoles('u-missing')).toEqual([]);
    });
});

describe('useGateway — endpoint wiring', () => {
    it('points the advertised LLM endpoint at the proxy, not the admin host', () => {
        const { result } = renderHook(() => useGateway({ gatewayUrl: 'http://control:5050' }));

        expect(result.current.llmProxyUrl).toMatch(/\/v1\/proxy\/llm$/);
        // The connect modal must not advertise the control plane, where
        // this route does not exist.
        expect(result.current.llmProxyUrl).not.toContain('control:5050');
    });

    it('honours an explicitly supplied admin URL', () => {
        const { result } = renderHook(() => useGateway({ gatewayUrl: 'http://control:5050' }));

        expect(result.current.gatewayUrl).toBe('http://control:5050');
    });
});

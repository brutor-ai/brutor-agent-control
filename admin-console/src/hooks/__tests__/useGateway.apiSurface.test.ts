/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useGateway's API surface.
 *
 * Most of the hook is a method per endpoint, so the bug it can have is a boring
 * and damaging one: the wrong client method, or arguments dropped or swapped.
 * The tables below pin the mapping, and for the methods that report failures,
 * that each one still rethrows and raises a banner a surface can filter on
 * (`code` and `context.resource`). An auth failure belongs to the auth layer,
 * so it must not raise a banner as well.
 *
 * The parts with real logic (registry payload clean-up, refresh-after-write,
 * degrade-on-failure lookups) have their own tests at the bottom.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { Mock } from 'vitest';

const mocks = vi.hoisted(() => {
    const fns = new Map<string, ReturnType<typeof vi.fn>>();
    // Any client method a test touches becomes a mock on first use.
    const client = new Proxy(
        {},
        {
            get: (_target, key: string) => {
                if (!fns.has(key)) fns.set(key, vi.fn());
                return fns.get(key);
            },
        },
    );
    return { fns, client, auth: { isAuthenticated: true, isLoading: false, gatewayClient: client } };
});

vi.mock('../useAuth', () => ({ useAuth: () => mocks.auth }));

import { PASS_THROUGH, REPORTING, RESOLVES_EMPTY, describeGatewaySurface, hookFn } from '../../test/gatewaySurface';
import { useGateway } from '../useGateway';

const clientFn = (name: string) => (mocks.client as Record<string, Mock>)[name];

function renderGateway() {
    return renderHook(() => useGateway());
}

beforeEach(() => {
    mocks.fns.clear();
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
    vi.restoreAllMocks();
});

// The tables, and the tests every row of them gets, are in
// `test/gatewaySurface.ts`: the Enterprise hook's rows left this file with
// the hook and are tested the same way in
// `enterprise/src/hooks/__tests__/useEnterpriseGateway.apiSurface.test.ts`.
describeGatewaySurface({ name: 'useGateway', render: renderGateway, clientFn, PASS_THROUGH, REPORTING, RESOLVES_EMPTY });

describe('useGateway refreshes what a write changed', () => {
    it('reloads the admin list after a user write', async () => {
        clientFn('fetchTenantAdminUsers').mockResolvedValue({ admins: [{ id: 'u1', username: 'alice' }] });
        const { result } = renderGateway();

        for (const [write, ...rest] of [
            ['createUser', { username: 'alice' }],
            ['updateUser', 'u1', {}],
            ['deleteUser', 'u1'],
        ] as [string, ...unknown[]][]) {
            clientFn('fetchTenantAdminUsers').mockClear();
            await act(async () => {
                await hookFn(result.current, write)(...rest);
            });
            expect(clientFn('fetchTenantAdminUsers')).toHaveBeenCalledTimes(1);
        }
        expect(result.current.users).toEqual([{ id: 'u1', username: 'alice' }]);
    });
});

describe('useGateway resolves users from the real endpoint', () => {
    it('reads `admins`, which is what the endpoint answers', async () => {
        clientFn('fetchTenantAdminUsers').mockResolvedValue({
            admins: [{ id: 'u-1', username: 'alice', full_name: 'Alice A', roles: ['tenant_admin'] }],
        });
        const { result } = renderGateway();

        await act(async () => {
            await result.current.fetchUsers();
        });

        expect(result.current.getUsernameById('u-1')).toBe('alice');
        expect(result.current.getUserDisplayName('u-1')).toBe('Alice A');
        expect(result.current.getUserRoles('u-1')).toEqual(['tenant_admin']);
    });
});

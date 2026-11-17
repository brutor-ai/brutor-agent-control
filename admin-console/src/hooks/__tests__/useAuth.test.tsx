/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Startup authentication.
 *
 * The behaviour under test is what happens when the startup token check
 * does NOT succeed, because that is where a module like this can fail
 * open: a non-expiry error (control plane unreachable, 5xx, CORS refusal)
 * must never leave a user object behind, least of all a synthetic one with
 * `permissions: ['*']` and `admin_type: 'system'`, which would render every
 * privileged surface to a caller who has just failed to authenticate.
 *
 * A test that only asserted "unauthenticated after a failure" would pass
 * against a failure path that populates a user object and derives
 * `isAuthenticated` from something else. So the assertions below
 * are specific: no user object at all, no wildcard permission, no system
 * admin type, and the stored token gone. Fail closed, provably.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, waitFor, act } from '@testing-library/react';

vi.mock('../../lib/gatewayApiClient', async () => (await import('../../test/auth/authClient')).gatewayApiClientModule());

import { AuthProvider, useAuth } from '../useAuth';
// The console's own slot items: what the Community console provides. The
// system administrator's principal is the Enterprise extension's; its case
// is in `enterprise/src/host/hooks/__tests__/`.
import { BUILTIN_SLOTS } from '../../app/builtinSlots';
import { permissionSetup, renderWithUser } from '../../test/auth/permissionProbe';
import { mocks, seedStoredSession } from '../../test/auth/authClient';
import type { PrincipalSlotItem } from '../../app/slots';

const STORED_KEYS = [
    'gateway_auth_token',
    'gateway_user',
    'gateway_admin_type',
    'gateway_tenant_id',
];

/** Reports exactly what the rest of the console gates its UI on. */
function Probe() {
    const { user, isAuthenticated, isLoading } = useAuth();
    if (isLoading) return <div data-testid="state">loading</div>;
    return (
        <div>
            <span data-testid="state">{isAuthenticated ? 'authenticated' : 'anonymous'}</span>
            <span data-testid="username">{user?.username ?? '-'}</span>
            <span data-testid="admin-type">{user?.admin_type ?? '-'}</span>
            <span data-testid="permissions">{(user?.permissions ?? []).join(',') || '-'}</span>
        </div>
    );
}

function renderAuth() {
    return render(
        <AuthProvider>
            <Probe />
        </AuthProvider>,
    );
}

describe('AuthProvider — startup token check', () => {
    beforeEach(() => {
        localStorage.clear();
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('leaves the caller anonymous when the control plane is unreachable', async () => {
        seedStoredSession();
        mocks.validateToken.mockRejectedValue(new TypeError('Failed to fetch'));

        renderAuth();

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        expect(screen.getByTestId('username')).toHaveTextContent('-');
    });

    it('never fabricates a system admin when the check fails', async () => {
        seedStoredSession();
        mocks.validateToken.mockRejectedValue(new Error('500 Internal Server Error'));

        renderAuth();

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        // The specific shape of a fail-open fallback.
        expect(screen.getByTestId('permissions')).not.toHaveTextContent('*');
        expect(screen.getByTestId('admin-type')).not.toHaveTextContent('system');
        expect(screen.getByTestId('username')).not.toHaveTextContent('dev_admin');
    });

    it('clears the stored session when the check fails', async () => {
        seedStoredSession();
        mocks.validateToken.mockRejectedValue(new Error('CORS failure'));

        renderAuth();

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        for (const key of STORED_KEYS) {
            expect(localStorage.getItem(key)).toBeNull();
        }
    });

    it('treats an expired token the same way — anonymous, storage cleared', async () => {
        seedStoredSession();
        mocks.validateToken.mockRejectedValue(new mocks.TokenExpiredError());

        renderAuth();

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        expect(screen.getByTestId('permissions')).toHaveTextContent('-');
        expect(localStorage.getItem('gateway_auth_token')).toBeNull();
    });

    it('leaves the caller anonymous when the server reports the token invalid', async () => {
        seedStoredSession();
        mocks.validateToken.mockResolvedValue({ valid: false });

        renderAuth();

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        expect(localStorage.getItem('gateway_auth_token')).toBeNull();
    });

    it('does not call the server at all when nothing is stored', async () => {
        renderAuth();

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        expect(mocks.validateToken).not.toHaveBeenCalled();
    });

    it('authenticates and restores the stored admin type on a valid token', async () => {
        seedStoredSession();
        mocks.validateToken.mockResolvedValue({
            valid: true,
            user: {
                id: 'u-1',
                username: 'real_admin',
                email: 'admin@acme.test',
                role_id: 'tenant_admin',
                permissions: ['resourcegroup:read'],
            },
        });

        renderAuth();

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('authenticated'),
        );
        expect(screen.getByTestId('username')).toHaveTextContent('real_admin');
        expect(screen.getByTestId('admin-type')).toHaveTextContent('tenant');
        expect(screen.getByTestId('permissions')).toHaveTextContent('resourcegroup:read');
        expect(localStorage.getItem('gateway_auth_token')).toBe('stored-jwt');
    });
});

/**
 * Session lifecycle, 2FA hand-off, and permission gating.
 *
 * These are the surfaces the console's access control actually rests on, and
 * none of them are exercised by rendering a tab: every other test in the repo
 * `vi.mock`s this provider away. The three failure modes worth pinning:
 *
 *  - A session that has expired but is never noticed. `isAuthenticated` is
 *    `!!user && !isTokenExpired`, so a stale `user` object outlives its token
 *    unless something clears it. Two things do — the client's global
 *    TOKEN_EXPIRED_EVENT and the 60s local `exp` poll — and both are easy to
 *    break without any test noticing, because the UI looks identical until a
 *    request 401s.
 *  - A 2FA challenge that leaks a session. `login` must NOT authenticate when
 *    the server asks for a second factor; only `complete2FALogin` may.
 *  - `hasPermission` returning true too readily. It short-circuits when the
 *    principal signed in as says the administrator holds every permission
 *    (the system administrator's: `system:admin` among its permissions), so
 *    an over-broad answer unhides every privileged surface at once.
 */

/** Builds a JWT whose payload the provider's local `exp` poll can read. */
function jwtExpiringIn(seconds: number): string {
    const payload = { exp: Math.floor(Date.now() / 1000) + seconds };
    return `header.${btoa(JSON.stringify(payload))}.signature`;
}

/**
 * Hands back a function that runs the provider's 60s session check once.
 *
 * Vitest's fake timers deadlock RTL's unmount path here, and the poll is a
 * plain callback anyway — capturing it off `setInterval` tests the same code
 * without pretending to move the clock.
 */
function captureSessionPoll(): () => void {
    let poll: (() => void) | null = null;
    vi.spyOn(globalThis, 'setInterval').mockImplementation((
        fn: () => void,
        ms?: number,
    ) => {
        if (ms === 60_000) poll = fn;
        return 0 as unknown as ReturnType<typeof setInterval>;
    });
    return () => {
        if (!poll) throw new Error('session poll was never scheduled');
        poll();
    };
}

/** Drives login / 2FA / logout from inside the provider. */
function ActionProbe() {
    const { user, isAuthenticated, isLoading, login, complete2FALogin, logout } = useAuth();
    if (isLoading) return <div data-testid="state">loading</div>;
    return (
        <div>
            <span data-testid="state">{isAuthenticated ? 'authenticated' : 'anonymous'}</span>
            <span data-testid="username">{user?.username ?? '-'}</span>
            <span data-testid="tenant">{user?.tenant_id ?? '-'}</span>
            <button
                data-testid="login"
                onClick={() => {
                    login('u', 'p', 'tenant', 'acme').catch((e: Error) => {
                        const el = document.createElement('span');
                        el.setAttribute('data-testid', 'login-error');
                        el.textContent = `${e.message}:${(e as any).temp_token ?? '-'}`;
                        document.body.appendChild(el);
                    });
                }}
            />
            <button
                data-testid="complete-2fa"
                onClick={() =>
                    complete2FALogin(
                        { token: 'second-factor-jwt', admin: { username: 'mfa_admin' }, tenant_id: 'acme' },
                        'tenant',
                    )
                }
            />
            <button data-testid="logout" onClick={() => { void logout(); }} />
        </div>
    );
}

describe('AuthProvider — session lifecycle', () => {
    beforeEach(() => {
        localStorage.clear();
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    async function renderAuthenticated() {
        seedStoredSession();
        mocks.validateToken.mockResolvedValue({
            valid: true,
            user: {
                id: 'u-1',
                username: 'real_admin',
                role_id: 'tenant_admin',
                permissions: ['resourcegroup:read'],
            },
        });
        renderAuth();
        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('authenticated'),
        );
    }

    it('drops the session when the client broadcasts TOKEN_EXPIRED_EVENT', async () => {
        await renderAuthenticated();

        // The client dispatches this from any request that comes back with an
        // expired token, so a session can end on a tab the user is not looking
        // at. Without the listener the console stays visibly "logged in".
        act(() => {
            window.dispatchEvent(new Event('gateway_token_expired'));
        });

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        expect(localStorage.getItem('gateway_auth_token')).toBeNull();
        expect(localStorage.getItem('gateway_user')).toBeNull();
    });

    it('the 60s poll ends a session whose token has expired', async () => {
        const tick = captureSessionPoll();
        await renderAuthenticated();

        // Expiry is local arithmetic on the stored JWT — the point of the poll
        // is that it needs no request, so an idle console does not sit
        // authenticated for an hour after its token died.
        localStorage.setItem('gateway_auth_token', jwtExpiringIn(-60));
        act(() => tick());

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        expect(localStorage.getItem('gateway_auth_token')).toBeNull();
    });

    it('the 60s poll leaves a still-valid token alone', async () => {
        const tick = captureSessionPoll();
        await renderAuthenticated();

        localStorage.setItem('gateway_auth_token', jwtExpiringIn(3600));
        act(() => tick());
        act(() => tick());

        expect(screen.getByTestId('state')).toHaveTextContent('authenticated');
    });

    it('the 60s poll ends a session whose token vanished from storage', async () => {
        const tick = captureSessionPoll();
        await renderAuthenticated();

        // Another tab logging out, or storage cleared under us.
        localStorage.removeItem('gateway_auth_token');
        act(() => tick());

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
    });

    it('the 60s poll fails closed on a token it cannot parse', async () => {
        const tick = captureSessionPoll();
        await renderAuthenticated();

        localStorage.setItem('gateway_auth_token', 'not-a-jwt');
        act(() => tick());

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
    });
});

describe('AuthProvider — 2FA hand-off', () => {
    beforeEach(() => {
        localStorage.clear();
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    function renderActions() {
        return render(
            <AuthProvider>
                <ActionProbe />
            </AuthProvider>,
        );
    }

    it('does not authenticate when the server asks for a second factor', async () => {
        mocks.login.mockResolvedValue({ requires_2fa: true, temp_token: 'tmp-123', tenant_id: 'acme' });
        renderActions();
        await waitFor(() => expect(screen.getByTestId('state')).toBeInTheDocument());

        await act(async () => {
            screen.getByTestId('login').click();
        });

        // A challenge is not a session. The caller gets 2FA_REQUIRED plus the
        // temp token to exchange; nothing is stored and nobody is logged in.
        await waitFor(() =>
            expect(screen.getByTestId('login-error')).toHaveTextContent('2FA_REQUIRED:tmp-123'),
        );
        expect(screen.getByTestId('state')).toHaveTextContent('anonymous');
        expect(localStorage.getItem('gateway_user')).toBeNull();
    });

    it('completes the login once the second factor is accepted', async () => {
        renderActions();
        await waitFor(() => expect(screen.getByTestId('state')).toBeInTheDocument());

        await act(async () => {
            screen.getByTestId('complete-2fa').click();
        });

        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('authenticated'),
        );
        expect(screen.getByTestId('username')).toHaveTextContent('mfa_admin');
        expect(screen.getByTestId('tenant')).toHaveTextContent('acme');
        // The 2FA endpoint returns `token`, the login endpoint `access_token`.
        // The provider normalises so the client is armed either way.
        expect(mocks.setAuthToken).toHaveBeenCalledWith('second-factor-jwt');
        expect(localStorage.getItem('gateway_admin_type')).toBe('tenant');
    });

    it('clears the local session even when the server logout fails', async () => {
        localStorage.setItem('gateway_auth_token', 'stored-jwt');
        localStorage.setItem('gateway_user', JSON.stringify({ username: 'real_admin' }));
        localStorage.setItem('gateway_admin_type', 'tenant');
        localStorage.setItem('gateway_tenant_id', 'acme');
        mocks.validateToken.mockResolvedValue({
            valid: true,
            user: { id: 'u-1', username: 'real_admin', role_id: 'tenant_admin', permissions: [] },
        });
        mocks.logout.mockRejectedValue(new Error('control plane unreachable'));

        renderActions();
        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('authenticated'),
        );

        await act(async () => {
            screen.getByTestId('logout').click();
        });

        // Leaving the token behind because the server call failed would keep a
        // credential on the machine of someone who asked to be logged out.
        await waitFor(() =>
            expect(screen.getByTestId('state')).toHaveTextContent('anonymous'),
        );
        for (const key of STORED_KEYS) {
            expect(localStorage.getItem(key)).toBeNull();
        }
    });
});

describe('AuthProvider — hasPermission', () => {
    permissionSetup();

    it('denies a tenant admin the permission they were not granted', async () => {
        const el = await renderWithUser(
            BUILTIN_SLOTS,
            { id: 'u-1', username: 'ta', role_id: 'tenant_admin', permissions: ['resourcegroup:read'] },
            'tenant:delete',
        );
        expect(el).toHaveTextContent('no');
    });

    it('grants a tenant admin the permission they were granted', async () => {
        const el = await renderWithUser(
            BUILTIN_SLOTS,
            { id: 'u-1', username: 'ta', role_id: 'tenant_admin', permissions: ['resourcegroup:read'] },
            'resourcegroup:read',
        );
        expect(el).toHaveTextContent('yes');
    });

    it('honours an explicit wildcard permission', async () => {
        const el = await renderWithUser(
            BUILTIN_SLOTS,
            { id: 'u-2', username: 'ta', role_id: 'tenant_admin', permissions: ['*'] },
            'tenant:delete',
        );
        expect(el).toHaveTextContent('yes');
    });

    it('denies everything when there is no user', async () => {
        const el = await renderWithUser(BUILTIN_SLOTS, null, 'resourcegroup:read');
        expect(el).toHaveTextContent('no');
    });
});

// What the provider does with a contributed principal (slot `app.principals`),
// with a stand-in for one. Only the principal the administrator is signed in
// as (the one whose `id` is the session's `admin_type`) is asked whether they
// hold every permission. That the system
// administrator's principal answers for `system:admin` among the
// permissions, as the control plane does, is in
// `enterprise/src/host/hooks/__tests__/`.
describe('AuthProvider — hasPermission, with a contributed principal', () => {
    permissionSetup();

    const Nothing = () => null;
    function standIn(
        holdsEveryPermission: PrincipalSlotItem['holdsEveryPermission'],
        id = 'stand-in',
    ): PrincipalSlotItem {
        return {
            id,
            label: 'Stand-in Admin',
            landingTab: 'my-profile',
            holdsEveryPermission,
            signIn: () => Promise.reject(new Error('not used: the session is a stored one')),
            signInForm: { icon: Nothing, hint: '', submit: '', Note: Nothing },
            profile: { scope: '', tenant: '', SecurityNote: Nothing, PasswordNotice: Nothing },
        };
    }

    it('grants whatever is asked when the principal signed in as says the administrator holds every permission', async () => {
        const holdsEveryPermission = vi.fn((admin: { permissions: string[] }) => admin.permissions.includes('stand-in:root'));
        const user = { id: 'u-9', username: 'root', permissions: ['stand-in:root'] };
        const slots = { 'app.principals': [standIn(holdsEveryPermission)] };
        const el = await renderWithUser(slots, user, 'tenant:delete', 'stand-in');
        expect(el).toHaveTextContent('yes');
        // Asked about the signed-in administrator, as the control plane answered it.
        expect(holdsEveryPermission).toHaveBeenCalledWith(expect.objectContaining(user));
    });

    it('falls back to what the session lists when the principal signed in as says no', async () => {
        const holdsEveryPermission = vi.fn(() => false);
        const user = { id: 'u-9', username: 'sa', permissions: ['resourcegroup:read'] };
        const slots = { 'app.principals': [standIn(holdsEveryPermission)] };
        expect(await renderWithUser(slots, user, 'tenant:delete', 'stand-in')).toHaveTextContent('no');
        expect(holdsEveryPermission).toHaveBeenCalled();
        cleanup();
        expect(await renderWithUser(slots, user, 'resourcegroup:read', 'stand-in')).toHaveTextContent('yes');
    });

    it('does not ask a principal the administrator is not signed in as: a tenant session with its root role is not granted everything', async () => {
        // The session lists what the stand-in grants everything for; the
        // session is a tenant administrator's, so the stand-in has no say.
        const holdsEveryPermission = vi.fn((admin: { permissions: string[] }) => admin.permissions.includes('stand-in:root'));
        const user = { id: 'u-9', username: 'ta', permissions: ['resourcegroup:read', 'stand-in:root'] };
        const slots = { 'app.principals': [standIn(holdsEveryPermission)] };
        expect(await renderWithUser(slots, user, 'tenant:delete')).toHaveTextContent('no');
        cleanup();
        expect(await renderWithUser(slots, user, 'resourcegroup:read')).toHaveTextContent('yes');
        expect(holdsEveryPermission).not.toHaveBeenCalled();
    });

    it('asks only the principal signed in as when several are contributed', async () => {
        const mine = vi.fn(() => false);
        const other = vi.fn(() => true);
        const user = { id: 'u-9', username: 'sa', permissions: [] };
        const slots = { 'app.principals': [standIn(other, 'other-stand-in'), standIn(mine)] };
        expect(await renderWithUser(slots, user, 'tenant:delete', 'stand-in')).toHaveTextContent('no');
        expect(mine).toHaveBeenCalled();
        expect(other).not.toHaveBeenCalled();
    });

    it('asks no principal when nobody is signed in', async () => {
        const holdsEveryPermission = vi.fn(() => true);
        const el = await renderWithUser({ 'app.principals': [standIn(holdsEveryPermission)] }, null, 'tenant:delete');
        expect(el).toHaveTextContent('no');
        expect(holdsEveryPermission).not.toHaveBeenCalled();
    });
});

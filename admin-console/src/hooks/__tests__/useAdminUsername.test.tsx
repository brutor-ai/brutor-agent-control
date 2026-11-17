/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useAdminUsername
 *
 * This hook exists for one reason: `useAuth` throws when no provider is
 * mounted, and panels that merely *adapt* to who is looking (defaulting an
 * approver field, greying a re-attest button for the person already on the
 * version) must still render standalone. So the contract has two halves that
 * a naive test would only check one of:
 *
 *   1. with a provider, it reports the real signed-in username — a hook that
 *      always returned null would pass a "does not throw" test while quietly
 *      disabling every self-attribution feature in the console;
 *   2. without a provider, it returns null instead of propagating the throw.
 *
 * It is driven against the real `AuthProvider` (with only the HTTP client
 * mocked) rather than a stubbed `useAuth`, so the null-vs-username decision is
 * made by the same auth state the rest of the console sees.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
    validateToken: vi.fn(),
    setAuthToken: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
}));

vi.mock('../../lib/gatewayApiClient', () => ({
    TOKEN_EXPIRED_EVENT: 'gateway_token_expired',
    TokenExpiredError: class TokenExpiredError extends Error {},
    GatewayApiClient: class {
        setAuthToken = mocks.setAuthToken;
        validateToken = mocks.validateToken;
        login = mocks.login;
        logout = mocks.logout;
    },
}));

import { AuthProvider } from '../useAuth';
import { useAdminUsername } from '../useAdminUsername';

/** Renders exactly what a self-attributing panel would read. */
function Probe() {
    const username = useAdminUsername();
    return <span data-testid="who">{username ?? 'NONE'}</span>;
}

function seedStoredSession() {
    localStorage.setItem('gateway_auth_token', 'stored-jwt');
    localStorage.setItem('gateway_user', JSON.stringify({ username: 'real_admin' }));
    localStorage.setItem('gateway_admin_type', 'tenant');
    localStorage.setItem('gateway_tenant_id', 'acme');
}

describe('useAdminUsername', () => {
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

    it('returns null when no auth provider is mounted, instead of throwing', () => {
        expect(() => render(<Probe />)).not.toThrow();
        expect(screen.getByTestId('who')).toHaveTextContent('NONE');
    });

    it('reports the signed-in username once the session validates', async () => {
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

        render(
            <AuthProvider>
                <Probe />
            </AuthProvider>
        );

        await waitFor(() => expect(screen.getByTestId('who')).toHaveTextContent('real_admin'));
    });

    it('returns null under a provider with no signed-in user', async () => {
        // No stored session — the provider settles anonymous without calling out.
        render(
            <AuthProvider>
                <Probe />
            </AuthProvider>
        );

        await waitFor(() => expect(screen.getByTestId('who')).toHaveTextContent('NONE'));
        expect(mocks.validateToken).not.toHaveBeenCalled();
    });

    it('returns null when the session is rejected, not a stale username', async () => {
        seedStoredSession();
        mocks.validateToken.mockResolvedValue({ valid: false });

        render(
            <AuthProvider>
                <Probe />
            </AuthProvider>
        );

        // The stored blob carried `real_admin`; a hook that read storage rather
        // than auth state would attribute an action to a rejected session.
        await waitFor(() => expect(screen.getByTestId('who')).toHaveTextContent('NONE'));
    });
});

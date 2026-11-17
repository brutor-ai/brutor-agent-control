/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The provider's part of enrolment at sign-in.
 *
 * `requires_2fa_enrollment` is not a session: `login` must leave the caller
 * anonymous with nothing stored and report '2FA_ENROLLMENT_REQUIRED' with the
 * temp token, distinct from '2FA_REQUIRED' (a code is owed) so the form shows
 * the right step. The response that ends the enrolment has the shape of a
 * password sign-in (`access_token`), and `complete2FALogin` installs it.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, cleanup, waitFor, act } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
    validateToken: vi.fn(),
    setAuthToken: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
}));

vi.mock('../../lib/gatewayApiClient', () => ({
    TOKEN_EXPIRED_EVENT: 'gateway_token_expired',
    TokenExpiredError: class extends Error {},
    GatewayApiClient: class {
        setAuthToken = mocks.setAuthToken;
        validateToken = mocks.validateToken;
        login = mocks.login;
        logout = mocks.logout;
    },
}));

import { AuthProvider, useAuth } from '../useAuth';

function Probe() {
    const { user, isAuthenticated, isLoading, login, complete2FALogin } = useAuth();
    if (isLoading) return <div data-testid="state">loading</div>;
    return (
        <div>
            <span data-testid="state">{isAuthenticated ? 'authenticated' : 'anonymous'}</span>
            <span data-testid="username">{user?.username ?? '-'}</span>
            <span data-testid="tenant">{user?.tenant_id ?? '-'}</span>
            <span data-testid="tenant-name">{user?.tenant_display_name ?? '-'}</span>
            <button
                data-testid="login"
                onClick={() => {
                    login('u', 'p', 'tenant', 'acme').catch((e: Error & { temp_token?: string }) => {
                        const el = document.createElement('span');
                        el.setAttribute('data-testid', 'login-error');
                        el.textContent = `${e.message}:${e.temp_token ?? '-'}`;
                        document.body.appendChild(el);
                    });
                }}
            />
            <button
                data-testid="complete"
                onClick={() =>
                    complete2FALogin(
                        {
                            access_token: 'enrolled-jwt',
                            admin: { username: 'new_mfa_admin', totp_enabled: true },
                            tenant_id: 'acme',
                            tenant_display_name: 'Acme Corp',
                            backup_codes: ['A1B2C3D4'],
                        },
                        'tenant',
                    )
                }
            />
        </div>
    );
}

describe('AuthProvider — enrolment at sign-in', () => {
    beforeEach(() => {
        localStorage.clear();
        vi.clearAllMocks();
        document.querySelectorAll('[data-testid="login-error"]').forEach((el) => el.remove());
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    const renderIt = () =>
        render(
            <AuthProvider>
                <Probe />
            </AuthProvider>,
        );

    it('does not authenticate when the server asks for enrolment', async () => {
        mocks.login.mockResolvedValue({
            requires_2fa_enrollment: true,
            temp_token: 'enrol-9',
            tenant_id: 'acme',
            user: { username: 'u', email: 'u@acme.test' },
        });
        renderIt();
        await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('anonymous'));

        act(() => {
            screen.getByTestId('login').click();
        });

        await waitFor(() =>
            expect(screen.getByTestId('login-error')).toHaveTextContent('2FA_ENROLLMENT_REQUIRED:enrol-9'),
        );
        expect(screen.getByTestId('state')).toHaveTextContent('anonymous');
        expect(screen.getByTestId('username')).toHaveTextContent('-');
        expect(localStorage.getItem('gateway_user')).toBeNull();
        expect(localStorage.getItem('gateway_admin_type')).toBeNull();
        expect(localStorage.getItem('gateway_tenant_id')).toBeNull();
        expect(mocks.setAuthToken).not.toHaveBeenCalled();
    });

    it.each([
        ['an enrolment', { requires_2fa_enrollment: true }, '2FA_ENROLLMENT_REQUIRED'],
        ['a code', { requires_2fa: true }, '2FA_REQUIRED'],
    ])('writes the temp token of a sign-in that owes %s nowhere in the browser console', async (_what, answer, reported) => {
        mocks.login.mockResolvedValue({ ...answer, temp_token: 'temp-token-7f3a', tenant_id: 'acme', user: { username: 'u' } });
        renderIt();
        await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('anonymous'));

        act(() => {
            screen.getByTestId('login').click();
        });
        await waitFor(() => expect(screen.getByTestId('login-error')).toHaveTextContent(`${reported}:temp-token-7f3a`));

        // Everything a console call was handed, own properties of errors included.
        const written = [console.error, console.warn, console.log]
            .flatMap((writer) => vi.mocked(writer).mock.calls.flat())
            .map((value: unknown) =>
                value !== null && typeof value === 'object'
                    ? JSON.stringify(Object.fromEntries(Object.getOwnPropertyNames(value).map((key) => [key, (value as Record<string, unknown>)[key]])))
                    : String(value),
            )
            .join('\n');
        expect(written).not.toContain('temp-token-7f3a');
    });

    it('installs the session the enrolment returned', async () => {
        renderIt();
        await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('anonymous'));

        act(() => {
            screen.getByTestId('complete').click();
        });

        await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('authenticated'));
        expect(mocks.setAuthToken).toHaveBeenCalledWith('enrolled-jwt');
        expect(screen.getByTestId('username')).toHaveTextContent('new_mfa_admin');
        expect(screen.getByTestId('tenant')).toHaveTextContent('acme');
        expect(screen.getByTestId('tenant-name')).toHaveTextContent('Acme Corp');
        // The backup codes are shown once by the form; they are not kept.
        expect(localStorage.getItem('gateway_user')).not.toContain('A1B2C3D4');
        expect(localStorage.getItem('gateway_user')).not.toContain('backup_codes');
    });
});

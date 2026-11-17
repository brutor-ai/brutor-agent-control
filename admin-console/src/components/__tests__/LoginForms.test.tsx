/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console's own sign-in form (the hosted tenant's is the Enterprise extension's).
 *
 * Pinned: what login is called with (a system admin carries no tenant), the 2FA
 * branch keyed on '2FA_REQUIRED', that the code goes to /login/2fa with its
 * temp_token and a good response completes the session. Backup codes are 8 hex
 * characters ("A1B2C3D4"): a form that strips non-digits and caps at 6 would
 * lock out an admin who has lost their authenticator.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const { auth, gatewayClient } = vi.hoisted(() => {
    const gatewayClient = { request: vi.fn() };
    const auth: { login: ReturnType<typeof vi.fn>; complete2FALogin: ReturnType<typeof vi.fn>; gatewayClient: unknown } = {
        login: vi.fn(),
        complete2FALogin: vi.fn(),
        gatewayClient,
    };
    return { gatewayClient, auth };
});
vi.mock('../../hooks/useAuth', async () => {
    // The client's own methods, issued through `gatewayClient.request`.
    const { clientOver } = await import('../../test/clientOver');
    auth.gatewayClient = clientOver(gatewayClient.request);
    return { useAuth: () => auth };
});
vi.mock('../BrutorLogo', () => ({ BrutorLogo: () => null }));

import LoginFormAlone from '../LoginForm';
// The console's own slot items (none), and a sign-in item that is nobody's:
// which tenant to sign in to is asked only where something contributes the
// question (slot `app.sign-in`). The real item and the system
// administrator's option are the Enterprise extension's; their cases are
// under `enterprise/`.
import { BUILTIN_SLOTS } from '../../app/builtinSlots';
import { SlotProvider } from '../../app/SlotProvider';
import { FAKE_SIGN_IN_SLOTS } from '../../test/fakeSignIn';
import { lockEnd } from '../../lib/accountLock';
import { accountLocked, LOCKED_UNTIL } from '../../test/accountLocked';

const LoginForm = () => (
    <SlotProvider slots={{ ...BUILTIN_SLOTS, ...FAKE_SIGN_IN_SLOTS }}>
        <LoginFormAlone />
    </SlotProvider>
);

/** The form as the Community console renders it: nothing contributed. */
const CommunityLoginForm = () => (
    <SlotProvider slots={BUILTIN_SLOTS}>
        <LoginFormAlone />
    </SlotProvider>
);

const twoFactorRequired = () => Object.assign(new Error('2FA_REQUIRED'), { temp_token: 'tmp-1' });
const set = (ph: string, value: string) => fireEvent.change(screen.getByPlaceholderText(ph), { target: { value } });

beforeEach(() => {
    vi.clearAllMocks();
    auth.login.mockReset();
    auth.complete2FALogin.mockReset();
    gatewayClient.request.mockReset();
    auth.login.mockResolvedValue(undefined);
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe('LoginForm', () => {
    const signIn = (who = 'Tenant') => fireEvent.click(screen.getByRole('button', { name: new RegExp(`^Sign in as ${who} Admin`) }));
    const enterPassword = () => {
        set('Enter your username', 'admin');
        set('Enter your password', 'secret');
    };

    it('signs a tenant admin in with the trimmed tenant', async () => {
        render(<LoginForm />);
        set('Enter tenant ID', '  acme  ');
        enterPassword();
        signIn();
        await waitFor(() => expect(auth.login).toHaveBeenCalledWith('admin', 'secret', 'tenant', 'acme'));
    });

    it('shows a failure, and typing clears it', async () => {
        auth.login.mockRejectedValue(new Error('Invalid credentials'));
        render(<LoginForm />);
        enterPassword();
        signIn();
        expect(await screen.findByText('Invalid credentials')).toBeInTheDocument();
        set('Enter your password', 'other');
        expect(screen.queryByText('Invalid credentials')).toBeNull();
    });

    it('a failure with no message still says something', async () => {
        auth.login.mockRejectedValue({});
        render(<LoginForm />);
        enterPassword();
        signIn();
        expect(await screen.findByText('Login failed')).toBeInTheDocument();
    });

    it('reveals the password on request', () => {
        render(<LoginForm />);
        const input = screen.getByPlaceholderText('Enter your password');
        expect(input).toHaveAttribute('type', 'password');
        fireEvent.click(screen.getByTitle('Show password'));
        expect(input).toHaveAttribute('type', 'text');
        fireEvent.click(screen.getByTitle('Hide password'));
        expect(input).toHaveAttribute('type', 'password');
    });

    describe('2FA', () => {
        async function reachChallenge() {
            auth.login.mockRejectedValue(twoFactorRequired());
            render(<LoginForm />);
            set('Enter tenant ID', ' acme ');
            enterPassword();
            signIn();
            await screen.findByPlaceholderText('000000');
        }
        const verify = () => screen.getByRole('button', { name: 'Verify and sign in' });

        it('needs at least six characters, and posts the code with its temp token', async () => {
            const resp = { token: 'jwt' };
            gatewayClient.request.mockResolvedValue(resp);
            await reachChallenge();
            expect(verify()).toBeDisabled();

            set('000000', '12 34 56');
            expect(verify()).toBeEnabled();
            fireEvent.click(verify());

            await waitFor(() =>
                expect(gatewayClient.request).toHaveBeenCalledWith('/v1/admin/users/login/2fa', {
                    method: 'POST',
                    body: JSON.stringify({ temp_token: 'tmp-1', code: '123456' }),
                }),
            );
            await waitFor(() => expect(auth.complete2FALogin).toHaveBeenCalledWith(resp, 'tenant', 'acme'));
        });

        it('a response with no token, or a refusal, is reported and does not sign in', async () => {
            gatewayClient.request.mockResolvedValueOnce({});
            await reachChallenge();
            set('000000', '123456');
            fireEvent.click(verify());
            expect(await screen.findByText('2FA verification failed')).toBeInTheDocument();

            gatewayClient.request.mockRejectedValueOnce({});
            fireEvent.click(verify());
            expect(await screen.findByText('Invalid 2FA code')).toBeInTheDocument();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });

        // The second step answers 423 for an account locked or deactivated
        // since the password verified; the body says which (`reason`).
        it('a locked account: says until when in local time, or what an administrator must do', async () => {
            await reachChallenge();
            set('000000', '123456');

            gatewayClient.request.mockRejectedValueOnce(accountLocked('timed_lock'));
            fireEvent.click(verify());
            expect(
                await screen.findByText(`This account is locked until ${lockEnd(LOCKED_UNTIL)}. An administrator can unlock it sooner.`),
            ).toBeInTheDocument();

            gatewayClient.request.mockRejectedValueOnce(accountLocked('lock_without_end', null, 'Locked.'));
            fireEvent.click(verify());
            expect(await screen.findByText('This account is locked. An administrator must unlock it.')).toBeInTheDocument();

            gatewayClient.request.mockRejectedValueOnce(accountLocked('deactivated', null, 'Locked.'));
            fireEvent.click(verify());
            expect(await screen.findByText('This account is deactivated. An administrator must reactivate it.')).toBeInTheDocument();

            gatewayClient.request.mockRejectedValueOnce(accountLocked(undefined, null, 'Locked, somehow.'));
            fireEvent.click(verify());
            expect(await screen.findByText('Locked, somehow.')).toBeInTheDocument();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });

        it('Back returns to the password form', async () => {
            await reachChallenge();
            fireEvent.click(screen.getByRole('button', { name: 'Back to sign in' }));
            expect(screen.getByPlaceholderText('Enter your password')).toBeInTheDocument();
        });
    });
});

describe('LoginForm, nothing contributed (the Community console)', () => {
    const signIn = () => fireEvent.click(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ }));
    const enterPassword = () => {
        set('Enter your username', 'admin');
        set('Enter your password', 'secret');
    };

    it('asks for no tenant, and signs in without naming one', async () => {
        render(<CommunityLoginForm />);
        expect(screen.queryByPlaceholderText('Enter tenant ID')).toBeNull();
        expect(screen.queryByText(/tenant id/i)).toBeNull();
        expect(document.querySelectorAll('input')).toHaveLength(2);
        // With a user name and a password the form can be sent: no tenant is owed.
        const submit = screen.getByRole('button', { name: /^Sign in as Tenant Admin/ });
        expect(submit).toBeDisabled();
        enterPassword();
        expect(submit).toBeEnabled();
        expect(submit).toHaveTextContent(/^Sign in as Tenant Admin$/);
        signIn();
        await waitFor(() => expect(auth.login).toHaveBeenCalledWith('admin', 'secret', 'tenant', undefined));
    });

    it('says nothing of a multi-tenant edition, and has no choice of administrator', () => {
        render(<CommunityLoginForm />);
        expect(document.body.textContent).not.toMatch(/multi-tenant/i);
        expect(document.body.textContent).not.toMatch(/edition/i);
        expect(screen.queryByText('Administrator Type')).toBeNull();
        expect(screen.queryByText('Fake footer line')).toBeNull();
    });

    // There is no stock password: the administrator's is generated for each
    // installation (or chosen by whoever starts the control plane). The form
    // prints no credentials on the local machine either, which is where
    // the tests run and where the Community package is opened.
    it('prints no credentials, on the local machine either', () => {
        expect(['localhost', '127.0.0.1']).toContain(window.location.hostname);
        render(<CommunityLoginForm />);
        expect(document.body.textContent).not.toMatch(/Development Credentials/i);
        expect(document.body.textContent).not.toMatch(/Admin123/);
        expect(document.body.textContent).not.toMatch(/Change these credentials/i);
    });

    it('is headed by the product\'s name and carries it with the version at the foot', () => {
        render(<CommunityLoginForm />);
        expect(screen.getByRole('heading', { name: 'Brutor Agent Control' })).toBeInTheDocument();
        expect(screen.getByText(/^Brutor Agent Control v/)).toBeInTheDocument();
        expect(document.body.textContent).not.toMatch(/AI Control Plane|AI Gateway/);
    });

    it('is headed by the name of the edition whose page it runs in', () => {
        const meta = document.createElement('meta');
        meta.setAttribute('name', 'title');
        meta.setAttribute('content', 'Brutor Agent Control Enterprise Admin Console');
        document.head.appendChild(meta);
        try {
            render(<CommunityLoginForm />);
            expect(screen.getByRole('heading', { name: 'Brutor Agent Control Enterprise' })).toBeInTheDocument();
            expect(screen.getByText(/^Brutor Agent Control Enterprise v/)).toBeInTheDocument();
        } finally {
            meta.remove();
        }
    });

    it('the 2FA code completes the session without a tenant', async () => {
        auth.login.mockRejectedValue(twoFactorRequired());
        const resp = { token: 'jwt' };
        gatewayClient.request.mockResolvedValue(resp);
        render(<CommunityLoginForm />);
        enterPassword();
        signIn();
        await screen.findByPlaceholderText('000000');
        set('000000', '123456');
        fireEvent.click(screen.getByRole('button', { name: 'Verify and sign in' }));
        await waitFor(() => expect(auth.complete2FALogin).toHaveBeenCalledWith(resp, 'tenant', undefined));
    });
});

describe('LoginForm, a sign-in item contributed', () => {
    it('shows the item\'s field with its initial value and its footer line', () => {
        render(<LoginForm />);
        expect(screen.getByPlaceholderText('Enter tenant ID')).toHaveValue('first');
        expect(screen.getByText('Fake footer line')).toBeInTheDocument();
        expect(document.body.textContent).not.toMatch(/Development Credentials/i);
        expect(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ })).toHaveTextContent('(first)');
    });

    it('the tenant is owed: with the field emptied the form cannot be sent', () => {
        render(<LoginForm />);
        set('Enter your username', 'admin');
        set('Enter your password', 'secret');
        expect(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ })).toBeEnabled();
        set('Enter tenant ID', '   ');
        expect(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ })).toBeDisabled();
    });
});

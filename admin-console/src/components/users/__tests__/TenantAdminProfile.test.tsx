/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The signed-in admin's own profile and password.
 *
 * Pinned: the change-password body (the server re-authenticates against
 * `current_password`, and a drop there turns a change into a 422), and that
 * each local guard stops the request.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../test/users/tenantAdminProfile'));
vi.mock('../../../hooks/useGateway', async () => (await shared()).useGatewayModule());
// As the auth provider does for a tenant administrator: signed in as no
// contributed principal (slot `app.principals`). The page for a
// system administrator, whose principal the Enterprise extension contributes,
// is in `enterprise/src/host/components/users/__tests__/TenantAdminProfile.test.tsx`.
// `signedInAs.principal` is null except in the block at the end, which signs
// in as a stand-in principal defined in this file.
const signedInAs = vi.hoisted((): { principal: PrincipalSlotItem | null } => ({ principal: null }));
vi.mock('../../../hooks/useAuth', async () => {
    const { auth } = await shared();
    return {
        useAuth: () => ({ ...auth, principal: signedInAs.principal }),
    };
});
vi.mock('../TwoFactorSetup', async () => (await shared()).twoFactorSetupModule());

import TenantAdminProfile from '../TenantAdminProfile';
import { auth, field, fill, request, row, submit, TENANT_ADMIN, type TestUser } from '../../../test/users/tenantAdminProfile';
import type { PrincipalSlotItem } from '../../../app/slots';
import { GatewayAPIError } from '../../../lib/gatewayApi/base';
import { lockEnd } from '../../../lib/accountLock';
import { accountLocked, LOCKED_UNTIL, SERVER_TEXT } from '../../../test/accountLocked';

describe('TenantAdminProfile', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        auth.user = TENANT_ADMIN;
        signedInAs.principal = null;
        request.mockResolvedValue({ ok: true });
    });

    afterEach(() => {
        cleanup();
    });

    describe('identity', () => {
        it('shows the tenant admin\'s fields and where to edit them', () => {
            render(<TenantAdminProfile />);

            expect(row('Username')).toBe('admin');
            expect(row('Email')).toBe('admin@acme.test');
            expect(row('Full name')).toBe('Ada Admin');
            expect(row('Role')).toBe('Tenant Admin');
            expect(row('Admin type')).toBe('tenant');
            expect(row('Tenant')).toBe('Acme Corp');
            expect(row('Last sign-in')).toBe(new Date('2026-10-01T10:00:00Z').toLocaleString());
            expect(screen.getByText(/edit your own row/)).toBeInTheDocument();
            expect(screen.queryByText(/also a service credential/)).not.toBeInTheDocument();
        });

        it('falls back through role id and tenant id, and dashes what is unknown', () => {
            auth.user = { id: 'adm-2', role_id: 'role-9', tenant_id: 'acme' };
            render(<TenantAdminProfile />);

            expect(row('Role')).toBe('role-9');
            expect(row('Tenant')).toBe('acme');
            expect(row('Email')).toBe('—');
            expect(screen.queryByText('Last sign-in')).not.toBeInTheDocument();
        });

        it('renders its copy, not a raw unicode escape', () => {
            render(<TenantAdminProfile />);
            expect(document.body.textContent).toContain('Changes here don’t affect other admins in the tenant.');
            expect(document.body.textContent).not.toContain('\\u2019');
        });
    });

    describe('change password', () => {
        it('sends the current and new password to the self-service endpoint, then clears the form', async () => {
            render(<TenantAdminProfile />);
            fill('old-password', 'new-password-1', 'new-password-1');
            submit();

            expect(await screen.findByText(/Password updated\. Next sign-in/)).toBeInTheDocument();
            expect(request).toHaveBeenCalledTimes(1);
            expect(request).toHaveBeenCalledWith('/v1/admin/users/me/change-password', {
                method: 'POST',
                body: JSON.stringify({ current_password: 'old-password', new_password: 'new-password-1' }),
            });
            expect(field('Current password')).toHaveValue('');
            expect(field('New password')).toHaveValue('');
            expect(field('Confirm new password')).toHaveValue('');
        });

        it('cannot be pressed until all three fields are filled', () => {
            render(<TenantAdminProfile />);
            const button = screen.getByRole('button', { name: 'Update password' });
            expect(button).toBeDisabled();
            fireEvent.change(field('Current password'), { target: { value: 'x' } });
            fireEvent.change(field('New password'), { target: { value: 'y' } });
            expect(button).toBeDisabled();
            fireEvent.change(field('Confirm new password'), { target: { value: 'y' } });
            expect(button).toBeEnabled();
        });

        it('blocks a missing current password even if the form is submitted another way', async () => {
            render(<TenantAdminProfile />);
            fireEvent.change(field('New password'), { target: { value: 'new-password-1' } });
            fireEvent.change(field('Confirm new password'), { target: { value: 'new-password-1' } });
            const form = field('New password').closest('form');
            if (!form) throw new Error('no form');
            fireEvent.submit(form);

            expect(await screen.findByText('Enter your current password.')).toBeInTheDocument();
            expect(request).not.toHaveBeenCalled();
        });

        it.each([
            ['too short', 'old-password', 'short12', 'short12', 'New password must be at least 8 characters and not blank.'],
            ['blank', 'old-password', '        ', '        ', 'New password must be at least 8 characters and not blank.'],
            ['mismatched', 'old-password', 'new-password-1', 'new-password-2', 'The two new passwords don’t match.'],
            ['unchanged', 'same-password', 'same-password', 'same-password', 'New password must differ from current password.'],
        ])('blocks a %s new password without calling the server', async (_name, current, next, confirm, message) => {
            render(<TenantAdminProfile />);
            fill(current, next, confirm);
            submit();

            expect(await screen.findByText(message)).toBeInTheDocument();
            expect(request).not.toHaveBeenCalled();
        });

        it('accepts exactly eight characters, the server\'s own minimum', async () => {
            render(<TenantAdminProfile />);
            fill('old-password', '12345678', '12345678');
            submit();
            await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
        });

        it('shows the server\'s error and keeps what was typed', async () => {
            request.mockRejectedValue(new Error('Current password is incorrect'));
            render(<TenantAdminProfile />);
            fill('wrong-password', 'new-password-1', 'new-password-1');
            submit();

            expect(await screen.findByText('Current password is incorrect')).toBeInTheDocument();
            expect(field('Current password')).toHaveValue('wrong-password');
            expect(screen.queryByText(/Password updated/)).not.toBeInTheDocument();
        });

        // The control plane locks the account after five wrong current
        // passwords, for 15 minutes; a lock stops new sign-ins only, so the
        // session that is changing its password goes on, and the change is
        // answered 423 with a body that says which case it is (`reason`)
        // and, for a timed lock, when it ends (`locked_until`, UTC). The
        // form says the case, and the end in the reader's own time.
        describe('a refused change, the account being locked (423)', () => {
            async function refusedWith(error: unknown) {
                request.mockRejectedValue(error);
                render(<TenantAdminProfile />);
                fill('wrong-password', 'new-password-1', 'new-password-1');
                submit();
                await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
            }

            it('a timed lock: says until when, in local time, and that an administrator can unlock it sooner', async () => {
                await refusedWith(accountLocked('timed_lock'));

                expect(
                    await screen.findByText(
                        `This account is locked until ${lockEnd(LOCKED_UNTIL)}. An administrator can unlock it sooner.`,
                    ),
                ).toBeInTheDocument();
                // Not the control plane's sentence, whose end is written in UTC.
                expect(screen.queryByText(new RegExp(LOCKED_UNTIL.replace('+', '\\+')))).not.toBeInTheDocument();
                expect(screen.queryByText(/Failed to update password/)).not.toBeInTheDocument();
                expect(screen.queryByText(/Password updated/)).not.toBeInTheDocument();
            });

            it('a lock without an end: an administrator must unlock it', async () => {
                await refusedWith(accountLocked('lock_without_end'));
                expect(await screen.findByText('This account is locked. An administrator must unlock it.')).toBeInTheDocument();
                expect(screen.queryByText(/locked until/)).not.toBeInTheDocument();
            });

            it('a deactivated account: an administrator must reactivate it', async () => {
                // The words of another reason: the text is keyed on `reason`.
                await refusedWith(accountLocked('deactivated', null, SERVER_TEXT.lock_without_end));
                expect(
                    await screen.findByText('This account is deactivated. An administrator must reactivate it.'),
                ).toBeInTheDocument();
                expect(screen.queryByText(/must unlock it/)).not.toBeInTheDocument();
            });

            it("an unknown or missing reason: the control plane's own text", async () => {
                await refusedWith(accountLocked('frozen', null, 'Frozen by the operator.'));
                expect(await screen.findByText('Frozen by the operator.')).toBeInTheDocument();
                cleanup();

                request.mockReset();
                await refusedWith(
                    new GatewayAPIError('Account is locked after too many failed attempts.', 423, undefined, {
                        detail: 'Account is locked after too many failed attempts.',
                    }),
                );
                expect(await screen.findByText('Account is locked after too many failed attempts.')).toBeInTheDocument();
            });
        });

        it('keeps the server\'s own words for a refusal that is not a lock', async () => {
            request.mockRejectedValue(new GatewayAPIError('Current password is incorrect', 401));
            render(<TenantAdminProfile />);
            fill('wrong-password', 'new-password-1', 'new-password-1');
            submit();
            expect(await screen.findByText('Current password is incorrect')).toBeInTheDocument();
            expect(screen.queryByText(/account is locked/i)).not.toBeInTheDocument();
        });

        it('clears an earlier error once a later attempt succeeds', async () => {
            request.mockRejectedValueOnce(new Error('Current password is incorrect'));
            render(<TenantAdminProfile />);
            fill('wrong-password', 'new-password-1', 'new-password-1');
            submit();
            await screen.findByText('Current password is incorrect');

            fill('old-password', 'new-password-1', 'new-password-1');
            submit();

            expect(await screen.findByText(/Password updated/)).toBeInTheDocument();
            expect(screen.queryByText('Current password is incorrect')).not.toBeInTheDocument();
        });

        it('reveals only the password whose eye was pressed', () => {
            render(<TenantAdminProfile />);
            expect(field('New password').type).toBe('password');

            const eye = field('New password').parentElement?.querySelector('button');
            if (!eye) throw new Error('no toggle');
            fireEvent.click(eye);

            expect(field('New password').type).toBe('text');
            expect(field('Current password').type).toBe('password');
            expect(field('Confirm new password').type).toBe('password');
        });

        it('offers no form to a caller with no identity', () => {
            auth.user = null;
            render(<TenantAdminProfile />);
            expect(screen.getByText('Sign in to change your password.')).toBeInTheDocument();
            expect(screen.queryByRole('button', { name: 'Update password' })).not.toBeInTheDocument();
        });

        it.todo(
            'BUG: a wrong current password reads "Authentication failed", not the server\'s "Current password is incorrect". gatewayApiClient.ts:656 builds the 401 error from errorData.message and ignores FastAPI\'s errorData.detail. The fix belongs in the shared client',
        );
    });
});

// What the page does for a contributed principal (slot `app.principals`),
// with a stand-in for one. What the system administrator's principal says is
// the Enterprise extension's: `enterprise/src/host/components/users/__tests__/`.
describe('TenantAdminProfile — signed in as a contributed principal', () => {
    const never = () => Promise.reject(new Error('not used by the profile page'));
    const Nothing = () => null;
    const STAND_IN: PrincipalSlotItem = {
        id: 'stand-in',
        label: 'Stand-in Admin',
        landingTab: 'my-profile',
        holdsEveryPermission: () => false,
        signIn: never,
        signInForm: { icon: Nothing, hint: '', submit: '', Note: Nothing },
        profile: {
            scope: ' of the stand-in kind.',
            tenant: 'Stand-in (no tenant)',
            SecurityNote: () => <span> Stand-in security note.</span>,
            PasswordNotice: () => <div>Stand-in password notice.</div>,
        },
    };
    const STAND_IN_ADMIN: TestUser = {
        id: 'si-1',
        username: 'standin',
        admin_type: 'stand-in',
        tenant_id: 'ignored-tenant',
        tenant_display_name: 'Ignored Tenant',
    };

    beforeEach(() => {
        vi.clearAllMocks();
        auth.user = STAND_IN_ADMIN;
        signedInAs.principal = STAND_IN;
        request.mockResolvedValue({ ok: true });
    });

    afterEach(() => {
        cleanup();
        signedInAs.principal = null;
    });

    it('shows the tenant and the scope the principal names, its password notice, and no pointer to an admin-users row', () => {
        render(<TenantAdminProfile />);

        // The principal's, not the session's tenant.
        expect(row('Tenant')).toBe('Stand-in (no tenant)');
        expect(row('Admin type')).toBe('stand-in');
        expect(document.body.textContent).toContain('Changes here don’t affect other admins of the stand-in kind.');
        expect(document.body.textContent).not.toContain('in the tenant.');
        expect(screen.getByText('Stand-in password notice.')).toBeInTheDocument();
        expect(screen.queryByText(/edit your own row/)).not.toBeInTheDocument();
        // It can still change its password.
        expect(screen.getByRole('button', { name: 'Update password' })).toBeInTheDocument();
    });

    it('after a password change, says the password changed and nothing else', async () => {
        render(<TenantAdminProfile />);
        fill('old-password', 'new-password-1', 'new-password-1');
        submit();
        expect((await screen.findByText(/Password updated/)).textContent).toBe(
            'Password updated. Next sign-in will require the new password.',
        );
    });

    it('the Security sub-tab hosts 2FA with the principal\'s note, and has no note for a tenant administrator', () => {
        const { unmount } = render(<TenantAdminProfile />);
        fireEvent.click(screen.getByRole('button', { name: 'Security' }));
        expect(screen.getByText('two factor setup')).toBeInTheDocument();
        expect(screen.queryByRole('heading', { name: 'Your Profile' })).not.toBeInTheDocument();
        expect(screen.getByText('Stand-in security note.')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Profile' }));
        expect(screen.getByRole('heading', { name: 'Your Profile' })).toBeInTheDocument();
        unmount();

        auth.user = TENANT_ADMIN;
        signedInAs.principal = null;
        render(<TenantAdminProfile />);
        fireEvent.click(screen.getByRole('button', { name: 'Security' }));
        expect(screen.getByText('two factor setup')).toBeInTheDocument();
        expect(screen.queryByText('Stand-in security note.')).not.toBeInTheDocument();
    });
});

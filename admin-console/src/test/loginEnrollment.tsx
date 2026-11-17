/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The cases of a sign-in form whose password verified while the tenant's
 * policy requires two-factor authentication and the administrator has none:
 * the enrolment step. Every sign-in form reaches the same step, so the cases
 * are written once and run per form: the console's own form here, the hosted
 * tenant's form under `enterprise/`.
 *
 * The file that runs them mocks `useAuth` (a `vi.mock` cannot be shared: it
 * is hoisted in the file it is written in) and hands the mocks over.
 */

import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { lockEnd } from '../lib/accountLock';
import { accountLocked, LOCKED_UNTIL } from './accountLocked';

export interface EnrollmentMocks {
    auth: { login: Mock; complete2FALogin: Mock };
    gatewayClient: { request: Mock; startTwoFactorEnrollment: Mock; completeTwoFactorEnrollment: Mock };
}

export interface EnrollmentForm {
    name: string;
    /** The tenant the session is installed for, as the form signs in. */
    tenant: string | undefined;
    /** The form, not yet rendered. */
    form: () => React.ReactElement;
    /** Fill the rendered form in and press its sign-in button. */
    signIn: () => void;
}

const QR = { qr_code: 'data:image/png;base64,QQ==', secret: 'JBSWY3DPEHPK3PXP', provisioning_uri: 'otpauth://totp/x' };
const SESSION = {
    access_token: 'session-jwt',
    admin: { username: 'admin' },
    tenant_id: 'acme',
    backup_codes: ['A1B2C3D4', 'E5F6A7B8'],
};

const enrollmentRequired = () =>
    Object.assign(new Error('2FA_ENROLLMENT_REQUIRED'), { temp_token: 'enrol-1', tenant_display_name: 'Acme Corp' });

export const setField = (ph: string, value: string) => fireEvent.change(screen.getByPlaceholderText(ph), { target: { value } });

/** The mocks as every enrolment case starts: sign-in answers "enrolment owed". */
export function enrollmentSetup({ auth, gatewayClient }: EnrollmentMocks): void {
    beforeEach(() => {
        vi.clearAllMocks();
        auth.login.mockReset();
        auth.complete2FALogin.mockReset();
        gatewayClient.request.mockReset();
        gatewayClient.startTwoFactorEnrollment.mockReset();
        gatewayClient.completeTwoFactorEnrollment.mockReset();
        auth.login.mockRejectedValue(enrollmentRequired());
        gatewayClient.startTwoFactorEnrollment.mockResolvedValue(QR);
        gatewayClient.completeTwoFactorEnrollment.mockResolvedValue(SESSION);
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });
}

/** `<name> — enrolment required at sign-in`, for each form. */
export function describeEnrollmentAtSignIn({ auth, gatewayClient }: EnrollmentMocks, forms: readonly EnrollmentForm[]): void {
    describe.each(forms)('$name — enrolment required at sign-in', ({ form, signIn, tenant }) => {
        const verify = () => screen.getByRole('button', { name: 'Verify' });
        const reach = () => {
            render(form());
            signIn();
        };

        it('shows the enrolment step, says why, and starts setup with the temp token', async () => {
            reach();
            expect(await screen.findByAltText('2FA QR Code')).toHaveAttribute('src', QR.qr_code);
            expect(screen.getByText(QR.secret)).toBeInTheDocument();
            expect(screen.getByText(/requires two-factor authentication/i)).toBeInTheDocument();
            expect(gatewayClient.startTwoFactorEnrollment).toHaveBeenCalledTimes(1);
            expect(gatewayClient.startTwoFactorEnrollment).toHaveBeenCalledWith('enrol-1');
            // No session, and not mistaken for the code challenge of an enrolled admin.
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
            expect(gatewayClient.request).not.toHaveBeenCalled();
            expect(screen.queryByPlaceholderText('Enter your password')).toBeNull();
        });

        it('verifies the code, shows the backup codes once, and only then signs in', async () => {
            reach();
            await screen.findByAltText('2FA QR Code');
            expect(verify()).toBeDisabled();
            setField('000000', '123456');
            fireEvent.click(verify());

            await waitFor(() => expect(gatewayClient.completeTwoFactorEnrollment).toHaveBeenCalledWith('enrol-1', '123456'));
            for (const code of SESSION.backup_codes) {
                expect(await screen.findByText(code)).toBeInTheDocument();
            }
            expect(screen.getByText(/won't be shown again/)).toBeInTheDocument();
            // The session is not installed while the codes are on screen.
            expect(auth.complete2FALogin).not.toHaveBeenCalled();

            fireEvent.click(screen.getByRole('button', { name: /Continue/ }));
            expect(auth.complete2FALogin).toHaveBeenCalledTimes(1);
            expect(auth.complete2FALogin).toHaveBeenCalledWith(SESSION, 'tenant', tenant);
        });

        it('a rejected code is reported, enables nothing here and does not sign in', async () => {
            gatewayClient.completeTwoFactorEnrollment.mockRejectedValue(new Error('Invalid 2FA code'));
            reach();
            await screen.findByAltText('2FA QR Code');
            setField('000000', '000000');
            fireEvent.click(verify());

            expect(await screen.findByText('Invalid 2FA code')).toBeInTheDocument();
            expect(screen.getByAltText('2FA QR Code')).toBeInTheDocument();
            expect(screen.queryByText(/won't be shown again/)).toBeNull();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });

        it('a response without a session is reported and does not sign in', async () => {
            gatewayClient.completeTwoFactorEnrollment.mockResolvedValue({ backup_codes: ['A1B2C3D4'] });
            reach();
            await screen.findByAltText('2FA QR Code');
            setField('000000', '123456');
            fireEvent.click(verify());

            expect(await screen.findByText(/did not complete/i)).toBeInTheDocument();
            expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });

        it('a setup that is refused says so and offers the way back', async () => {
            gatewayClient.startTwoFactorEnrollment.mockRejectedValue(
                new Error('The time to set up two-factor authentication ran out. Please sign in again.'),
            );
            reach();
            expect(await screen.findByText(/ran out. Please sign in again/)).toBeInTheDocument();
            expect(screen.queryByAltText('2FA QR Code')).toBeNull();

            fireEvent.click(screen.getByRole('button', { name: /Back to (sign in|login)/ }));
            expect(screen.getByPlaceholderText('Enter your password')).toBeInTheDocument();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });

        // Both steps answer 423 for an account locked or deactivated since
        // the password verified; the body says which case (`reason`).
        it('a setup refused for a locked account says until when, in local time', async () => {
            gatewayClient.startTwoFactorEnrollment.mockRejectedValue(accountLocked('timed_lock'));
            reach();
            expect(
                await screen.findByText(`This account is locked until ${lockEnd(LOCKED_UNTIL)}. An administrator can unlock it sooner.`),
            ).toBeInTheDocument();
            expect(screen.queryByAltText('2FA QR Code')).toBeNull();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });

        it('a code refused for a deactivated account says an administrator must reactivate it', async () => {
            // The words of another reason: the text is keyed on `reason`.
            gatewayClient.completeTwoFactorEnrollment.mockRejectedValue(
                accountLocked('deactivated', null, 'This account is locked. An administrator must unlock it.'),
            );
            reach();
            await screen.findByAltText('2FA QR Code');
            setField('000000', '123456');
            fireEvent.click(verify());

            expect(await screen.findByText('This account is deactivated. An administrator must reactivate it.')).toBeInTheDocument();
            expect(screen.queryByText(/won't be shown again/)).toBeNull();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });

        it('Back returns to the password form without signing in', async () => {
            reach();
            await screen.findByAltText('2FA QR Code');
            fireEvent.click(screen.getByRole('button', { name: /Back to (sign in|login)/ }));
            expect(screen.getByPlaceholderText('Enter your password')).toBeInTheDocument();
            expect(gatewayClient.completeTwoFactorEnrollment).not.toHaveBeenCalled();
            expect(auth.complete2FALogin).not.toHaveBeenCalled();
        });
    });
}

/** `enrolment step — one setup per temp token`, reached through `form`. */
export function describeOneSetupPerTempToken({ gatewayClient }: EnrollmentMocks, { form, signIn }: EnrollmentForm): void {
    describe('enrolment step — one setup per temp token', () => {
        it('asks for a secret once even when React runs the effect twice', async () => {
            // Every setup call replaces the pending secret on the server. Under
            // StrictMode (development) effects run twice; a second call would
            // invalidate the QR code already on screen.
            render(<StrictMode>{form()}</StrictMode>);
            signIn();

            await screen.findByAltText('2FA QR Code');
            await new Promise((resolve) => setTimeout(resolve, 50));
            expect(gatewayClient.startTwoFactorEnrollment).toHaveBeenCalledTimes(1);
        });
    });
}

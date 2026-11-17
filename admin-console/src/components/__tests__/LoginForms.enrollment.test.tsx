/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Sign-in when the tenant's two-factor policy requires 2FA and the admin has
 * none.
 *
 * The control plane then answers the password with `requires_2fa_enrollment`
 * and a temp token instead of a session; `useAuth.login` reports that as
 * '2FA_ENROLLMENT_REQUIRED'. Pinned for both forms: the enrolment step is
 * shown, the temp token is what opens setup and verify, the backup codes are
 * shown BEFORE the session is installed (installing it unmounts the form, and
 * they are shown once), and nothing signs in on a refusal.
 */

import { vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';

const { auth, gatewayClient } = vi.hoisted(() => {
    const gatewayClient = {
        request: vi.fn(),
        startTwoFactorEnrollment: vi.fn(),
        completeTwoFactorEnrollment: vi.fn(),
    };
    return { gatewayClient, auth: { login: vi.fn(), complete2FALogin: vi.fn(), gatewayClient } };
});
vi.mock('../../hooks/useAuth', () => ({ useAuth: () => auth }));
vi.mock('../BrutorLogo', () => ({ BrutorLogo: () => null }));

import LoginForm from '../LoginForm';
import { SlotProvider } from '../../app/SlotProvider';
import { FAKE_SIGN_IN_SLOTS } from '../../test/fakeSignIn';
import {
    describeEnrollmentAtSignIn,
    describeOneSetupPerTempToken,
    enrollmentSetup,
    setField,
    type EnrollmentForm,
} from '../../test/loginEnrollment';

const password = () => {
    setField('Enter your username', 'admin');
    setField('Enter your password', 'secret');
    fireEvent.click(screen.getByRole('button', { name: /^Sign in as Tenant Admin/ }));
};

/** The console's own form where something asks which tenant (slot `app.sign-in`; a fake item here). */
const WITH_TENANT_FIELD: EnrollmentForm = {
    name: 'LoginForm',
    tenant: 'acme',
    form: () => (
        <SlotProvider slots={FAKE_SIGN_IN_SLOTS}>
            <LoginForm />
        </SlotProvider>
    ),
    signIn: () => {
        setField('Enter tenant ID', ' acme ');
        password();
    },
};

/** The Community console's form: no tenant is asked, and the session is installed for the one the answer names. */
const COMMUNITY: EnrollmentForm = {
    name: 'LoginForm, no tenant asked',
    tenant: undefined,
    form: () => <LoginForm />,
    signIn: password,
};

const mocks = { auth, gatewayClient };
enrollmentSetup(mocks);
describeEnrollmentAtSignIn(mocks, [WITH_TENANT_FIELD, COMMUNITY]);
describeOneSetupPerTempToken(mocks, COMMUNITY);

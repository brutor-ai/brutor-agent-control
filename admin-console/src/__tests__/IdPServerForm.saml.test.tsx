/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * IdPServerForm and SAML, which is not in this release.
 *
 * The form is OIDC only: it offers none of the SAML provider's settings,
 * the email-trust switch among them (the Control Plane neither takes nor
 * shows any of them).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import IdPServerForm from '../components/idp/IdPServerForm';
import { WithBuiltinSlots } from '../test/builtinSlots';
import { createIdPConfig as mockCreate, updateIdPConfig as mockUpdate } from '../test/idp/idpServerForm';

vi.mock('../hooks/useGateway', async () => (await import('../test/idp/idpServerForm')).useGatewayModule());

describe('IdPServerForm — trust_saml_email_unverified toggle', () => {
    beforeEach(() => {
        mockCreate.mockClear();
        mockUpdate.mockClear();
    });

    it('does NOT render the trust-email toggle for OIDC IdPs', () => {
        // The SAML tab — and the toggle inside it — must not appear
        // when protocol is OIDC. The OIDC path uses the standard
        // `email_verified` claim from the userinfo / id_token and
        // doesn't need the operator-driven trust signal.
        render(<IdPServerForm onCancel={() => {}} onSuccess={() => {}} />, { wrapper: WithBuiltinSlots });
        expect(
            screen.queryByText(/Trust this IdP's email claim as verified/i),
        ).not.toBeInTheDocument();
    });
});

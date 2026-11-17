/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The identity provider form and its protocol.
 *
 * OIDC is the console's one protocol: nothing else is offered, and a stored
 * provider of another protocol (SAML, not in this release) is shown locked.
 * These tests pin what the form sends and that it sends nothing of SAML.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import IdPServerForm from '../IdPServerForm';
import type { IdPConfig } from '../../../lib/types';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import {
    CORE_DEFAULTS,
    createIdPConfig,
    expectNotSaved,
    field,
    fillOidc,
    hasField,
    OIDC_ROW,
    openTab,
    refusedByServer,
    resetIdPServerForm,
    SAML_ROW,
    save,
    sent,
    set,
    tabLabels,
    updateIdPConfig,
} from '../../../test/idp/idpServerForm';

vi.mock('../../../hooks/useGateway', async () => (await import('../../../test/idp/idpServerForm')).useGatewayModule());

/** The keys of a save that are SAML's. */
const samlKeys = (payload: Record<string, unknown>) => Object.keys(payload).filter((key) => key.startsWith('saml_') || key === 'target_user_type' || key === 'trust_saml_email_unverified');

beforeEach(resetIdPServerForm);

describe('IdPServerForm: an OIDC provider', () => {
    const renderForm = (idp?: IdPConfig, onCancel: () => void = () => {}) =>
        render(<IdPServerForm idp={idp} isEdit={!!idp} onCancel={onCancel} />);

    it('is an OIDC form with nothing to pick', () => {
        const r = renderForm();
        expect(hasField(r, 'protocol')).toBe(false);
        expect(screen.queryByText(/SAML/)).not.toBeInTheDocument();
        expect(screen.getByText('Configure OIDC / OAuth 2.0 identity provider settings')).toBeInTheDocument();
        expect(tabLabels(r)).toEqual(['Basic Settings', 'Endpoints', 'OAuth Settings', 'Advanced']);
        expect(hasField(r, 'type')).toBe(true);
        expect(hasField(r, 'well_known_url')).toBe(true);
        expect(field(r, 'issuer').placeholder).toBe('https://accounts.google.com');
    });

    it('saves an OIDC provider and sends nothing of SAML', async () => {
        const r = renderForm();
        fillOidc(r);
        save();
        await waitFor(() => expect(createIdPConfig).toHaveBeenCalledTimes(1));
        const payload = sent(createIdPConfig);
        expect(payload).toEqual({
            ...CORE_DEFAULTS,
            name: 'Google',
            issuer: 'https://accounts.google.com',
            protocol: 'oidc',
            authorization_endpoint: 'https://accounts.google.com/auth',
            token_endpoint: 'https://accounts.google.com/token',
        });
        expect(samlKeys(payload)).toEqual([]);
    });

    it('still requires the OIDC endpoints', async () => {
        const r = renderForm();
        set(r, 'name', 'Google');
        set(r, 'issuer', 'https://accounts.google.com');
        save();
        await expectNotSaved();
        openTab('Endpoints');
        expect(screen.getByText('Authorization endpoint is required')).toBeInTheDocument();
        expect(screen.getByText('Token endpoint is required')).toBeInTheDocument();
    });

    it('updates a stored OIDC provider and sends nothing of SAML', async () => {
        renderForm(OIDC_ROW);
        save('Update Provider');
        await waitFor(() => expect(updateIdPConfig).toHaveBeenCalledTimes(1));
        const payload = sent(updateIdPConfig, 1);
        expect(payload).toMatchObject({ name: 'Google', protocol: 'oidc', type: 'oidc', supports_client_credentials: true });
        expect(samlKeys(payload)).toEqual([]);
    });

    it('does not open a stored SAML provider as an OIDC one: it says so and offers no save', async () => {
        const onCancel = vi.fn();
        const r = renderForm(SAML_ROW, onCancel);
        expect(screen.getByTestId('idp-unsupported-protocol')).toHaveTextContent(
            'This identity provider uses the protocol saml, which this Admin Console cannot edit. It has not been changed.',
        );
        expect(r.container.querySelectorAll('[name]')).toHaveLength(0);
        expect(screen.queryByRole('button', { name: 'Update Provider' })).not.toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Create Provider' })).not.toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(onCancel).toHaveBeenCalledTimes(1);
        await expectNotSaved();
    });
});

describe('IdPServerForm and a stored provider of another protocol', () => {
    it('is not opened as OIDC', () => {
        const row = { ...SAML_ROW, protocol: 'ldap' } as IdPConfig;
        render(<IdPServerForm idp={row} isEdit onCancel={() => {}} />, { wrapper: WithBuiltinSlots });
        expect(screen.getByTestId('idp-unsupported-protocol')).toHaveTextContent('uses the protocol ldap');
        expect(screen.queryByRole('button', { name: 'Update Provider' })).not.toBeInTheDocument();
    });
});

describe('IdPServerForm and a save the Control Plane refuses', () => {
    const DETAIL = "protocol=oidc rejects SAML-only fields: ['saml_metadata_url']. Set protocol='saml' to use these.";

    it('shows a 422 in the server\'s words, keeps the form open and reports no success', async () => {
        updateIdPConfig.mockRejectedValueOnce(refusedByServer(422, { detail: DETAIL }));
        const onSuccess = vi.fn();
        const r = render(<IdPServerForm idp={OIDC_ROW} isEdit onSuccess={onSuccess} onCancel={() => {}} />);
        save('Update Provider');
        expect(await screen.findByRole('alert')).toHaveTextContent(DETAIL);
        expect(onSuccess).not.toHaveBeenCalled();
        expect(field(r, 'name').value).toBe('Google');
        // A save that then goes through takes the message away.
        save('Update Provider');
        await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('shows a 422 that names fields as one line per field', async () => {
        createIdPConfig.mockRejectedValueOnce(
            refusedByServer(422, { detail: [{ loc: ['body', 'issuer'], msg: 'Value error, issuer must be a URL', type: 'value_error' }] }),
        );
        const r = render(<IdPServerForm onCancel={() => {}} />);
        fillOidc(r);
        save();
        expect(await screen.findByRole('alert')).toHaveTextContent('issuer: Value error, issuer must be a URL');
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuthAppClientForm registers an OAuth client against an IdP. The payload it
 * builds is a security surface, so these tests pin it directly:
 *
 *  - a public (PKCE) client must never carry a `client_secret`, even if one
 *    was typed before the type was switched;
 *  - editing an existing client with the secret field left blank must omit
 *    the field entirely rather than sending an empty string, which would
 *    overwrite the stored secret with nothing;
 *  - identity-defining fields (app type, IdP, client type) are frozen on
 *    edit, because changing them silently re-points an existing client;
 *  - scopes are typed as a free-text list and must be normalised to an array.
 *
 * A save that rejects must surface the reason and re-enable the form — a
 * stuck "Saving..." with no message is the failure this guards.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

import OAuthAppClientForm from '../OAuthAppClientForm';
import type { OAuthAppClientPayload } from '../OAuthAppClientForm';

const idps = [
    { id: 'idp-1', name: 'Keycloak' },
    { id: 'idp-2', name: 'Okta' },
];

const existing: React.ComponentProps<typeof OAuthAppClientForm>['client'] = {
    id: 'c1',
    name: 'Web App',
    app_type: 'webapp',
    idp_id: 'idp-1',
    client_id: 'web-app',
    redirect_uri: 'http://localhost:3000/callback',
    scopes: ['openid', 'profile'],
    client_type: 'confidential',
    supports_pkce: true,
    allowed_grant_types: ['authorization_code'],
    description: 'Front door',
};

const setup = (client: typeof existing | null = null) => {
    const onSave = vi.fn<(data: OAuthAppClientPayload) => Promise<void>>().mockResolvedValue(undefined);
    const onCancel = vi.fn();
    render(<OAuthAppClientForm client={client} idps={idps} onSave={onSave} onCancel={onCancel} />);
    return { onSave, onCancel };
};

const type = (placeholder: RegExp | string, value: string) =>
    fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });

const select = (name: string, value: string) => {
    const el = document.querySelector(`select[name="${name}"]`) as HTMLSelectElement;
    fireEvent.change(el, { target: { value } });
};

/** Client id and client secret share a placeholder, so address them by name. */
const input = (name: string) => document.querySelector(`input[name="${name}"]`) as HTMLInputElement;
const typeInto = (name: string, value: string) =>
    fireEvent.change(input(name), { target: { value } });

const submit = () => fireEvent.click(screen.getByRole('button', { name: /create client|update client/i }));

const fillNewClient = () => {
    type(/web app client/i, 'CLI Tool');
    select('idp_id', 'idp-1');
    typeInto('client_id', 'cli-tool');
    type(/localhost:3000\/callback/, 'http://localhost:9000/cb');
};

describe('OAuthAppClientForm — create', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(() => cleanup());

    it('offers the configured identity providers', () => {
        setup();
        expect(screen.getByRole('option', { name: 'Keycloak' })).toBeTruthy();
        expect(screen.getByRole('option', { name: 'Okta' })).toBeTruthy();
    });

    it('refuses to submit with required fields blank, and says which', async () => {
        const { onSave } = setup();
        submit();
        expect(await screen.findByText('Please fill in all required fields')).toBeTruthy();
        expect(onSave).not.toHaveBeenCalled();
    });

    it('requires a secret for a confidential client', async () => {
        const { onSave } = setup();
        fillNewClient();
        submit();
        expect(
            await screen.findByText(/Client secret is required for confidential/)
        ).toBeTruthy();
        expect(onSave).not.toHaveBeenCalled();
    });

    it('clears the validation message as soon as the operator edits a field', async () => {
        setup();
        submit();
        await screen.findByText('Please fill in all required fields');
        type(/web app client/i, 'CLI Tool');
        expect(screen.queryByText('Please fill in all required fields')).toBeNull();
    });

    it('normalises the comma-separated scope list into an array', async () => {
        const { onSave } = setup();
        fillNewClient();
        type(/^openid$/, 'openid,  profile , , email');
        typeInto('client_secret', 's3cret');
        submit();

        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        expect(onSave.mock.calls[0][0].scopes).toEqual(['openid', 'profile', 'email']);
    });

    it('hides the secret field for a public client and omits it from the payload', async () => {
        const { onSave } = setup();
        fillNewClient();
        // Type a secret first, then switch to a PKCE-only public client.
        typeInto('client_secret', 'leaked');
        select('client_type', 'public');
        expect(document.querySelector('input[name="client_secret"]')).toBeNull();

        submit();
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        expect(onSave.mock.calls[0][0].client_secret).toBeUndefined();
        expect(onSave.mock.calls[0][0].client_type).toBe('public');
    });

    it('keeps the secret for a public_with_secret client', async () => {
        const { onSave } = setup();
        fillNewClient();
        select('client_type', 'public_with_secret');
        typeInto('client_secret', 'hubspot-secret');
        submit();

        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        expect(onSave.mock.calls[0][0]).toMatchObject({
            client_type: 'public_with_secret',
            client_secret: 'hubspot-secret',
            supports_pkce: true,
        });
    });

    it('sends the full payload the control plane expects', async () => {
        const { onSave } = setup();
        fillNewClient();
        select('app_type', 'cli');
        typeInto('client_secret', 's3cret');
        fireEvent.click(screen.getByLabelText(/enable pkce/i));
        submit();

        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        expect(onSave.mock.calls[0][0]).toEqual({
            name: 'CLI Tool',
            app_type: 'cli',
            idp_id: 'idp-1',
            client_id: 'cli-tool',
            client_secret: 's3cret',
            redirect_uri: 'http://localhost:9000/cb',
            scopes: ['openid', 'profile', 'email'],
            client_type: 'confidential',
            supports_pkce: false,
            allowed_grant_types: ['authorization_code'],
            description: undefined,
        });
    });

    it('surfaces a save failure and leaves the form usable', async () => {
        const onSave = vi.fn<(d: OAuthAppClientPayload) => Promise<void>>()
            .mockRejectedValue(new Error('client_id already registered'));
        render(
            <OAuthAppClientForm client={null} idps={idps} onSave={onSave} onCancel={vi.fn()} />
        );
        fillNewClient();
        typeInto('client_secret', 's3cret');
        submit();

        expect(await screen.findByText('client_id already registered')).toBeTruthy();
        expect(screen.getByRole('button', { name: /create client/i }).hasAttribute('disabled')).toBe(
            false
        );
    });

    it('cancels without saving', () => {
        const { onSave, onCancel } = setup();
        fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
        expect(onCancel).toHaveBeenCalledTimes(1);
        expect(onSave).not.toHaveBeenCalled();
    });
});

describe('OAuthAppClientForm — edit', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(() => cleanup());

    it('hydrates from the existing client, joining its scopes back into text', () => {
        setup(existing);
        expect(screen.getByDisplayValue('Web App')).toBeTruthy();
        expect(screen.getByDisplayValue('web-app')).toBeTruthy();
        expect(screen.getByDisplayValue('openid, profile')).toBeTruthy();
        expect(screen.getByDisplayValue('Front door')).toBeTruthy();
    });

    it('freezes the identity-defining fields', () => {
        setup(existing);
        expect(document.querySelector('select[name="app_type"]')!.hasAttribute('disabled')).toBe(true);
        expect(document.querySelector('select[name="idp_id"]')!.hasAttribute('disabled')).toBe(true);
        expect(document.querySelector('select[name="client_type"]')!.hasAttribute('disabled')).toBe(true);
        expect(screen.getAllByText(/Cannot change/).length).toBeGreaterThan(0);
    });

    it('omits the secret entirely when the field is left blank on edit', async () => {
        const { onSave } = setup(existing);
        submit();

        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        // An empty string here would blank the stored secret.
        expect(onSave.mock.calls[0][0]).not.toHaveProperty('client_secret', '');
        expect(onSave.mock.calls[0][0].client_secret).toBeUndefined();
    });

    it('sends a replacement secret when one is typed', async () => {
        const { onSave } = setup(existing);
        typeInto('client_secret', 'rotated');
        submit();
        await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
        expect(onSave.mock.calls[0][0].client_secret).toBe('rotated');
    });
});

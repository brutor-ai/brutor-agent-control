/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuthDiscoveryDisplay turns a 401 challenge from an upstream MCP server
 * into a one-click IdP registration. The governance-relevant behaviour is
 * the enabled gate: an IdP discovered without an authorization or token
 * endpoint must be imported *disabled*, and the operator must be told why —
 * importing it enabled would leave a half-configured auth server that fails
 * at first use. The already-registered path must suppress the import card
 * entirely so nobody creates a duplicate IdP for the same issuer.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

import OAuthDiscoveryDisplay from '../OAuthDiscoveryDisplay';

type Props = React.ComponentProps<typeof OAuthDiscoveryDisplay>;

const discovery: Props['oauthDiscovery'] = {
    auth_scheme: 'Bearer',
    issuer: 'https://idp.example.com',
    error_description: 'Missing or invalid access token',
};

const suggested = (overrides: Partial<NonNullable<Props['suggestedIdPConfig']>> = {}) => ({
    id: 'idp-new',
    name: 'Example IdP',
    type: 'oidc',
    issuer: 'https://idp.example.com',
    authorization_endpoint: 'https://idp.example.com/authorize',
    token_endpoint: 'https://idp.example.com/token',
    oidc_discovered: true,
    ...overrides,
});

const renderDisplay = (overrides: Partial<Props> = {}) =>
    render(
        <OAuthDiscoveryDisplay
            oauthDiscovery={discovery}
            suggestedIdPConfig={suggested()}
            onImportIdP={vi.fn().mockResolvedValue(undefined)}
            {...overrides}
        />
    );

describe('OAuthDiscoveryDisplay', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(() => cleanup());

    it('reports the challenge the upstream server returned', () => {
        renderDisplay();
        expect(screen.getByText('OAuth Authentication Required')).toBeTruthy();
        expect(screen.getByText('Bearer')).toBeTruthy();
        expect(screen.getByText('Missing or invalid access token')).toBeTruthy();
    });

    it('badges a full OIDC discovery differently from a partial one', () => {
        const { rerender } = renderDisplay();
        expect(screen.getByText('OIDC Auto-configured')).toBeTruthy();

        rerender(
            <OAuthDiscoveryDisplay
                oauthDiscovery={discovery}
                suggestedIdPConfig={suggested({ oidc_discovered: false })}
                onImportIdP={vi.fn()}
            />
        );
        expect(screen.getByText('Partial Discovery')).toBeTruthy();
    });

    it('shows no discovery badge and no endpoint toggle when nothing was found', () => {
        renderDisplay({
            suggestedIdPConfig: suggested({
                oidc_discovered: false,
                authorization_endpoint: undefined,
                token_endpoint: undefined,
            }),
        });
        expect(screen.queryByText('OIDC Auto-configured')).toBeNull();
        expect(screen.queryByText('Partial Discovery')).toBeNull();
        expect(screen.queryByRole('button', { name: /discovered endpoints/i })).toBeNull();
    });

    it('reveals the discovered endpoints on demand', () => {
        renderDisplay();
        expect(screen.queryByText('Discovered OAuth Endpoints')).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: /show discovered endpoints/i }));
        expect(screen.getByText('Discovered OAuth Endpoints')).toBeTruthy();
        expect(screen.getByText('https://idp.example.com/authorize')).toBeTruthy();
        expect(screen.getByText('https://idp.example.com/token')).toBeTruthy();

        fireEvent.click(screen.getByRole('button', { name: /hide discovered endpoints/i }));
        expect(screen.queryByText('Discovered OAuth Endpoints')).toBeNull();
    });

    it('imports the suggested IdP enabled when both required endpoints are present', async () => {
        const onImportIdP = vi.fn().mockResolvedValue(undefined);
        renderDisplay({ onImportIdP });

        fireEvent.click(screen.getByRole('button', { name: /import identity provider/i }));
        await waitFor(() => expect(onImportIdP).toHaveBeenCalledTimes(1));
        expect(onImportIdP.mock.calls[0][0]).toMatchObject({
            id: 'idp-new',
            issuer: 'https://idp.example.com',
            enabled: true,
        });
        expect(await screen.findByText('Identity Provider Created')).toBeTruthy();
    });

    it('warns about, and disables, an IdP missing a required endpoint', async () => {
        const onImportIdP = vi.fn().mockResolvedValue(undefined);
        renderDisplay({
            suggestedIdPConfig: suggested({ token_endpoint: undefined, oidc_discovered: false }),
            onImportIdP,
        });

        expect(screen.getByText('Incomplete Configuration Detected')).toBeTruthy();
        expect(screen.getAllByText('Token Endpoint').length).toBeGreaterThan(0);

        fireEvent.click(screen.getByRole('button', { name: /import identity provider/i }));
        await waitFor(() => expect(onImportIdP).toHaveBeenCalledTimes(1));
        // A half-configured auth server must not go live.
        expect(onImportIdP.mock.calls[0][0]).toMatchObject({ enabled: false });
        expect(await screen.findByText(/must configure the following endpoints/i)).toBeTruthy();
    });

    it('does not warn when the configuration is complete', () => {
        renderDisplay();
        expect(screen.queryByText('Incomplete Configuration Detected')).toBeNull();
    });

    it('surfaces an import failure without claiming success', async () => {
        const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
        renderDisplay({ onImportIdP: vi.fn().mockRejectedValue(new Error('IdP store offline')) });

        fireEvent.click(screen.getByRole('button', { name: /import identity provider/i }));
        expect(await screen.findByText('Import Failed')).toBeTruthy();
        expect(screen.getByText('IdP store offline')).toBeTruthy();
        expect(screen.queryByText('Successfully imported')).toBeNull();
        errorSpy.mockRestore();
    });

    it('treats an "already exists" rejection as an existing IdP, not a failure to retry', async () => {
        const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
        renderDisplay({
            onImportIdP: vi.fn().mockRejectedValue(new Error('IdP already exists for issuer')),
        });

        fireEvent.click(screen.getByRole('button', { name: /import identity provider/i }));
        expect(await screen.findByText('This Identity Provider already exists')).toBeTruthy();
        // The import button is gone — retrying would only 409 again.
        expect(screen.queryByRole('button', { name: /import identity provider/i })).toBeNull();
        errorSpy.mockRestore();
    });

    it('honours caller-driven importing state', () => {
        renderDisplay({ importing: true });
        expect(screen.getByText('Importing IdP...')).toBeTruthy();
        expect(
            screen.getByRole('button', { name: /importing idp/i }).hasAttribute('disabled')
        ).toBe(true);
    });

    it('suppresses the import card entirely when the issuer is already registered', () => {
        renderDisplay({
            existingIdP: {
                id: 'idp-1',
                name: 'Keycloak',
                enabled: true,
                issuer: 'https://idp.example.com',
            },
        });
        expect(screen.getByText('Identity Provider Already Registered')).toBeTruthy();
        expect(screen.getByText('Enabled')).toBeTruthy();
        expect(screen.queryByRole('button', { name: /import identity provider/i })).toBeNull();
    });

    it('shows a registered-but-disabled IdP as disabled', () => {
        renderDisplay({
            existingIdP: {
                id: 'idp-1',
                name: 'Keycloak',
                enabled: false,
                issuer: 'https://idp.example.com',
            },
        });
        expect(screen.getByText('Disabled')).toBeTruthy();
    });

    it('offers no import affordance when the caller supplies no handler', () => {
        renderDisplay({ onImportIdP: undefined });
        expect(screen.queryByRole('button', { name: /import identity provider/i })).toBeNull();
    });
});

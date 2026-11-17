/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Identity Providers list.
 *
 * Pinned: which id each action targets, that delete is confirmed by name and
 * reports a refusal inline, that a test/discovery the server answered with
 * "failed" is reported as failure (a 200 with connected:false is not a success),
 * and that the form is seeded from the full record rather than the list row.
 * The form has its own concerns and is stubbed to its callbacks.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../test/idp/idpServersTab'));
vi.mock('../../../hooks/useGateway', async () => (await shared()).useGatewayModule());
vi.mock('../../../components/common/Toast', async () => (await shared()).toastModule());
vi.mock('../IdPServerForm', async () => (await shared()).formModule());

import IdPServersTab from '../IdPServersTab';
// The list with the console's built-in slot items. A stored SAML provider is
// locked: `IdPServersTab.unavailableProtocol.test`.
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import { gateway, resetIdPServersTab, rowOf, toast } from '../../../test/idp/idpServersTab';
import { thrownByClient } from '../../../test/serverAnswers';

async function open() {
    render(<IdPServersTab />, { wrapper: WithBuiltinSlots });
    await screen.findByText('Okta');
}

describe('IdPServersTab', () => {
    beforeEach(resetIdPServersTab);

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('lists providers, shortening only endpoints under the issuer', async () => {
        await open();
        const okta = within(rowOf('Okta'));
        expect(okta.getByText('/authorize')).toBeInTheDocument();
        expect(okta.getByText('https://other.test/token')).toBeInTheDocument();
        expect(okta.getByText('OIDC')).toBeInTheDocument();
        expect(okta.getByText('azure ad')).toBeInTheDocument();
        expect(okta.getByText('PKCE Support')).toBeInTheDocument();
        expect(okta.getByText('Enabled')).toBeInTheDocument();

        // A stored SAML provider: its protocol by its own name, locked, nothing of SAML offered.
        const adfs = within(rowOf('ADFS'));
        expect(adfs.getByText('saml')).toBeInTheDocument();
        expect(adfs.getByTestId('idp-protocol-unavailable')).toBeInTheDocument();
        expect(adfs.queryByText('SAML 2.0')).toBeNull();
        expect(adfs.queryByText('synced')).toBeNull();
        expect(adfs.getByText('Disabled')).toBeInTheDocument();
        expect(screen.queryByText(/SAML 2\.0/)).toBeNull();
    });

    it('filters by search and status, and says so when nothing matches', async () => {
        await open();
        const search = screen.getByPlaceholderText('Search by name, ID, or issuer...');
        fireEvent.change(search, { target: { value: 'OKTA.TEST' } });
        expect(screen.getByText('Okta')).toBeInTheDocument();
        expect(screen.queryByText('ADFS')).toBeNull();
        fireEvent.change(search, { target: { value: '' } });

        fireEvent.change(screen.getByRole('combobox'), { target: { value: 'disabled' } });
        expect(screen.queryByText('Okta')).toBeNull();
        expect(screen.getByText('ADFS')).toBeInTheDocument();

        fireEvent.change(screen.getByRole('combobox'), { target: { value: 'enabled' } });
        expect(screen.queryByText('ADFS')).toBeNull();

        fireEvent.change(search, { target: { value: 'nothing' } });
        expect(screen.getByText('No Identity Providers match your filters')).toBeInTheDocument();
    });

    it('offers the first-provider path when empty, and creates through the form', async () => {
        gateway.fetchIdPConfigs.mockResolvedValue({ idps: {} });
        render(<IdPServersTab />, { wrapper: WithBuiltinSlots });
        expect(await screen.findByText('No Identity Providers configured')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Add First Identity Provider' }));
        expect(screen.getByTestId('form-mode')).toHaveTextContent('create');
        expect(screen.getByTestId('form-idp')).toHaveTextContent('none');

        fireEvent.click(screen.getByText('form-cancel'));
        expect(screen.queryByTestId('form')).toBeNull();
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(1);
    });

    it('edits from the full record and reloads after saving', async () => {
        await open();
        fireEvent.click(within(rowOf('Okta')).getByTitle('Edit'));
        await waitFor(() => expect(gateway.fetchIdPConfig).toHaveBeenCalledWith('okta'));
        expect(await screen.findByTestId('form-idp')).toHaveTextContent('full-record');
        expect(screen.getByTestId('form-mode')).toHaveTextContent('edit');

        fireEvent.click(screen.getByText('form-save'));
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith(
                'Identity Provider Saved',
                'Identity Provider configuration has been saved successfully.',
            ),
        );
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(2);
        expect(screen.queryByTestId('form')).toBeNull();
    });

    it('does not open the form if the full record cannot be loaded', async () => {
        await open();
        gateway.fetchIdPConfig.mockRejectedValue(new Error('gone'));
        fireEvent.click(within(rowOf('Okta')).getByTitle('Edit'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to Load IdP', 'gone'));
        expect(screen.queryByTestId('form')).toBeNull();
    });

    it('reports the connection test as the server answered it', async () => {
        await open();
        gateway.testExistingIdP.mockResolvedValueOnce({ connected: true });
        fireEvent.click(within(rowOf('Okta')).getByTitle('Test Connection'));
        await waitFor(() => expect(gateway.testExistingIdP).toHaveBeenCalledWith('okta'));
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith(
            'Connection Test Successful',
            'Identity Provider is reachable and responding.',
        ));

        // A 200 saying "not connected" is a failure.
        gateway.testExistingIdP.mockResolvedValueOnce({ connected: false, error: 'TLS handshake' });
        fireEvent.click(within(rowOf('Okta')).getByTitle('Test Connection'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Connection Test Failed', 'TLS handshake'));

        gateway.testExistingIdP.mockRejectedValueOnce(new Error('500'));
        await waitFor(() => expect(within(rowOf('Okta')).getByTitle('Test Connection')).toBeEnabled());
        fireEvent.click(within(rowOf('Okta')).getByTitle('Test Connection'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Connection Test Failed', '500'));
    });

    it('shows details and re-discovers endpoints against the selected provider', async () => {
        await open();
        fireEvent.click(within(rowOf('Okta')).getByTitle('View Details'));
        expect(await screen.findByText('Identity Provider Details')).toBeInTheDocument();
        expect(screen.getByText('https://okta.test/jwks')).toBeInTheDocument();

        gateway.discoverIdPEndpoints.mockResolvedValueOnce({ discovery_successful: true });
        fireEvent.click(screen.getByRole('button', { name: 'Discover Endpoints' }));
        await waitFor(() => expect(gateway.discoverIdPEndpoints).toHaveBeenCalledWith('okta', true));
        await waitFor(() => expect(toast.success).toHaveBeenCalledWith(
            'Discovery Successful',
            'OAuth endpoints have been discovered and updated.',
        ));
        // The panel and the table both refresh.
        await waitFor(() => expect(gateway.fetchIdPConfig).toHaveBeenCalledTimes(2));
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(2);

        gateway.discoverIdPEndpoints.mockResolvedValueOnce({ discovery_successful: false, error: 'no well-known' });
        fireEvent.click(await screen.findByRole('button', { name: 'Discover Endpoints' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Discovery Failed', 'no well-known'));

        gateway.discoverIdPEndpoints.mockRejectedValueOnce(new Error('timeout'));
        fireEvent.click(await screen.findByRole('button', { name: 'Discover Endpoints' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Discovery Failed', 'timeout'));

        fireEvent.click(await screen.findByRole('button', { name: 'Close' }));
        expect(screen.queryByText('Identity Provider Details')).toBeNull();
    });

    it('surfaces load failures for the list and for details', async () => {
        gateway.fetchIdPConfigs.mockRejectedValueOnce(new Error('cp down'));
        render(<IdPServersTab />, { wrapper: WithBuiltinSlots });
        expect(await screen.findByText('cp down')).toBeInTheDocument();
        expect(toast.error).toHaveBeenCalledWith('Failed to Load IdPs', 'cp down');
        cleanup();

        await open();
        gateway.fetchIdPConfig.mockRejectedValueOnce(new Error('no such idp'));
        fireEvent.click(within(rowOf('Okta')).getByTitle('View Details'));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to Load IdP Details', 'no such idp'));
        expect(screen.queryByText('Identity Provider Details')).toBeNull();
    });

    it('Refresh reloads the list', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
        await waitFor(() => expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(2));
    });
});

describe('IdPServersTab: deleting an OIDC provider', () => {
    beforeEach(resetIdPServersTab);
    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    async function openList() {
        render(<IdPServersTab />);
        await screen.findByText('Okta');
    }

    it('deletes only after typing the name, then reloads', async () => {
        gateway.deleteIdPConfig.mockResolvedValue({});
        await openList();
        fireEvent.click(within(rowOf('Okta')).getByTitle('Delete'));
        const input = await screen.findByPlaceholderText('Enter "Okta"');
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(gateway.deleteIdPConfig).not.toHaveBeenCalled();
        fireEvent.change(input, { target: { value: 'Okta' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(gateway.deleteIdPConfig).toHaveBeenCalledWith('okta'));
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith('Identity Provider Deleted', 'Okta has been deleted successfully.'),
        );
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(2);
        expect(screen.queryByPlaceholderText('Enter "Okta"')).toBeNull();
    });

    it('shows a refused delete inline and keeps the dialog open', async () => {
        gateway.deleteIdPConfig.mockRejectedValue({ detail: 'still used by 2 MCP servers' });
        await openList();
        fireEvent.click(within(rowOf('Okta')).getByTitle('Delete'));
        const input = await screen.findByPlaceholderText('Enter "Okta"');
        fireEvent.change(input, { target: { value: 'Okta' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(await screen.findByText('still used by 2 MCP servers')).toBeInTheDocument();
        expect(screen.getByPlaceholderText('Enter "Okta"')).toBeInTheDocument();
        expect(toast.success).not.toHaveBeenCalled();
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledTimes(1);
    });

    it('a delete refused because the provider is in use names what uses it, as the Control Plane words it', async () => {
        const message =
            "Identity Provider 'Okta' is in use and is not deleted: OAuth app clients (and the access configurations built on them): Billing app; " +
            'MCP servers: Jira, Wiki; agent identities: nightly-agent. Remove or re-point these first.';
        gateway.deleteIdPConfig.mockRejectedValue(
            await thrownByClient(409, {
                detail: {
                    message,
                    used_by: {
                        oauth_app_clients: [{ id: 'c1', name: 'Billing app' }],
                        mcp_servers: [{ id: 'm1', name: 'Jira' }, { id: 'm2', name: 'Wiki' }],
                        agent_identities: [{ id: 'a1', name: 'nightly-agent' }],
                    },
                },
            }),
        );
        await openList();
        fireEvent.click(within(rowOf('Okta')).getByTitle('Delete'));
        const input = await screen.findByPlaceholderText('Enter "Okta"');
        fireEvent.change(input, { target: { value: 'Okta' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(await screen.findByText(message)).toBeInTheDocument();
        const listed = screen.getByTestId('in-use-list');
        expect(within(listed).getByText('OAuth app clients')).toBeInTheDocument();
        expect(within(listed).getByText('Billing app')).toBeInTheDocument();
        expect(within(listed).getByText('MCP servers')).toBeInTheDocument();
        expect(within(listed).getByText('Wiki')).toBeInTheDocument();
        expect(within(listed).getByText('Agent identities')).toBeInTheDocument();
        expect(within(listed).getByText('nightly-agent')).toBeInTheDocument();
        expect(screen.getByPlaceholderText('Enter "Okta"')).toBeInTheDocument();
        expect(toast.success).not.toHaveBeenCalled();
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP server form and what the Control Plane states of a stored
 * OAuth-protected server (`oauth_pairing`): whether the gateway exchanges
 * an authorization code for it, for which identity providers, and the
 * Control Plane's own warning when there is none. The form shows that
 * statement; for a server that is not saved yet there is none, and the form
 * says what it knows itself.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../../../test/mcp/serverForm'));
vi.mock('../../../../../hooks/useGateway.ts', async () => (await shared()).useGatewayModule());
vi.mock('../../../../common/Toast.tsx', async () => (await shared()).toastModule());
vi.mock('../ServerIcon.tsx', async () => (await shared()).blankModule());
vi.mock('../oauth/OAuthDiscoveryDisplay.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../groups/subtabs/MCPLimitsForm.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../groups/subtabs/MCPGovernanceForm.tsx', async () => (await shared()).blankModule());
vi.mock('../../../../common/AuditInfoTab', async () => (await shared()).blankModule());

import ServerForm from '../ServerForm';
import type { MCPServerConfig } from '../../../../../lib/types.ts';
import { resetServerForm } from '../../../../../test/mcp/serverForm';

const WARNING =
    'No identity provider is paired with this OAuth-protected server: the gateway refuses the sign-in (403 "Identity Provider is not configured for this MCP server") until the server names an identity provider, or a server configuration of it attaches an active OAuth client.';

const stored = (over: Partial<MCPServerConfig> = {}): MCPServerConfig => ({
    id: 'srv-1',
    name: 'Payments',
    description: '',
    base_url: 'https://mcp.example.com',
    mcp_endpoint_path: '/mcp',
    auth_type: 'oauth',
    timeout: 30,
    enabled: true,
    ...over,
});

const renderForm = (server: MCPServerConfig | null) => render(<ServerForm server={server} onSave={vi.fn()} onCancel={vi.fn()} />);

beforeEach(resetServerForm);
afterEach(cleanup);

describe('ServerForm: the pairing of a stored OAuth-protected server', () => {
    it('shows the Control Plane\'s warning when no identity provider is paired', () => {
        renderForm(stored({ oauth_pairing: { paired: false, idp_ids: [], warning: WARNING } }));
        const shown = screen.getByTestId('server-idp-pairing');
        expect(shown).toHaveAttribute('data-state', 'not_paired');
        expect(shown).toHaveTextContent(WARNING);
        // The Control Plane's words, not the console's own beside them.
        expect(screen.queryByText(/This MCP server names no identity provider\. The gateway accepts/)).toBeNull();
    });

    it('says a server that names no identity provider is paired through an OAuth client, with the identity providers', () => {
        renderForm(stored({ oauth_pairing: { paired: true, idp_ids: ['idp-b'], warning: null } }));
        const shown = screen.getByTestId('server-idp-pairing');
        expect(shown).toHaveAttribute('data-state', 'paired');
        expect(shown).toHaveTextContent(
            'This MCP server names no identity provider of its own. It is paired through an active OAuth client of a server configuration: the gateway accepts OAuth sign-ins for this server from idp-b.',
        );
        expect(shown).not.toHaveTextContent('refuses');
    });

    it('names every paired identity provider of a server that names one, the server\'s own by its name', () => {
        renderForm(
            stored({
                idp_id: 'idp-a',
                idp_name: 'Acme SSO',
                oauth_pairing: { paired: true, idp_ids: ['idp-a', 'idp-b'], warning: null },
            }),
        );
        const shown = screen.getByTestId('server-idp-pairing');
        expect(shown).toHaveAttribute('data-state', 'paired');
        expect(shown).toHaveTextContent('The gateway accepts OAuth sign-ins for this server from Acme SSO and idp-b.');
    });

    it('shows the warning of a server whose own identity provider does not pair it', () => {
        // The server names an identity provider the tenant does not have.
        renderForm(stored({ idp_id: 'idp-gone', idp_name: 'Old SSO', oauth_pairing: { paired: false, idp_ids: [], warning: WARNING } }));
        const shown = screen.getByTestId('server-idp-pairing');
        expect(shown).toHaveAttribute('data-state', 'not_paired');
        expect(shown).toHaveTextContent(WARNING);
        expect(shown).not.toHaveTextContent('accepts OAuth sign-ins for this server from it');
    });

    it('says what it knows itself of a server the Control Plane has stated nothing about', () => {
        renderForm(stored());
        const shown = screen.getByTestId('server-idp-pairing');
        expect(shown).toHaveAttribute('data-state', 'not_paired');
        expect(shown).toHaveTextContent(
            'This MCP server names no identity provider. The gateway accepts OAuth sign-ins for it only from the identity provider of an active OAuth client attached to it in a server configuration, and refuses any other.',
        );
    });
});

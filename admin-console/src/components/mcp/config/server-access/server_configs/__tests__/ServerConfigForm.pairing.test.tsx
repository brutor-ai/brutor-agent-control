/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The server configuration form says whether its MCP server is paired with
 * an identity provider.
 *
 * The gateway accepts an OAuth sign-in for a server only from an identity
 * provider configured for it: the server's own, or the one of an active
 * OAuth client attached to it in a server configuration. The form states
 * which holds for what is chosen. It refuses nothing on it.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

vi.mock('../../../../../../hooks/useGateway', () => ({
    useGateway: () => ({ getServerIconUrl: () => '' }),
}));

import ServerConfigForm from '../ServerConfigForm';

afterEach(cleanup);

const client = (overrides: Record<string, unknown> = {}) => ({ id: 'cl-1', name: 'Web app', app_type: 'webapp', idp_id: 'idp-b', is_active: true, ...overrides });

function open(server: Record<string, unknown>, clients: Array<Record<string, unknown>>, oauthClientId: string | null) {
    render(
        <ServerConfigForm
            config={{ id: 'cfg-1', name: 'Default', mcp_server_id: 's-1', oauth_client_id: oauthClientId, group_ids: [], priority: 100, enabled: true } as never}
            servers={[{ id: 's-1', name: 'GitHub', auth_type: 'oauth', ...server }] as never}
            oauthClients={clients as never}
            groupTree={[] as never}
            onSave={vi.fn()}
            onCancel={vi.fn()}
        />,
    );
    return screen.getByTestId('server-idp-pairing');
}

describe('ServerConfigForm: the pairing of the server with an identity provider', () => {
    it('is through the server\'s own identity provider', () => {
        const pairing = open({ idp_id: 'idp-a', idp_name: 'Acme SSO' }, [client({ idp_id: 'idp-a' })], 'cl-1');
        expect(pairing).toHaveAttribute('data-state', 'paired');
        expect(pairing).toHaveTextContent('Paired with identity provider Acme SSO: it is the identity provider set on the MCP server, and of the OAuth client Web app.');
    });

    it('is through the chosen OAuth client when the server names none', () => {
        const pairing = open({}, [client()], 'cl-1');
        expect(pairing).toHaveAttribute('data-state', 'paired');
        expect(pairing).toHaveTextContent('Paired with identity provider idp-b through the OAuth client Web app.');
    });

    it('is none when the server names no identity provider and no client is chosen, and the form still opens', () => {
        const pairing = open({}, [client()], null);
        expect(pairing).toHaveAttribute('data-state', 'not_paired');
        expect(pairing).toHaveTextContent('Not paired with an identity provider: the MCP server names none and no OAuth client is chosen. The gateway refuses OAuth sign-ins for this server.');
    });
});

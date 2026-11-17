/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ServerRow is one line of the MCP server table, and the column an operator
 * actually copies out of is the endpoint URL. That URL is *composed*:
 * `base_url` + `mcp_endpoint_path`, the two the server row carries. Showing the base
 * without the path (or a doubled slash) sends the operator to a URL that
 * 404s, so the composition is pinned in several shapes.
 *
 * The rest is disclosure: an OAuth server with no IdP linked can never
 * complete a login, so the row must say so; and the destructive actions live
 * behind a portal-rendered menu, which must actually be reachable.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

vi.mock('../ServerIcon.tsx', () => ({
    default: ({ serverName }: { serverName: string }) => <span>icon:{serverName}</span>,
}));

import ServerRow from '../ServerRow';

type Props = React.ComponentProps<typeof ServerRow>;

const server = (overrides: Partial<Props['server']> = {}): Props['server'] => ({
    id: 'github-mcp',
    name: 'GitHub MCP',
    base_url: 'http://localhost:9001',
    mcp_endpoint_path: '/mcp',
    auth_type: 'none',
    timeout: 30,
    enabled: true,
    ...overrides,
});

const renderRow = (overrides: Partial<Props> = {}) => {
    const props: Props = {
        server: server(),
        isSelected: false,
        loading: false,
        onSelect: vi.fn(),
        onToggle: vi.fn(),
        onEdit: vi.fn(),
        onDelete: vi.fn(),
        gatewayBaseUrl: 'http://localhost:8100',
        ...overrides,
    };
    const view = render(
        <table>
            <tbody>
                <ServerRow {...props} />
            </tbody>
        </table>
    );
    /** Re-render the row with a patched server, keeping the same handlers. */
    const update = (patch: Partial<Props['server']>) =>
        view.rerender(
            <table>
                <tbody>
                    <ServerRow {...props} server={{ ...props.server, ...patch }} />
                </tbody>
            </table>
        );
    return { ...props, update };
};

const openMenu = () => {
    const buttons = screen.getAllByRole('button');
    fireEvent.click(buttons[buttons.length - 1]);
};

describe('ServerRow — endpoint URL composition', () => {
    afterEach(() => cleanup());

    it('joins the base URL and the MCP endpoint path', () => {
        renderRow();
        expect(screen.getByText('http://localhost:9001/mcp')).toBeTruthy();
    });

    it('does not double the slash when the base URL has a trailing one', () => {
        renderRow({ server: server({ base_url: 'http://localhost:9001/' }) });
        expect(screen.getByText('http://localhost:9001/mcp')).toBeTruthy();
    });

    it('reads the address from the base URL and the endpoint path alone', () => {
        renderRow({
            server: server({
                base_url: '',
                mcp_endpoint_path: '',
                url: 'https://mcp.example.com/api/mcp',
            } as never),
        });
        expect(screen.queryByText('https://mcp.example.com/api/mcp')).toBeNull();
        expect(screen.getByText('Not configured')).toBeTruthy();
    });

});

describe('ServerRow — status disclosure', () => {
    afterEach(() => cleanup());

    it('reports health, defaulting to Unknown when never checked', () => {
        const { update } = renderRow();
        expect(screen.getByText('Unknown')).toBeTruthy();

        update({
            health_status: {
                status: 'unhealthy',
                last_check: '2026-01-01T00:00:00Z',
                consecutive_failures: 3,
                error_message: 'connection refused',
            },
        });
        const badge = screen.getByText('Unhealthy');
        expect(badge.getAttribute('title')).toBe('connection refused');
    });

    it.each([
        ['healthy', 'Healthy'],
        ['degraded', 'Degraded'],
        ['unhealthy', 'Unhealthy'],
        ['unknown', 'Unknown'],
        // No server is stored with this: it is shown as what it is, not as Unknown.
        ['warning', 'Unknown status: warning'],
    ])('reads the status %s as "%s"', (status, label) => {
        renderRow({ server: server({ health_status: { status, consecutive_failures: 0 } }) });
        expect(screen.getByText(label).getAttribute('title')).toBe(`Health: ${label}`);
    });

    it('shows Enabled or Disabled for the server itself', () => {
        const { update } = renderRow();
        expect(screen.getByText('Enabled')).toBeTruthy();

        update({ enabled: false });
        expect(screen.getByText('Disabled')).toBeTruthy();
    });

    it('flags an OAuth server with no IdP linked — it can never complete a login', () => {
        renderRow({ server: server({ auth_type: 'oauth' }) });
        expect(screen.getByText('No IdP linked')).toBeTruthy();
    });

    describe('what the Control Plane states of an OAuth-protected server (oauth_pairing)', () => {
        const WARNING =
            'No identity provider is paired with this OAuth-protected server: the gateway refuses the sign-in (403 "Identity Provider is not configured for this MCP server") until the server names an identity provider, or a server configuration of it attaches an active OAuth client.';

        it('shows its warning in place of the short flag when no identity provider is paired', () => {
            renderRow({ server: server({ auth_type: 'oauth', oauth_pairing: { paired: false, idp_ids: [], warning: WARNING } }) });
            expect(screen.getByTestId('server-oauth-pairing-warning')).toHaveTextContent(WARNING);
            expect(screen.queryByText('No IdP linked')).toBeNull();
        });

        it('shows the warning also under an identity provider the server names but is not paired with', () => {
            renderRow({
                server: server({
                    auth_type: 'oauth', idp_id: 'idp-gone', idp_name: 'Old SSO',
                    oauth_pairing: { paired: false, idp_ids: [], warning: WARNING },
                }),
            });
            expect(screen.getByText('Old SSO')).toBeTruthy();
            expect(screen.getByTestId('server-oauth-pairing-warning')).toHaveTextContent(WARNING);
        });

        it('does not flag a server that names no identity provider but is paired through an OAuth client', () => {
            renderRow({ server: server({ auth_type: 'oauth', oauth_pairing: { paired: true, idp_ids: ['idp-b'], warning: null } }) });
            expect(screen.queryByText('No IdP linked')).toBeNull();
            expect(screen.queryByTestId('server-oauth-pairing-warning')).toBeNull();
            expect(screen.getByText('Paired through an OAuth client')).toBeTruthy();
        });

        it('says nothing more of a paired server that names its identity provider, nor of a server that is not OAuth-protected', () => {
            const { update } = renderRow({
                server: server({ auth_type: 'oauth', idp_id: 'idp-1', idp_name: 'Keycloak', oauth_pairing: { paired: true, idp_ids: ['idp-1'], warning: null } }),
            });
            expect(screen.getByText('Keycloak')).toBeTruthy();
            expect(screen.queryByTestId('server-oauth-pairing-warning')).toBeNull();
            update({ auth_type: 'bearer', idp_id: undefined, idp_name: undefined, oauth_pairing: null });
            expect(screen.queryByTestId('server-oauth-pairing-warning')).toBeNull();
            expect(screen.queryByText('No IdP linked')).toBeNull();
        });
    });

    it('names the linked IdP instead when one is bound', () => {
        renderRow({ server: server({ auth_type: 'oauth', idp_id: 'idp-1', idp_name: 'Keycloak' }) });
        expect(screen.getByText('Keycloak')).toBeTruthy();
        expect(screen.queryByText('No IdP linked')).toBeNull();
    });

    it('distinguishes never-discovered from discovered-but-empty capabilities', () => {
        const { update } = renderRow();
        expect(screen.getByText('No capabilities discovered')).toBeTruthy();

        update({ capabilities: { tools: [], resources: [], prompts: [] } });
        expect(screen.getByText('No capabilities found')).toBeTruthy();
    });

    it('counts discovered capabilities and breaks them down by kind', () => {
        renderRow({
            server: server({
                capabilities: {
                    tools: [{ name: 'a' }, { name: 'b' }],
                    resources: [{ name: 'r' }],
                    prompts: [],
                },
            }),
        });
        expect(screen.getByText('3 capabilities')).toBeTruthy();
        expect(screen.getByText('2 tools')).toBeTruthy();
        expect(screen.getByText('1 resources')).toBeTruthy();
        expect(screen.queryByText(/prompts/)).toBeNull();
    });

    it('renders the negotiated MCP wire contract when discovery established one', () => {
        renderRow({ server: server({ protocol_version: '2026-07-28', dialect: 'dual' }) });
        const badge = screen.getByText(/MCP 2026-07-28/);
        expect(badge.textContent).toContain('dual');
        expect(badge.getAttribute('title')).toMatch(/Dual-era/);
    });

    it('offers the OAuth discovery hint only for an OAuth server with nothing discovered', () => {
        const onShowOAuthInfo = vi.fn();
        const { update } = renderRow({
            server: server({ auth_type: 'oauth' }),
            onShowOAuthInfo,
        });
        fireEvent.click(screen.getByRole('button', { name: /how to discover capabilities/i }));
        expect(onShowOAuthInfo).toHaveBeenCalledWith('github-mcp');

        update({ capabilities: { tools: [{ name: 'a' }] } });
        expect(screen.queryByRole('button', { name: /how to discover capabilities/i })).toBeNull();
    });
});

describe('ServerRow — interactions', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(() => cleanup());

    it('reports selection changes to the caller', () => {
        const props = renderRow();
        fireEvent.click(screen.getByRole('checkbox'));
        expect(props.onSelect).toHaveBeenCalledWith(true);
    });

    it('opens the endpoint modal against the proxy base URL', () => {
        renderRow();
        fireEvent.click(screen.getByRole('button', { name: /^endpoint$/i }));
        expect(screen.getByText('http://localhost:8100/v1/proxy/mcp/github-mcp')).toBeTruthy();
    });

    it('opens the capability viewer only once something has been discovered', () => {
        const { update } = renderRow();
        fireEvent.click(screen.getByText('No capabilities discovered'));
        expect(screen.queryByText('Discovered Capabilities')).toBeNull();

        update({ capabilities: { tools: [{ name: 'search' }] } });
        fireEvent.click(screen.getByText('1 capabilities'));
        expect(screen.getByText('Discovered Capabilities')).toBeTruthy();
        expect(screen.getByText('search')).toBeTruthy();
    });

    it('routes the menu actions and closes the menu behind each one', () => {
        const props = renderRow();
        openMenu();

        fireEvent.click(screen.getByRole('button', { name: /^edit$/i }));
        expect(props.onEdit).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole('button', { name: /^edit$/i })).toBeNull();

        openMenu();
        fireEvent.click(screen.getByRole('button', { name: /^disable$/i }));
        expect(props.onToggle).toHaveBeenCalledTimes(1);

        openMenu();
        fireEvent.click(screen.getByRole('button', { name: /^delete$/i }));
        expect(props.onDelete).toHaveBeenCalledTimes(1);
    });

    it('labels the toggle by what it will do', () => {
        renderRow({ server: server({ enabled: false }) });
        openMenu();
        expect(screen.getByRole('button', { name: /^enable$/i })).toBeTruthy();
    });

    it('runs the optional health check and capability refresh for this server', async () => {
        const onHealthCheck = vi.fn().mockResolvedValue(undefined);
        const onRefreshCapabilities = vi.fn().mockResolvedValue(undefined);
        renderRow({ onHealthCheck, onRefreshCapabilities });

        openMenu();
        fireEvent.click(screen.getByRole('button', { name: /health check/i }));
        await waitFor(() => expect(onHealthCheck).toHaveBeenCalledWith('github-mcp'));

        openMenu();
        fireEvent.click(screen.getByRole('button', { name: /refresh capabilities/i }));
        await waitFor(() => expect(onRefreshCapabilities).toHaveBeenCalledWith('github-mcp'));
    });

    it('omits the optional menu entries when no handler is supplied', () => {
        renderRow();
        openMenu();
        expect(screen.queryByRole('button', { name: /health check/i })).toBeNull();
        expect(screen.queryByRole('button', { name: /refresh capabilities/i })).toBeNull();
    });

    it('disables the mutating menu entries while a row action is in flight', () => {
        renderRow({ loading: true });
        openMenu();
        expect(screen.getByRole('button', { name: /^edit$/i }).hasAttribute('disabled')).toBe(true);
        expect(screen.getByRole('button', { name: /^delete$/i }).hasAttribute('disabled')).toBe(true);
    });
});

describe('ServerRow — requests in flight at once', () => {
    afterEach(() => cleanup());

    it('a server that states none allows the default of 10 on each gateway instance, and says so', () => {
        renderRow({ server: server({ max_concurrent_requests: null }) });
        expect(screen.getByTestId('server-max-concurrent-requests')).toHaveTextContent(
            'Max concurrent requests (per gateway instance): 10 (default)',
        );
    });

    it('a server that states a number shows it', () => {
        renderRow({ server: server({ max_concurrent_requests: 25 }) });
        expect(screen.getByTestId('server-max-concurrent-requests')).toHaveTextContent(
            'Max concurrent requests (per gateway instance): 25',
        );
        expect(screen.getByTestId('server-max-concurrent-requests')).not.toHaveTextContent('default');
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP servers bound to a group.
 *
 * Enablement here is TWO-LEVEL and the distinction is the whole reason the
 * column carries an explainer: a binding can be switched off in its own
 * right, or it can be dark because the MCP server behind it is disabled
 * globally. Only the second is fixed somewhere else, so the row has to say
 * which one it is ("by MCP Server") rather than reporting one flat
 * "Disabled".
 *
 * The capability cell is the other load-bearing one. "Not discovered" (we
 * have never seen this server's tools) must not read like "no capabilities"
 * — the first is a gap in our knowledge, the second is a governed surface
 * with nothing on it — and an approval gate must not be summed together
 * with a hard disable, since one asks a human and the other refuses.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

import GroupMCPServersTableView from '../GroupMCPServersTableView';
import type { ServerConfigInfo } from '../../../../lib/groupTypes';
import type { ServerHealthStatus } from '../../../../lib/types';
import { buildMCPServerConfig } from '../../../../test/mcp/mcpServerConfig';

/** A probed server. The status is a string in the contract; a server is stored with one of four. */
const health = (status: string): ServerHealthStatus => ({
    status,
    last_check: '2026-09-19T10:00:00Z',
    consecutive_failures: 0,
});

function server(overrides: Partial<ServerConfigInfo> = {}): ServerConfigInfo {
    return {
        id: 'cfg-1',
        name: 'salesforce',
        description: 'CRM read/write bridge',
        enabled: true,
        icon_url: '',
        is_inherited: false,
        mcp_server: buildMCPServerConfig({
            health_status: health('healthy'),
            capabilities: { tools: [{ name: 'query' }, { name: 'update' }] },
        }),
        ...overrides,
    };
}

function renderTable(
    servers: ServerConfigInfo[],
    props: Partial<React.ComponentProps<typeof GroupMCPServersTableView>> = {},
) {
    const onRemove = vi.fn();
    const onConfigureCapabilities = vi.fn();
    const view = render(
        <GroupMCPServersTableView
            servers={servers}
            onRemove={onRemove}
            onConfigureCapabilities={onConfigureCapabilities}
            removingId={null}
            {...props}
        />,
    );
    return { onRemove, onConfigureCapabilities, ...view };
}

const rowFor = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

describe('GroupMCPServersTableView — reading a row', () => {
    it('shows the config name, its id and its description', () => {
        renderTable([server()]);
        const row = rowFor('salesforce');
        expect(within(row).getByText('cfg-1')).toBeInTheDocument();
        expect(within(row).getByText('CRM read/write bridge')).toBeInTheDocument();
    });

    it('a config with no description says so rather than leaving a gap', () => {
        renderTable([server({ description: undefined })]);
        expect(within(rowFor('salesforce')).getByText('No description')).toBeInTheDocument();
    });

    it('separates direct bindings from inherited ones', () => {
        renderTable([server(), server({ id: 'cfg-2', name: 'jira', is_inherited: true })]);
        expect(within(rowFor('salesforce')).getByText('Direct')).toBeInTheDocument();
        expect(within(rowFor('jira')).getByText('Inherited')).toBeInTheDocument();
    });

    it('counts what it is showing, singular and plural', () => {
        const { rerender } = renderTable([server()]);
        expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 1 server');
        rerender(
            <GroupMCPServersTableView
                servers={[server(), server({ id: 'cfg-2', name: 'jira' })]}
                onRemove={vi.fn()}
                onConfigureCapabilities={vi.fn()}
                removingId={null}
            />,
        );
        expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 2 servers');
    });
});

describe('GroupMCPServersTableView — health', () => {
    it.each([
        ['healthy', 'Healthy'],
        ['unhealthy', 'Unhealthy'],
        ['degraded', 'Degraded'],
        ['unknown', 'Unknown'],
        // No server is stored with these: each is shown as what it is.
        ['warning', 'Unknown status: warning'],
        ['critical', 'Unknown status: critical'],
    ])('%s reads as "%s"', (status, label) => {
        renderTable([
            server({ mcp_server: buildMCPServerConfig({ health_status: health(status) }) }),
        ]);
        expect(within(rowFor('salesforce')).getByText(label)).toBeInTheDocument();
    });

    it('a server we have never probed reads as Unknown, not as healthy', () => {
        renderTable([server({ mcp_server: buildMCPServerConfig() })]);
        expect(within(rowFor('salesforce')).getByText('Unknown')).toBeInTheDocument();
    });
});

describe('GroupMCPServersTableView — two-level enablement', () => {
    it('an enabled binding says so', () => {
        renderTable([server()]);
        expect(within(rowFor('salesforce')).getByText('Enabled')).toBeInTheDocument();
    });

    it('a binding switched off in its own right reads as plain Disabled', () => {
        renderTable([server({ enabled: false })]);
        const row = rowFor('salesforce');
        expect(within(row).getByText('Disabled')).toBeInTheDocument();
        expect(within(row).queryByText('(by MCP Server)')).toBeNull();
    });

    it('a binding dark because the server itself is off says where to fix it', () => {
        renderTable([
            server({
                enabled: false,
                mcp_server: buildMCPServerConfig({ enabled: false, health_status: health('healthy') }),
            }),
        ]);
        const row = rowFor('salesforce');
        expect(within(row).getByText('Disabled')).toBeInTheDocument();
        expect(within(row).getByText('(by MCP Server)')).toBeInTheDocument();
    });

    it('the column explains the two levels on demand', () => {
        renderTable([server()]);
        expect(screen.queryByText('Two-level enable/disable')).toBeNull();
        const header = screen.getByRole('columnheader', { name: /Enabled/ });
        fireEvent.click(within(header).getByRole('button'));
        expect(screen.getByText('Two-level enable/disable')).toBeInTheDocument();
    });
});

describe('GroupMCPServersTableView — the capability cell', () => {
    it('a never-discovered server says so rather than reading as "nothing to govern"', () => {
        renderTable([server({ mcp_server: buildMCPServerConfig() })]);
        expect(within(rowFor('salesforce')).getByText('Not discovered')).toBeInTheDocument();
    });

    it('a discovered-but-empty server also reads as not discovered', () => {
        renderTable([
            server({
                mcp_server: buildMCPServerConfig({ capabilities: { tools: [], resources: [], prompts: [] } }),
            }),
        ]);
        expect(within(rowFor('salesforce')).getByText('Not discovered')).toBeInTheDocument();
    });

    it('counts each capability kind separately', () => {
        renderTable([
            server({
                mcp_server: buildMCPServerConfig({
                    capabilities: {
                        tools: [{ name: 'a' }, { name: 'b' }],
                        resources: [{ name: 'r' }],
                        prompts: [{ name: 'p1' }, { name: 'p2' }, { name: 'p3' }],
                        resource_templates: [{ name: 't' }],
                    },
                }),
            }),
        ]);
        const row = rowFor('salesforce');
        expect(within(row).getByTitle('2 tools')).toBeInTheDocument();
        expect(within(row).getByTitle('1 resources')).toBeInTheDocument();
        expect(within(row).getByTitle('3 prompts')).toBeInTheDocument();
        expect(within(row).getByTitle('1 templates')).toBeInTheDocument();
    });

    it('keeps "needs approval" apart from "switched off"', () => {
        renderTable([
            server({
                capability_filters: { has_filters: true, total: 7, enabled: 5, disabled: 2, approval_required: 3 },
            }),
        ]);
        const row = rowFor('salesforce');
        expect(within(row).getByText('3 approval')).toBeInTheDocument();
        expect(within(row).getByText('2 off')).toBeInTheDocument();
    });

    it('shows no filter badges on a server with no filters', () => {
        renderTable([server({ capability_filters: { has_filters: false, total: 0, enabled: 0, disabled: 0, approval_required: 0 } })]);
        const row = rowFor('salesforce');
        expect(within(row).queryByText(/off$/)).toBeNull();
        expect(within(row).queryByText(/approval$/)).toBeNull();
    });

    it('the cell is a shortcut into the capability filter dialog', () => {
        const s = server();
        const { onConfigureCapabilities } = renderTable([s]);
        fireEvent.click(within(rowFor('salesforce')).getByTitle('2 tools').closest('button')!);
        expect(onConfigureCapabilities).toHaveBeenCalledWith(s);
    });
});

describe('GroupMCPServersTableView — actions', () => {
    it('removal is offered on a direct binding only', () => {
        renderTable([server(), server({ id: 'cfg-2', name: 'jira', is_inherited: true })]);
        expect(within(rowFor('salesforce')).getByTitle('Remove server')).toBeInTheDocument();
        expect(within(rowFor('jira')).queryByTitle('Remove server')).toBeNull();
    });

    it('removal reports the id and name so the caller can confirm', () => {
        const { onRemove } = renderTable([server()]);
        fireEvent.click(within(rowFor('salesforce')).getByTitle('Remove server'));
        expect(onRemove).toHaveBeenCalledWith('cfg-1', 'salesforce');
    });

    it('a removal in flight disables that row’s button only', () => {
        renderTable([server(), server({ id: 'cfg-2', name: 'jira' })], { removingId: 'cfg-1' });
        expect(within(rowFor('salesforce')).getByTitle('Remove server')).toBeDisabled();
        expect(within(rowFor('jira')).getByTitle('Remove server')).toBeEnabled();
    });

    it('says on the row whether per-server limits are already in force', () => {
        renderTable([server({ has_limits: true })]);
        expect(
            within(rowFor('salesforce')).getByTitle('Per-server limits active — click to configure'),
        ).toBeInTheDocument();
    });

    it('routes per-server and per-tool limits to different handlers', () => {
        const onConfigureLimits = vi.fn();
        const onConfigureToolLimits = vi.fn();
        const s = server();
        renderTable([s], { onConfigureLimits, onConfigureToolLimits });
        const row = rowFor('salesforce');

        fireEvent.click(within(row).getByTitle('Configure per-server limits'));
        expect(onConfigureLimits).toHaveBeenCalledWith(s);

        fireEvent.click(within(row).getByTitle('Configure per-tool rate limits'));
        expect(onConfigureToolLimits).toHaveBeenCalledWith(s);
        expect(onConfigureLimits).toHaveBeenCalledTimes(1);
    });
});

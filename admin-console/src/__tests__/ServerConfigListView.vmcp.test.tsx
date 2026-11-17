/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * ServerConfigListView VMCP rendering regression
 *
 * VMCP-backed configs and MCP-backed configs share the same table
 * but render distinctly in the "MCP / VMCP Server" column:
 *
 *   - MCP-backed: existing server icon + name.
 *   - VMCP-backed: sky-blue `Layers` glyph in a `bg-sky-100`
 *     circle, name from the looked-up VMCP, and a small
 *     `bg-sky-100 text-sky-700` "VMCP" badge.
 *
 * The Auth column also differs:
 *
 *   - MCP-backed: OAuth badge or "Open".
 *   - VMCP-backed: "Delegated" with a `Layers` glyph (VMCPs
 *     delegate per-call auth to the underlying MCPs, so a
 *     per-config OAuth client doesn't apply).
 *
 * Covered:
 *
 *   1. Column header reads "MCP / VMCP Server".
 *   2. VMCP-backed row renders the sky-blue VMCP badge with the
 *      looked-up friendly name.
 *   3. VMCP-backed row's Auth cell shows "Delegated", NOT "Open"
 *      or "OAuth".
 *   4. MCP-backed row renders the OAuth/Open badges.
 *   5. If the VMCP isn't in the lookup list (operator deleted it
 *      after the config was created), the row falls back to the
 *      VMCP id string so the operator can still identify and
 *      clean up the orphaned config.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import type { ServerConfig } from '../lib/types';
import ServerConfigListView from '../components/mcp/config/server-access/server_configs/ServerConfigListView';


vi.mock('../hooks/useGateway.ts', () => ({
    useGateway: () => ({
        getServerIconUrl: (id: string) => `https://icons.example/${id}.png`,
    }),
}));


const MCP_SERVERS = [
    {
        id: 'mcp-real',
        name: 'Real MCP Server',
        auth_type: 'none',
        enabled: true,
    },
];

const VMCPS = [
    {
        id: 'vs-aggregator',
        name: 'Customer Aggregator',
        enabled: true,
    },
];


const buildConfig = (overrides: Partial<ServerConfig> = {}): ServerConfig => ({
    id: 'cfg-default',
    name: 'Default',
    description: null,
    mcp_server_id: null,
    virtual_server_id: null,
    server_kind: 'mcp',
    oauth_client_id: null,
    priority: 100,
    is_default: false,
    enabled: true,
    display_name: null,
    display_description: null,
    icon_url: null,
    usage_count: 0,
    last_used: null,
    created_by: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    group_ids: [],
    groups: [],
    requires_oauth: false,
    is_open_server: true,
    ...overrides,
});


describe('ServerConfigListView — VMCP rendering', () => {

    it('renames the column header to "MCP / VMCP Server"', () => {
        render(
            <ServerConfigListView
                configs={[]}
                servers={MCP_SERVERS}
                virtualServers={VMCPS}
                oauthClients={[]}
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />
        );
        expect(screen.getByText(/^MCP \/ VMCP Server$/)).toBeTruthy();
    });

    it('renders a VMCP-backed config with sky-blue badge + Delegated auth', () => {
        const cfg = buildConfig({
            id: 'cfg-vmcp',
            name: 'Aggregator Access',
            virtual_server_id: 'vs-aggregator',
            server_kind: 'vmcp',
            mcp_server_id: null,
            oauth_client_id: null,
        });

        render(
            <ServerConfigListView
                configs={[cfg]}
                servers={MCP_SERVERS}
                virtualServers={VMCPS}
                oauthClients={[]}
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />
        );

        // The VMCP badge text is "VMCP". There must be exactly one
        // such badge in the row.
        const vmcpBadge = screen.getByText(/^VMCP$/);
        expect(vmcpBadge).toBeTruthy();
        // The badge carries the sky-blue token pair.
        expect(vmcpBadge.className).toMatch(/bg-sky-100/);
        expect(vmcpBadge.className).toMatch(/text-sky-700/);

        // Friendly VMCP name is looked up from the props.
        expect(screen.getByText('Customer Aggregator')).toBeTruthy();

        // Auth column shows "Delegated".
        expect(screen.getByText(/^Delegated$/)).toBeTruthy();
        // And does NOT show "Open" or "OAuth".
        expect(screen.queryByText(/^Open$/)).toBeNull();
        expect(screen.queryByText(/^OAuth$/)).toBeNull();
    });

    it('renders an MCP-backed config with the existing icon + Open badge (no regression)', () => {
        const cfg = buildConfig({
            id: 'cfg-mcp',
            name: 'Real Access',
            mcp_server_id: 'mcp-real',
            server_kind: 'mcp',
            virtual_server_id: null,
            mcp_server: {
                name: 'Real MCP Server',
                auth_type: 'none',
            },
        });

        render(
            <ServerConfigListView
                configs={[cfg]}
                servers={MCP_SERVERS}
                virtualServers={VMCPS}
                oauthClients={[]}
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />
        );

        // Existing "Open" badge for a no-auth MCP — proves we didn't
        // accidentally drive the row down the VMCP code path.
        expect(screen.getByText(/^Open$/)).toBeTruthy();
        // No VMCP badge in the row.
        expect(screen.queryByText(/^VMCP$/)).toBeNull();
        expect(screen.queryByText(/^Delegated$/)).toBeNull();
        // MCP server name appears.
        expect(screen.getByText('Real MCP Server')).toBeTruthy();
    });

    it('falls back to the VMCP id when the VMCP is not in the lookup (orphaned config)', () => {
        const cfg = buildConfig({
            id: 'cfg-orphan',
            name: 'Orphan Config',
            virtual_server_id: 'vs-deleted',
            server_kind: 'vmcp',
            mcp_server_id: null,
            // No nested virtual_server object — happens if the row
            // was loaded with a stale lookup, or the VMCP was deleted
            // out from under it.
            virtual_server: null,
        });

        render(
            <ServerConfigListView
                configs={[cfg]}
                // virtualServers list deliberately empty, simulating
                // an orphaned binding the operator needs to find and
                // clean up.
                servers={MCP_SERVERS}
                virtualServers={[]}
                oauthClients={[]}
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />
        );

        // The row must STILL render, with the raw id as the fallback
        // label so the operator can identify what got orphaned.
        expect(screen.getByText('vs-deleted')).toBeTruthy();
        // The sky-blue VMCP badge is still there — it's keyed off
        // `server_kind === 'vmcp'`, not the lookup.
        expect(screen.getByText(/^VMCP$/)).toBeTruthy();
    });

    it('uses the nested virtual_server.name when present (the API embeds it)', () => {
        const cfg = buildConfig({
            id: 'cfg-nested',
            name: 'Nested Lookup',
            virtual_server_id: 'vs-x',
            server_kind: 'vmcp',
            mcp_server_id: null,
            // Server-side response embeds the VMCP info; the list
            // view should prefer the nested name over the picker
            // lookup so that the cell stays accurate even when the
            // parent prop list is stale.
            virtual_server: {
                id: 'vs-x',
                name: 'Embedded Name From API',
                enabled: true,
                tool_count: 4,
            },
        });

        render(
            <ServerConfigListView
                configs={[cfg]}
                servers={MCP_SERVERS}
                // Lookup list says something different — to prove
                // the nested name wins.
                virtualServers={[{ id: 'vs-x', name: 'Stale Picker Name' }]}
                oauthClients={[]}
                onEdit={vi.fn()}
                onDelete={vi.fn()}
            />
        );

        expect(screen.getByText('Embedded Name From API')).toBeTruthy();
        expect(screen.queryByText('Stale Picker Name')).toBeNull();
    });
});


// Smoke utility — kept so the linter doesn't complain about the
// `within` import; useful if the test suite later needs to assert
// "the VMCP badge is INSIDE the same row as the config name".
void within;

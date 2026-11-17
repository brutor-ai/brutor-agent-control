/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * VirtualServerForm tool renaming
 *
 * Covers tool renaming, subsetting and the prefix in the VMCP editor:
 *
 *   1. Tool-name prefix applied as a VMCP-wide default.
 *   2. Explicit per-tool exposed_name overrides the prefix and the
 *      original capability_name.
 *   3. description_override round-trips on the submitted payload.
 *   4. Collision detection: two tools whose effective exposed names
 *      match get visually flagged so the operator sees the problem
 *      before they save (the backend rejects on save too).
 *   5. The submitted payload's `capabilities` carries the rename
 *      fields ONLY on tool entries (resources/prompts don't have
 *      the rename mechanic).
 *   6. Edit-mode preload: opening an existing VMCP with overrides
 *      shows them in the form.
 */

import { describe, expect, it, vi, beforeEach, type Mock } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import VirtualServerForm from '../components/mcp/virtual/VirtualServerForm';
import type { VirtualServerConfig } from '../lib/types';

// Stub hooks. The mocked return values MUST be stable across renders
// — the form's preload effect has [virtualServer, currentUser] deps,
// and a fresh `user` object every render triggers an infinite loop.
const STABLE_USER = { id: 'admin-1', username: 'admin' };
const STABLE_USERS: unknown[] = [];
const STABLE_HAS_PERMISSION = () => true;

vi.mock('../hooks/useAuth.tsx', () => ({
    useAuth: () => ({
        user: STABLE_USER,
        hasPermission: STABLE_HAS_PERMISSION,
    }),
}));

vi.mock('../hooks/useGateway.ts', () => ({
    useGateway: () => ({
        users: STABLE_USERS,
        usersLoaded: true,
        loadingUsers: false,
        fetchUsers: vi.fn(),
        getUsernameById: (id: string) => id,
        getUserDisplayName: (id: string) => id,
    }),
}));


const makeAvailableServer = (id: string, toolNames: string[], opts?: {
    resources?: { name?: string; uri: string }[];
}) => ({
    id,
    name: id,
    base_url: `https://${id}.example.invalid`,
    enabled: true,
    capabilities: {
        tools: toolNames.map(n => ({ name: n, description: `${n} desc` })),
        resources: opts?.resources ?? [],
        prompts: [],
    },
});


/** Helper: select the upstream server in the "Add Capabilities"
 * panel. There's exactly one <select> in the form (the Capability
 * Type uses buttons), so getByRole('combobox') is unambiguous. */
function selectServer(serverId: string) {
    const serverSelect = screen.getByRole('combobox');
    fireEvent.change(serverSelect, { target: { value: serverId } });
}

/** Helper: click the Add button on the row whose capability name
 * matches `capName`. Scoped to the right-hand "Add Capabilities"
 * panel so we don't trip over a left-panel row with the same name
 * (which appears once the capability has been added).
 *
 * We anchor on the "Add Capabilities" heading and walk to its
 * parent section, then search inside that subtree only. */
function addCapability(capName: string) {
    const heading = screen.getByText('Add Capabilities');
    const panel = heading.closest('div')!.parentElement as HTMLElement;
    const matches = within(panel).getAllByText(capName);
    // Find the first one that's a list row (not already-added).
    const nameSpan = matches.find(el => {
        const row = el.closest('div.flex.items-center.justify-between');
        return row && within(row as HTMLElement).queryByRole('button', { name: /^Add$/i });
    });
    expect(nameSpan).toBeDefined();
    const row = nameSpan!.closest('div.flex.items-center.justify-between') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: /^Add$/i }));
}


describe('VirtualServerForm — tool renaming/subsetting', () => {
    /**
     * What the form hands to `onSave`: the fields of a virtual server, and on
     * its capabilities the prefix and the per-tool rename and description
     * the form adds, which the type of a virtual server does not name.
     */
    type SavedCapability = { server_id: string; capability_name: string; exposed_name?: string; description_override?: string };
    type Saved = Omit<VirtualServerConfig, 'created_at' | 'capabilities'> & {
        capabilities: {
            tool_name_prefix?: string;
            tools?: SavedCapability[];
            resources?: SavedCapability[];
            prompts?: SavedCapability[];
        };
    };
    type OnSave = (formData: Saved) => Promise<void>;
    let onSave: Mock<OnSave>;
    beforeEach(() => {
        onSave = vi.fn<OnSave>().mockResolvedValue(undefined);
    });

    it('emits tool_name_prefix on the capabilities payload when set', async () => {
        const availableServers = [makeAvailableServer('srv-a', ['search'])];
        render(
            <VirtualServerForm
                virtualServer={null}
                availableServers={availableServers as any}
                onSave={onSave}
                onCancel={() => {}}
            />,
        );

        fireEvent.change(screen.getByPlaceholderText(/e\.g\., Productivity Assistant/i), {
            target: { value: 'Test VMCP' },
        });

        selectServer('srv-a');
        addCapability('search');

        fireEvent.change(screen.getByPlaceholderText(/applied to every tool without an explicit rename/i), {
            target: { value: 'acme_' },
        });

        fireEvent.click(screen.getByRole('button', { name: /Create Virtual Server/i }));
        await vi.waitFor(() => expect(onSave).toHaveBeenCalled());

        const payload = onSave.mock.calls[0][0];
        expect(payload.capabilities.tool_name_prefix).toBe('acme_');
        expect(payload.capabilities.tools).toHaveLength(1);
        expect(payload.capabilities.tools?.[0].exposed_name).toBeUndefined();
    });

    it('emits explicit exposed_name on the tool entry when operator customises', async () => {
        const availableServers = [makeAvailableServer('srv-a', ['search'])];
        render(
            <VirtualServerForm
                virtualServer={null}
                availableServers={availableServers as any}
                onSave={onSave}
                onCancel={() => {}}
            />,
        );

        fireEvent.change(screen.getByPlaceholderText(/e\.g\., Productivity Assistant/i), {
            target: { value: 'Test VMCP' },
        });
        selectServer('srv-a');
        addCapability('search');

        // Expand the per-tool customise editor.
        fireEvent.click(screen.getByRole('button', { name: /Customise/i }));

        const exposedInput = screen.getByPlaceholderText(/Default: search/i);
        fireEvent.change(exposedInput, { target: { value: 'company_search' } });

        const descTextarea = screen.getByPlaceholderText(/Leave empty to keep the upstream/i);
        fireEvent.change(descTextarea, { target: { value: 'Custom description' } });

        fireEvent.click(screen.getByRole('button', { name: /Create Virtual Server/i }));
        await vi.waitFor(() => expect(onSave).toHaveBeenCalled());

        const tool = onSave.mock.calls[0][0].capabilities.tools?.[0];
        expect(tool?.exposed_name).toBe('company_search');
        expect(tool?.description_override).toBe('Custom description');
        expect(tool?.capability_name).toBe('search');
    });

    it('flags exposed-name collisions in the UI before submit', async () => {
        // Two upstreams both have a `search` tool. With no override
        // and no prefix, both resolve to `search` — the UI must warn.
        const availableServers = [
            makeAvailableServer('srv-a', ['search']),
            makeAvailableServer('srv-b', ['search']),
        ];
        render(
            <VirtualServerForm
                virtualServer={null}
                availableServers={availableServers as any}
                onSave={onSave}
                onCancel={() => {}}
            />,
        );

        fireEvent.change(screen.getByPlaceholderText(/e\.g\., Productivity Assistant/i), {
            target: { value: 'Test VMCP' },
        });

        selectServer('srv-a');
        addCapability('search');
        selectServer('srv-b');
        addCapability('search');

        // Both rows in the left "Selected Capabilities" panel render
        // the collision badge.
        const collisions = screen.getAllByText(/^collision$/i);
        expect(collisions.length).toBeGreaterThanOrEqual(2);
    });

    it('does NOT attach rename fields to resource entries', async () => {
        // Resources are not part of the tool rename mechanic.
        const availableServers = [
            makeAvailableServer('srv-a', [], {
                resources: [{ uri: 'res://doc1' }],
            }),
        ];
        render(
            <VirtualServerForm
                virtualServer={null}
                availableServers={availableServers as any}
                onSave={onSave}
                onCancel={() => {}}
            />,
        );

        fireEvent.change(screen.getByPlaceholderText(/e\.g\., Productivity Assistant/i), {
            target: { value: 'Test VMCP' },
        });

        // The Capability Type tabs are buttons with capitalised
        // labels. Find by visible text inside the button.
        const resourcesTabBtn = screen.getAllByText('resources').find(
            el => el.closest('button') !== null,
        );
        expect(resourcesTabBtn).toBeDefined();
        fireEvent.click(resourcesTabBtn!.closest('button')!);

        selectServer('srv-a');
        addCapability('res://doc1');

        fireEvent.click(screen.getByRole('button', { name: /Create Virtual Server/i }));
        await vi.waitFor(() => expect(onSave).toHaveBeenCalled());

        const resource = onSave.mock.calls[0][0].capabilities.resources?.[0];
        expect(resource).toEqual({ server_id: 'srv-a', capability_name: 'res://doc1' });
        // Critically — no rename fields on resources.
        expect(resource).not.toHaveProperty('exposed_name');
        expect(resource).not.toHaveProperty('description_override');
    });

    it('preloads existing exposed_name + description_override + prefix when editing', () => {
        const existing = {
            id: 'vs-existing',
            name: 'Pre-existing VMCP',
            description: '',
            owner: 'admin-1',
            enabled: true,
            capabilities: {
                tool_name_prefix: 'acme_',
                tools: [
                    {
                        server_id: 'srv-a',
                        capability_name: 'search',
                        exposed_name: 'company_search',
                        description_override: 'Custom',
                    },
                ],
                resources: [],
                prompts: [],
                source_mapping: { 'company_search': 'srv-a' },
                reverse_name_mapping: { 'company_search': 'search' },
            },
        };
        render(
            <VirtualServerForm
                virtualServer={existing as any}
                availableServers={[makeAvailableServer('srv-a', ['search'])] as any}
                onSave={vi.fn()}
                onCancel={() => {}}
            />,
        );

        // Prefix loaded into the form.
        const prefixInput = screen.getByPlaceholderText<HTMLInputElement>(/applied to every tool without an explicit rename/i);
        expect(prefixInput.value).toBe('acme_');

        // Effective exposed name visible inline on the row.
        expect(screen.getByText('company_search')).toBeInTheDocument();
        // Customised badge present.
        expect(screen.getByText(/^customised$/i)).toBeInTheDocument();
    });
});

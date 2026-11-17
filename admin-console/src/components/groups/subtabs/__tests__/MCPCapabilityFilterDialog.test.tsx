/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCP capability filters — per-group E/A/D controls.
 *
 * Pinned: a capability restricted by an ancestor cannot be relaxed, by its own
 * control or by any bulk action, and that shows in the payload. An all-enabled
 * state clears the filters rather than storing a no-op set. Discovered
 * capabilities with no stored filter join the list as enabled.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import type { ServerConfigInfo } from '../../../../lib/groupTypes';

const gateway = {
    getMCPCapabilityFilters: vi.fn(),
    updateMCPCapabilityFilters: vi.fn(),
    clearMCPCapabilityFilters: vi.fn(),
    listArgumentPolicies: vi.fn(() => Promise.resolve({ policies: [] })),
};
const toast = { success: vi.fn(), error: vi.fn() };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../ArgumentPolicyGateBadge', () => ({ default: () => null }));

import MCPCapabilityFilterDialog from '../MCPCapabilityFilterDialog';

const SERVER = { id: 'srv-1', name: 'GitHub', mcp_server: { auth_type: 'none' } } as unknown as ServerConfigInfo;
const onClose = vi.fn();

const tool = (name: string, description?: string) => ({ capability_type: 'tool', name, description });

async function open(server: ServerConfigInfo = SERVER) {
    render(<MCPCapabilityFilterDialog isOpen onClose={onClose} groupId="rg-1" groupName="Eng" server={server} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save Filters' })).toBeEnabled());
    expect(gateway.getMCPCapabilityFilters).toHaveBeenCalledWith('rg-1', server.id);
}

/** The row holding this capability's name. */
function row(name: string): HTMLElement {
    let el: HTMLElement | null = screen.getByText(name);
    while (el && within(el).queryAllByRole('button', { name: 'E' }).length === 0) el = el.parentElement;
    return el as HTMLElement;
}
const pick = (name: string, access: 'E' | 'A' | 'D') =>
    fireEvent.click(within(row(name)).getByRole('button', { name: access }));
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Save Filters' }));

describe('MCPCapabilityFilterDialog', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        gateway.getMCPCapabilityFilters.mockReset();
        gateway.updateMCPCapabilityFilters.mockReset();
        gateway.clearMCPCapabilityFilters.mockReset();
        gateway.updateMCPCapabilityFilters.mockResolvedValue({ has_filters: true });
        gateway.clearMCPCapabilityFilters.mockResolvedValue(undefined);
        gateway.getMCPCapabilityFilters.mockResolvedValue({
            has_filters: false,
            capabilities: [],
            discovered_capabilities: [tool('create_issue', 'Opens an issue'), tool('delete_repo')],
        });
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('renders nothing and loads nothing when closed', () => {
        const { container } = render(
            <MCPCapabilityFilterDialog isOpen={false} onClose={onClose} groupId="rg-1" groupName="Eng" server={SERVER} />,
        );
        expect(container).toBeEmptyDOMElement();
        expect(gateway.getMCPCapabilityFilters).not.toHaveBeenCalled();
    });

    it('with no restriction left, clears filters rather than storing an all-enabled set', async () => {
        await open();
        expect(screen.getByText('Opens an issue')).toBeInTheDocument();
        expect(screen.getByText('No filters active.')).toBeInTheDocument();

        save();
        await waitFor(() => expect(gateway.clearMCPCapabilityFilters).toHaveBeenCalledWith('rg-1', 'srv-1'));
        expect(gateway.updateMCPCapabilityFilters).not.toHaveBeenCalled();
        expect(toast.success).toHaveBeenCalledWith('All capabilities enabled — filters cleared');
        expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('sends each capability with its chosen access and an approval timeout', async () => {
        await open();
        pick('delete_repo', 'D');
        pick('create_issue', 'A');
        fireEvent.change(screen.getByPlaceholderText('300'), { target: { value: '600' } });

        save();
        await waitFor(() =>
            expect(gateway.updateMCPCapabilityFilters).toHaveBeenCalledWith('rg-1', 'srv-1', [
                { capability_type: 'tool', capability_name: 'create_issue', access: 'approval_required', approval_timeout_seconds: 600 },
                { capability_type: 'tool', capability_name: 'delete_repo', access: 'disabled' },
            ]),
        );
        expect(toast.success).toHaveBeenCalledWith('Capability filters saved (1 approval, 1 disabled)');
        expect(onClose).toHaveBeenCalled();
    });

    it('bounds a capability\'s approval timeout to 1 .. 604800 seconds and saves nothing outside it', async () => {
        const RANGE = 'An approval timeout is a whole number of seconds from 1 to 604800 (7 days).';
        await open();
        pick('create_issue', 'A');
        const input = screen.getByPlaceholderText<HTMLInputElement>('300');
        expect(input.min).toBe('1');
        expect(input.max).toBe('604800');
        for (const value of ['0', '604801']) {
            fireEvent.change(input, { target: { value } });
            expect(screen.getByRole('alert')).toHaveTextContent(RANGE);
            expect(screen.getByRole('button', { name: 'Save Filters' })).toBeDisabled();
        }
        fireEvent.change(input, { target: { value: '1' } });
        expect(screen.queryByRole('alert')).toBeNull();
        save();
        await waitFor(() => expect(gateway.updateMCPCapabilityFilters).toHaveBeenCalled());
        expect(gateway.updateMCPCapabilityFilters.mock.calls[0][2]).toContainEqual(
            { capability_type: 'tool', capability_name: 'create_issue', access: 'approval_required', approval_timeout_seconds: 1 },
        );
    });

    it('merges stored filters with newly discovered capabilities, which join as enabled', async () => {
        gateway.getMCPCapabilityFilters.mockResolvedValue({
            has_filters: true,
            capabilities: [{ capability_type: 'tool', capability_name: 'delete_repo', access: 'disabled' }],
            discovered_capabilities: [tool('delete_repo'), tool('new_tool')],
        });
        await open();
        expect(screen.getByText('1 enabled', { exact: false })).toBeInTheDocument();
        save();
        await waitFor(() => expect(gateway.updateMCPCapabilityFilters).toHaveBeenCalled());
        expect(gateway.updateMCPCapabilityFilters.mock.calls[0][2]).toEqual([
            { capability_type: 'tool', capability_name: 'delete_repo', access: 'disabled' },
            { capability_type: 'tool', capability_name: 'new_tool', access: 'enabled' },
        ]);
    });

    describe('inherited restrictions', () => {
        beforeEach(() => {
            gateway.getMCPCapabilityFilters.mockResolvedValue({
                has_filters: true,
                capabilities: [
                    {
                        capability_type: 'tool',
                        capability_name: 'locked',
                        access: 'approval_required',
                        inherited_access: 'approval_required',
                        source_group_name: 'Org',
                    },
                    { capability_type: 'tool', capability_name: 'free', access: 'disabled' },
                ],
                discovered_capabilities: [tool('locked'), tool('free')],
            });
        });

        it('locks the looser choices and offers no way to remove the row', async () => {
            await open();
            expect(screen.getByText('Org')).toBeInTheDocument();
            expect(screen.getByText(/You cannot relax them/)).toBeInTheDocument();

            const locked = row('locked');
            expect(within(locked).getByRole('button', { name: 'E' })).toBeDisabled();
            expect(within(locked).getByRole('button', { name: 'E' })).toHaveAttribute(
                'title',
                'Cannot relax — parent requires approval required',
            );
            expect(within(locked).getByRole('button', { name: 'A' })).toBeEnabled();
            expect(within(locked).queryByTitle('Remove')).toBeNull();
            expect(within(row('free')).getByTitle('Remove')).toBeInTheDocument();
        });

        it('may tighten further', async () => {
            await open();
            pick('locked', 'D');
            save();
            await waitFor(() => expect(gateway.updateMCPCapabilityFilters).toHaveBeenCalled());
            expect((gateway.updateMCPCapabilityFilters.mock.calls[0][2] as unknown[])[0]).toMatchObject({
                capability_name: 'locked',
                access: 'disabled',
            });
        });

        it.each(['Enable all (clear filters)', 'Enable All'])(
            'bulk "%s" leaves the inherited restriction in the payload',
            async (label) => {
                await open();
                fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${label.replace(/[()]/g, '\\$&')}$`) }));
                save();
                await waitFor(() => expect(gateway.updateMCPCapabilityFilters).toHaveBeenCalled());
                expect(gateway.clearMCPCapabilityFilters).not.toHaveBeenCalled();
                const sent = gateway.updateMCPCapabilityFilters.mock.calls[0][2] as { capability_name: string; access: string }[];
                expect(sent.find((c) => c.capability_name === 'locked')?.access).toBe('approval_required');
                expect(sent.find((c) => c.capability_name === 'free')?.access).toBe('enabled');
            },
        );
    });

    it('Disable All affects only the active tab', async () => {
        gateway.getMCPCapabilityFilters.mockResolvedValue({
            has_filters: false,
            capabilities: [],
            discovered_capabilities: [tool('t1'), { capability_type: 'prompt', name: 'p1' }],
        });
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Disable All' }));
        save();
        await waitFor(() => expect(gateway.updateMCPCapabilityFilters).toHaveBeenCalled());
        expect(gateway.updateMCPCapabilityFilters.mock.calls[0][2]).toEqual([
            { capability_type: 'tool', capability_name: 't1', access: 'disabled' },
            { capability_type: 'prompt', capability_name: 'p1', access: 'enabled' },
        ]);
        expect(toast.success).toHaveBeenCalledWith('Capability filters saved (1 enabled, 1 disabled)');
    });

    it('adds a capability manually under the active tab, refusing duplicates and removing on request', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: /Resources/ }));
        expect(screen.getByText(/No resources discovered/)).toBeInTheDocument();

        const input = screen.getByRole('textbox');
        expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
        fireEvent.change(input, { target: { value: '  file://x  ' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(screen.getByText('file://x')).toBeInTheDocument();
        expect((input as HTMLInputElement).value).toBe('');

        fireEvent.change(input, { target: { value: 'file://x' } });
        fireEvent.click(screen.getByRole('button', { name: 'Add' }));
        expect(toast.error).toHaveBeenCalledWith('Capability "file://x" already exists');
        expect(screen.getAllByText('file://x')).toHaveLength(1);

        pick('file://x', 'D');
        save();
        await waitFor(() => expect(gateway.updateMCPCapabilityFilters).toHaveBeenCalled());
        expect(gateway.updateMCPCapabilityFilters.mock.calls[0][2]).toContainEqual({
            capability_type: 'resource',
            capability_name: 'file://x',
            access: 'disabled',
        });

        fireEvent.click(within(row('file://x')).getByTitle('Remove'));
        expect(screen.queryByText('file://x')).toBeNull();
    });

    it('a failed save stays open and shows why', async () => {
        await open();
        pick('delete_repo', 'D');
        gateway.updateMCPCapabilityFilters.mockRejectedValue(new Error('group is locked'));
        save();
        expect(await screen.findByText('group is locked')).toBeInTheDocument();
        expect(onClose).not.toHaveBeenCalled();
        expect(toast.success).not.toHaveBeenCalled();
    });

    it('a failed clear stays open and shows why', async () => {
        await open();
        gateway.clearMCPCapabilityFilters.mockRejectedValue({});
        save();
        expect(await screen.findByText('Failed to save capability filters')).toBeInTheDocument();
        expect(onClose).not.toHaveBeenCalled();
    });

    it('a failed load is shown', async () => {
        gateway.getMCPCapabilityFilters.mockRejectedValue(new Error('offline'));
        render(<MCPCapabilityFilterDialog isOpen onClose={onClose} groupId="rg-1" groupName="Eng" server={SERVER} />);
        expect(await screen.findByText('offline')).toBeInTheDocument();
    });

    it.each([
        ['oauth', 'OAuth authentication'],
        ['none', 'Ensure the MCP server is running and reachable'],
    ])('with nothing discovered, explains discovery for auth type %s', async (auth, expected) => {
        gateway.getMCPCapabilityFilters.mockResolvedValue({
            has_filters: false,
            capabilities: [],
            discovered_capabilities: [],
        });
        await open({ id: 'srv-2', name: 'X', mcp_server: { auth_type: auth } } as unknown as ServerConfigInfo);
        expect(screen.getByText('No capabilities discovered yet')).toBeInTheDocument();
        expect(screen.getByText(expected)).toBeInTheDocument();
    });

    it('Cancel closes without saving', async () => {
        await open();
        pick('delete_repo', 'D');
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(onClose).toHaveBeenCalledTimes(1);
        expect(gateway.updateMCPCapabilityFilters).not.toHaveBeenCalled();
    });
});

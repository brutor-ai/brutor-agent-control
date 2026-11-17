/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Virtual MCP servers tab: who may act on a VMCP, what reaches the gateway on
 * toggle / create / edit / delete, and what the operator sees on failure.
 *
 * Also pinned: a capabilities object may carry the normaliser's non-array
 * siblings (source_mapping and friends), which once crashed the card, and a save
 * returning `poisoned_tools` must interrupt the operator. The form
 * and delete dialog are stubs exposing their callbacks.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';

import type { VirtualServerConfig, MCPServerConfig } from '../../../../lib/types';

const gateway = {
    users: [] as unknown[],
    usersLoaded: true,
    fetchUsers: vi.fn(),
    getUsernameById: vi.fn(),
    getUserDisplayName: vi.fn(),
    getUserRoles: vi.fn(),
    fetchServers: vi.fn(),
    fetchVirtualServers: vi.fn(),
    createVirtualServer: vi.fn(),
    updateVirtualServer: vi.fn(),
    deleteVirtualServer: vi.fn(),
};
const auth = {
    user: { id: 'me', username: 'me-user', full_name: 'Me Myself' } as
        { id: string; username: string; full_name: string } | null,
    hasPermission: vi.fn<(permission: string) => boolean>(),
};
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

interface FormProps {
    virtualServer: VirtualServerConfig | null;
    availableServers: MCPServerConfig[];
    onSave: (data: Omit<VirtualServerConfig, 'created_at'>) => Promise<void>;
    onCancel: () => void;
}
const captured: { form: FormProps | null } = { form: null };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../hooks/useAuth', () => ({ useAuth: () => auth }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../VirtualServerForm', () => ({
    default: (props: FormProps) => {
        captured.form = props;
        return (
            <div>
                <span>form:{props.virtualServer ? props.virtualServer.id : 'new'}</span>
                <button onClick={props.onCancel}>cancel form</button>
            </div>
        );
    },
}));
vi.mock('../../../common/GenericDeleteConfirmationModal', () => ({
    default: ({ isOpen, itemName, itemInfo, errorMessage, onConfirm, onClose }: {
        isOpen: boolean; itemName: string; itemInfo?: { owner?: string; capabilityCount?: number };
        errorMessage?: string | null; onConfirm: () => void; onClose: () => void;
    }) => (isOpen ? (
        <div>
            <span>confirm-delete:{itemName}:{itemInfo?.owner}:{itemInfo?.capabilityCount}</span>
            {errorMessage && <span>delete-error:{errorMessage}</span>}
            <button onClick={onConfirm}>confirm delete</button>
            <button onClick={onClose}>cancel delete</button>
        </div>
    ) : null),
}));

import VirtualServersTab from '../VirtualServersTab';

function vmcp(id: string, overrides: Record<string, unknown> = {}): VirtualServerConfig {
    return {
        id,
        name: `VMCP ${id}`,
        description: `about ${id}`,
        enabled: true,
        owner: 'me',
        created_at: '2026-03-15T12:00:00Z',
        capabilities: { tools: [], resources: [], prompts: [] },
        ...overrides,
    };
}
const upstream = { id: 'up1', name: 'Upstream One' } as unknown as MCPServerConfig;

async function renderTab(servers: VirtualServerConfig[], available: MCPServerConfig[] = [upstream]) {
    gateway.fetchVirtualServers.mockResolvedValue(servers);
    gateway.fetchServers.mockResolvedValue(available);
    render(<VirtualServersTab />);
    await screen.findByText(/configurations$/);
    // The header renders before the rows: an effect builds them from the loaded list.
    if (servers.length > 0) {
        await waitFor(() => expect(servers.some((s) => screen.queryAllByText(s.name).length > 0)).toBe(true));
    }
}

describe('VirtualServersTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
        captured.form = null;
        gateway.users = [];
        gateway.usersLoaded = true;
        gateway.fetchUsers.mockResolvedValue([]);
        gateway.getUsernameById.mockImplementation((id: string) => `${id}-name`);
        gateway.getUserDisplayName.mockImplementation((id: string) => `${id} Display`);
        gateway.getUserRoles.mockReturnValue([]);
        auth.user = { id: 'me', username: 'me-user', full_name: 'Me Myself' };
        auth.hasPermission.mockImplementation((permission) => permission === 'virtual:server:update' || permission === 'virtual:server:delete');
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('loading', () => {
        it('fetches both lists itself when no props are given, and shows the card', async () => {
            await renderTab([vmcp('a', {
                capabilities: {
                    tools: [
                        { capability_name: 't1', server_id: 'up1' },
                        { capability_name: 't2', server_id: 'gone' },
                        { capability_name: 't3', server_id: 'up1' },
                    ],
                    resources: [{ capability_name: 'r1', server_id: 'up1' }],
                    prompts: [],
                    // The normaliser adds non-array siblings: they must not break the card.
                    source_mapping: { t1: 'up1' },
                    tool_name_prefix: 'x',
                },
            })]);
            expect(screen.getByText('1 of 1 configurations')).toBeTruthy();
            expect(screen.getByText('VMCP a')).toBeTruthy();
            expect(screen.getByText(/• t1/)).toBeTruthy();
            expect(screen.queryByText(/• t3/)).toBeNull();
            expect(screen.getByText('+1 more')).toBeTruthy();
            // Source servers resolve to names, falling back to the raw id.
            expect(screen.getAllByText(/from Upstream One/).length).toBe(2);
            expect(screen.getByText('from gone')).toBeTruthy();
            expect(screen.getByText('(You)')).toBeTruthy();
            expect(screen.getByText('me Display')).toBeTruthy();
            expect(screen.getByText('Mar 15, 2026')).toBeTruthy();
        });

        it('uses props without fetching when they are given', async () => {
            render(<VirtualServersTab virtualServers={[vmcp('p')]} availableServers={[upstream]} />);
            expect(await screen.findByText('VMCP p')).toBeTruthy();
            expect(gateway.fetchVirtualServers).not.toHaveBeenCalled();
            expect(gateway.fetchServers).not.toHaveBeenCalled();
        });

        it('with no upstream servers, cannot create, and says why', async () => {
            await renderTab([], []);
            expect(screen.getByText('No MCP Servers Available')).toBeTruthy();
            expect(screen.getByText('No virtual servers configured')).toBeTruthy();
            expect(screen.getByText('Add First Virtual Server').closest('button')?.disabled).toBe(true);
            expect(screen.getByText('Add Virtual Server').closest('button')?.disabled).toBe(true);
        });

        it('fetches users only when they are not loaded, and shows Loading for others until then', async () => {
            gateway.usersLoaded = false;
            await renderTab([vmcp('mine'), vmcp('theirs', { owner: 'other' })]);
            expect(gateway.fetchUsers).toHaveBeenCalledTimes(1);
            expect(screen.getByText('Me Myself')).toBeTruthy();
            expect(screen.getAllByText('Loading...').length).toBeGreaterThan(0);
        });

        it('a failed load leaves an empty, usable screen', async () => {
            gateway.fetchVirtualServers.mockRejectedValue(new Error('down'));
            gateway.fetchServers.mockResolvedValue([]);
            render(<VirtualServersTab />);
            expect(await screen.findByText('No virtual servers configured')).toBeTruthy();
        });
    });

    describe('access', () => {
        it('an administrator with neither permission gets view only, with no action that could mutate', async () => {
            auth.hasPermission.mockReturnValue(false);
            await renderTab([vmcp('a')]);
            expect(screen.getByText('View only')).toBeTruthy();
            expect(screen.queryByTitle('Disable')).toBeNull();
            expect(screen.queryByTitle('Edit')).toBeNull();
            expect(screen.queryByTitle('Delete')).toBeNull();
        });

        it('asks for the permissions the Control Plane asks for, and for no role', async () => {
            await renderTab([vmcp('a')]);
            const asked = new Set(auth.hasPermission.mock.calls.map(([permission]) => permission));
            expect([...asked].sort()).toEqual(['virtual:server:delete', 'virtual:server:update']);
        });

        it('virtual:server:update alone enables, disables and edits, and does not delete', async () => {
            auth.hasPermission.mockImplementation((permission) => permission === 'virtual:server:update');
            await renderTab([vmcp('a')]);
            expect(screen.getByTitle('Disable')).toBeTruthy();
            expect(screen.getByTitle('Edit')).toBeTruthy();
            expect(screen.queryByTitle('Delete')).toBeNull();
            expect(screen.queryByText('View only')).toBeNull();
        });

        it('virtual:server:delete alone deletes, and neither edits nor switches', async () => {
            auth.hasPermission.mockImplementation((permission) => permission === 'virtual:server:delete');
            await renderTab([vmcp('a')]);
            expect(screen.getByTitle('Delete')).toBeTruthy();
            expect(screen.queryByTitle('Edit')).toBeNull();
            expect(screen.queryByTitle('Disable')).toBeNull();
        });

        it('no signed-in user is view only, even if a permission check would pass', async () => {
            auth.user = null;
            await renderTab([vmcp('a')]);
            expect(screen.getByText('View only')).toBeTruthy();
        });

        it.each([
            ['super_admin', 'Admin'],
            ['virtual_server_admin', 'VS Admin'],
            ['virtual_server_manager', 'Manager'],
        ])('owner role %s shows the %s badge', async (role, badge) => {
            gateway.getUserRoles.mockReturnValue([role]);
            await renderTab([vmcp('a')]);
            expect(screen.getByText(badge)).toBeTruthy();
        });
    });

    describe('filters', () => {
        const estate = [vmcp('a', { description: 'billing' }), vmcp('b', { owner: 'other', enabled: false })];

        it('search covers description and owner name; a miss says so', async () => {
            await renderTab(estate);
            const box = screen.getByPlaceholderText(/Search virtual servers/);
            fireEvent.change(box, { target: { value: 'BILLING' } });
            expect(screen.getByText('1 of 2 configurations')).toBeTruthy();
            expect(screen.getByText('VMCP a')).toBeTruthy();
            fireEvent.change(box, { target: { value: 'other Display' } });
            expect(screen.getByText('VMCP b')).toBeTruthy();
            expect(screen.queryByText('VMCP a')).toBeNull();
            fireEvent.change(box, { target: { value: 'zzz' } });
            expect(screen.getByText(/Try adjusting your search/)).toBeTruthy();
            expect(screen.queryByText('Add First Virtual Server')).toBeNull();
        });

        it.each([
            [0, 'enabled', 'a', 'b'],
            [0, 'disabled', 'b', 'a'],
            [1, 'mine', 'a', 'b'],
            [1, 'others', 'b', 'a'],
        ])('select %i value %s keeps %s and hides %s', async (index, value, kept, hidden) => {
            await renderTab(estate);
            fireEvent.change(screen.getAllByRole('combobox')[index], { target: { value } });
            expect(screen.getByText(`VMCP ${kept}`)).toBeTruthy();
            expect(screen.queryByText(`VMCP ${hidden}`)).toBeNull();
        });
    });

    describe('toggle', () => {
        it('sends the inverse, refetches and names the outcome', async () => {
            await renderTab([vmcp('a', { enabled: false })]);
            gateway.updateVirtualServer.mockResolvedValue({});
            fireEvent.click(screen.getByTitle('Enable'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Virtual Server Enabled', 'VMCP a has been enabled.'));
            expect(gateway.updateVirtualServer).toHaveBeenCalledWith('a', { enabled: true });
            expect(gateway.fetchVirtualServers).toHaveBeenCalledTimes(2);
        });

        it('a failure shows in the error list, does not claim success, and can be cleared', async () => {
            await renderTab([vmcp('a')]);
            gateway.updateVirtualServer.mockRejectedValue(new Error('locked'));
            fireEvent.click(screen.getByTitle('Disable'));
            expect(await screen.findByText('1 error in virtual server operations')).toBeTruthy();
            expect(gateway.updateVirtualServer).toHaveBeenCalledWith('a', { enabled: false });
            expect(toast.success).not.toHaveBeenCalled();
            fireEvent.click(screen.getByText('Clear All Errors'));
            expect(screen.queryByText('1 error in virtual server operations')).toBeNull();
        });

        it('uses the injected handler and does not refetch when props drive the list', async () => {
            const onUpdate = vi.fn().mockResolvedValue(undefined);
            render(<VirtualServersTab virtualServers={[vmcp('a')]} availableServers={[upstream]} onUpdateVirtualServer={onUpdate} />);
            fireEvent.click(await screen.findByTitle('Disable'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
            expect(onUpdate).toHaveBeenCalledWith('a', { enabled: false });
            expect(gateway.updateVirtualServer).not.toHaveBeenCalled();
            expect(gateway.fetchVirtualServers).not.toHaveBeenCalled();
        });
    });

    describe('save', () => {
        const data = vmcp('a', { name: 'Renamed', owner: 'other' });

        it('create passes the form data through, reloads and closes', async () => {
            await renderTab([]);
            fireEvent.click(screen.getByText('Add First Virtual Server'));
            expect(screen.getByText('form:new')).toBeTruthy();
            expect(captured.form?.availableServers).toEqual([upstream]);
            gateway.createVirtualServer.mockResolvedValue({ id: 'a' });

            await act(async () => { await captured.form?.onSave(data); });

            expect(gateway.createVirtualServer).toHaveBeenCalledWith(data);
            expect(toast.success).toHaveBeenCalledWith('Virtual Server Created', 'Renamed has been created successfully.');
            expect(gateway.fetchVirtualServers).toHaveBeenCalledTimes(2);
            expect(screen.queryByText('form:new')).toBeNull();
        });

        it('edit sends only the editable fields, and closes', async () => {
            await renderTab([vmcp('a')]);
            fireEvent.click(screen.getByTitle('Edit'));
            expect(screen.getByText('form:a')).toBeTruthy();
            gateway.updateVirtualServer.mockResolvedValue({});

            await act(async () => { await captured.form?.onSave(data); });

            expect(gateway.updateVirtualServer).toHaveBeenCalledWith('a', {
                name: 'Renamed',
                description: 'about a',
                capabilities: data.capabilities,
                enabled: true,
                owner: 'other',
            });
            expect(toast.success).toHaveBeenCalledWith('Virtual Server Updated', 'Renamed has been updated successfully.');
            expect(screen.queryByText('form:a')).toBeNull();
        });

        it('a failed save rethrows to the form, which stays open', async () => {
            await renderTab([vmcp('a')]);
            fireEvent.click(screen.getByText('Add Virtual Server'));
            gateway.createVirtualServer.mockRejectedValue(new Error('name taken'));

            await act(async () => {
                await expect(captured.form?.onSave(data)).rejects.toThrow('name taken');
            });

            expect(screen.getByText('form:new')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();
            fireEvent.click(screen.getByText('cancel form'));
            expect(screen.queryByText('form:new')).toBeNull();
        });

        it('an edit without an id is refused before reaching the API', async () => {
            await renderTab([vmcp('a')]);
            fireEvent.click(screen.getByTitle('Edit'));
            await act(async () => {
                await expect(captured.form?.onSave(vmcp('', { id: undefined }))).rejects.toThrow('Virtual server ID is required');
            });
            expect(gateway.updateVirtualServer).not.toHaveBeenCalled();
        });

        it('poisoned overrides interrupt the operator', async () => {
            await renderTab([vmcp('a')]);
            fireEvent.click(screen.getByTitle('Edit'));
            gateway.updateVirtualServer.mockResolvedValue({
                poisoned_tools: [{
                    tool_name: 'renamed_tool',
                    field_kind: 'name',
                    description_excerpt: 'ignore previous',
                    description_truncated: false,
                    confidence: 0.8,
                    patterns_matched: 1,
                    suspicious_indicator_count: 2,
                }],
            });

            await act(async () => { await captured.form?.onSave(data); });

            expect(screen.getByText('renamed_tool')).toBeTruthy();
            expect(screen.getByText(/ignore previous/)).toBeTruthy();
            fireEvent.click(screen.getByText('Acknowledge'));
            expect(screen.queryByText('renamed_tool')).toBeNull();
        });
    });

    describe('delete', () => {
        const withCaps = vmcp('a', { capabilities: { tools: [{ capability_name: 't', server_id: 'up1' }], resources: [], prompts: [] } });

        it('asks first, naming owner and capability count, then deletes', async () => {
            await renderTab([withCaps]);
            fireEvent.click(screen.getByTitle('Delete'));
            expect(await screen.findByText('confirm-delete:VMCP a:me Display:1')).toBeTruthy();
            expect(gateway.deleteVirtualServer).not.toHaveBeenCalled();

            gateway.deleteVirtualServer.mockResolvedValue(undefined);
            gateway.fetchVirtualServers.mockResolvedValue([]);
            fireEvent.click(screen.getByText('confirm delete'));

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Virtual Server Deleted', 'VMCP a has been deleted successfully.'));
            expect(gateway.deleteVirtualServer).toHaveBeenCalledWith('a');
            expect(await screen.findByText('No virtual servers configured')).toBeTruthy();
            expect(screen.queryByText(/^confirm-delete/)).toBeNull();
        });

        it('cancel does nothing', async () => {
            await renderTab([withCaps]);
            fireEvent.click(screen.getByTitle('Delete'));
            fireEvent.click(await screen.findByText('cancel delete'));
            expect(screen.queryByText(/^confirm-delete/)).toBeNull();
            expect(gateway.deleteVirtualServer).not.toHaveBeenCalled();
        });

        it('a failure stays in the dialog, preferring the API detail', async () => {
            await renderTab([withCaps]);
            fireEvent.click(screen.getByTitle('Delete'));
            await screen.findByText(/^confirm-delete/);
            gateway.deleteVirtualServer.mockRejectedValue(Object.assign(new Error('409'), { detail: 'bound to an access config' }));
            fireEvent.click(screen.getByText('confirm delete'));
            expect(await screen.findByText('delete-error:bound to an access config')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();
            expect(screen.getByText('VMCP a')).toBeTruthy();
        });
    });

    describe('refresh', () => {
        it('refetches lists and users', async () => {
            await renderTab([vmcp('a')]);
            gateway.fetchVirtualServers.mockResolvedValue([vmcp('a'), vmcp('b')]);
            fireEvent.click(screen.getByText('Refresh'));
            expect(await screen.findByText('VMCP b')).toBeTruthy();
            expect(gateway.fetchUsers).toHaveBeenCalledTimes(1);
            expect(gateway.fetchServers).toHaveBeenCalledTimes(2);
        });

        it('prefers the injected refresh', async () => {
            const onRefresh = vi.fn().mockResolvedValue(undefined);
            render(<VirtualServersTab virtualServers={[vmcp('a')]} availableServers={[upstream]} onRefresh={onRefresh} />);
            fireEvent.click(await screen.findByText('Refresh'));
            await waitFor(() => expect(gateway.fetchUsers).toHaveBeenCalledTimes(1));
            expect(onRefresh).toHaveBeenCalledTimes(1);
            expect(gateway.fetchVirtualServers).not.toHaveBeenCalled();
        });

        it('a failed refresh surfaces in the error list and keeps the servers on screen', async () => {
            await renderTab([vmcp('a')]);
            gateway.fetchVirtualServers.mockRejectedValueOnce(new Error('flaky'));
            fireEvent.click(screen.getByText('Refresh'));
            expect(await screen.findByText('1 error in virtual server operations')).toBeTruthy();
            expect(screen.getByText('VMCP a')).toBeTruthy();
        });
    });
});

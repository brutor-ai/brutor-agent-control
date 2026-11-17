/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Server access configs tab: what binds MCP / VMCP servers to resource groups.
 *
 * Pinned: the form is offered only active OAuth clients and enabled VMCPs, save
 * routes to create or update and rejects back to the form, and delete failures
 * stay in the dialog. A VMCP-backed config has no mcp_server_id; its card
 * names the virtual server rather than showing a blank, as the list view does.
 * The real row is used so this is what the operator actually reads.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';

const gateway = {
    fetchServerConfigs: vi.fn(),
    createServerConfig: vi.fn(),
    updateServerConfig: vi.fn(),
    deleteServerConfig: vi.fn(),
    fetchServers: vi.fn(),
    fetchVirtualServers: vi.fn(),
    fetchOAuthAppClients: vi.fn(),
    fetchGroupHierarchyTree: vi.fn(),
    getServerIconUrl: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

interface Named { id: string; name: string }
interface FormProps {
    config: { id: string } | null;
    servers: Named[];
    virtualServers: Named[];
    oauthClients: Named[];
    groupTree: unknown[];
    onSave: (data: Record<string, unknown>) => Promise<void>;
    onCancel: () => void;
}
const captured: { form: FormProps | null } = { form: null };

vi.mock('../../../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../ServerConfigForm', () => ({
    default: (props: FormProps) => {
        captured.form = props;
        return (
            <div>
                <span>form:{props.config ? props.config.id : 'new'}</span>
                <button onClick={props.onCancel}>cancel form</button>
            </div>
        );
    },
}));
vi.mock('../ServerConfigListView', () => ({
    default: ({ configs, onEdit, onDelete }: {
        configs: Named[]; onEdit: (c: Named) => void; onDelete: (id: string) => void;
    }) => (
        <div>
            <span>list-view:{configs.map(c => c.id).join(',')}</span>
            <button onClick={() => onEdit(configs[0])}>list edit</button>
            <button onClick={() => onDelete('missing')}>list delete missing</button>
        </div>
    ),
}));
vi.mock('../../../../../common/GenericDeleteConfirmationModal', () => ({
    default: ({ itemName, itemInfo, customNote, errorMessage, onConfirm, onClose }: {
        itemName: string; itemInfo?: { description?: string }; customNote?: string;
        errorMessage?: string | null; onConfirm: () => void; onClose: () => void;
    }) => (
        <div>
            <span>confirm-delete:{itemName}</span>
            <span>delete-info:{itemInfo?.description}</span>
            <span>delete-note:{customNote}</span>
            {errorMessage && <span>delete-error:{errorMessage}</span>}
            <button onClick={onConfirm}>confirm delete</button>
            <button onClick={onClose}>cancel delete</button>
        </div>
    ),
}));

import ServerConfigTab from '../ServerConfigTab';

function config(id: string, overrides: Record<string, unknown> = {}) {
    return {
        id,
        name: `Config ${id}`,
        description: '',
        mcp_server_id: 'mcp1',
        oauth_client_id: null,
        virtual_server_id: null,
        enabled: true,
        is_default: false,
        priority: 5,
        usage_count: 3,
        groups: [],
        ...overrides,
    };
}

function backend(configs: unknown[]) {
    gateway.fetchServerConfigs.mockResolvedValue({ configs });
    gateway.fetchServers.mockResolvedValue([{ id: 'mcp1', name: 'Real MCP' }]);
    gateway.fetchVirtualServers.mockResolvedValue([
        { id: 'v1', name: 'Enabled VMCP', enabled: true },
        { id: 'v2', name: 'Disabled VMCP', enabled: false },
    ]);
    gateway.fetchOAuthAppClients.mockResolvedValue({
        clients: [{ id: 'c1', name: 'Live Client', is_active: true }, { id: 'c2', name: 'Dead Client', is_active: false }],
    });
    gateway.fetchGroupHierarchyTree.mockResolvedValue({ tree: [{ id: 'g1' }] });
}

async function renderConfigs(configs: unknown[] = [config('a')]) {
    backend(configs);
    render(<ServerConfigTab />);
    await screen.findByText('MCP / VMCP Server Access Configurations', { exact: false });
}

describe('ServerConfigTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
        localStorage.clear();
        captured.form = null;
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('loading', () => {
        it('a failure shows inline and as a toast, and the inline error can be dismissed', async () => {
            backend([]);
            gateway.fetchServers.mockRejectedValue(new Error('down'));
            render(<ServerConfigTab />);
            expect(await screen.findByText('down')).toBeTruthy();
            expect(toast.error).toHaveBeenCalledWith('Failed to Load Server Configs', 'down');
            fireEvent.click(screen.getByText('×'));
            expect(screen.queryByText('down')).toBeNull();
        });

        it('an empty estate invites a first config', async () => {
            await renderConfigs([]);
            expect(screen.getByText('No server configurations created')).toBeTruthy();
        });

        it('Refresh refetches everything', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByText('Refresh'));
            await waitFor(() => expect(gateway.fetchServerConfigs).toHaveBeenCalledTimes(2));
            expect(gateway.fetchGroupHierarchyTree).toHaveBeenCalledTimes(2);
        });
    });

    describe('backend name on the card', () => {
        const nameOnCard = () => screen.getByText('MCP Server').nextElementSibling?.textContent;

        it('resolves an MCP-backed config by id', async () => {
            await renderConfigs();
            expect(nameOnCard()).toBe('Real MCP');
        });

        it('prefers the embedded server name', async () => {
            await renderConfigs([config('a', { mcp_server: { name: 'Embedded' } })]);
            expect(nameOnCard()).toBe('Embedded');
        });

        it('resolves a VMCP-backed config, which has no mcp_server_id', async () => {
            await renderConfigs([config('a', { mcp_server_id: null, virtual_server_id: 'v1' })]);
            expect(nameOnCard()).toBe('Enabled VMCP');
        });
    });

    describe('filters', () => {
        const estate = [
            config('a', { name: 'Alpha', oauth_client_id: 'c1' }),
            config('b', { name: 'Beta', mcp_server_id: 'other', enabled: false }),
            config('v', { name: 'Vee', mcp_server_id: null, virtual_server_id: 'v1' }),
        ];

        it('search covers config, backend (including VMCP) and client names', async () => {
            await renderConfigs(estate);
            const box = screen.getByPlaceholderText(/Search by name, server/);
            for (const [term, shown] of [['alpha', 'Alpha'], ['Enabled VMCP', 'Vee'], ['Live Client', 'Alpha']]) {
                fireEvent.change(box, { target: { value: term } });
                expect(screen.getByText(shown)).toBeTruthy();
                expect(screen.queryByText('Beta')).toBeNull();
            }
            fireEvent.change(box, { target: { value: 'zzz' } });
            expect(screen.getByText('No configurations found')).toBeTruthy();
        });

        it('server and status selects narrow the list', async () => {
            await renderConfigs(estate);
            const [server, status] = screen.getAllByRole('combobox');
            fireEvent.change(server, { target: { value: 'mcp1' } });
            expect(screen.getByText('Alpha')).toBeTruthy();
            expect(screen.queryByText('Beta')).toBeNull();
            expect(screen.queryByText('Vee')).toBeNull();
            fireEvent.change(server, { target: { value: 'all' } });
            fireEvent.change(status, { target: { value: 'disabled' } });
            expect(screen.getByText('Beta')).toBeTruthy();
            expect(screen.queryByText('Alpha')).toBeNull();
            fireEvent.change(status, { target: { value: 'enabled' } });
            expect(screen.queryByText('Beta')).toBeNull();
        });

        it.todo('BUG: the server filter lists only MCP servers although it also matches VMCP ids, so VMCP-backed configs cannot be filtered by backend (ServerConfigTab.tsx server select)');
    });

    describe('view mode', () => {
        it('switches to the list and remembers it', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByTitle('List View'));
            expect(screen.getByText('list-view:a')).toBeTruthy();
            cleanup();

            await renderConfigs();
            expect(screen.getByText('list-view:a')).toBeTruthy();
            fireEvent.click(screen.getByTitle('Card View'));
            expect(screen.queryByText(/^list-view/)).toBeNull();
        });

        it('deleting an id that is gone reports it rather than opening the dialog', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByTitle('List View'));
            fireEvent.click(screen.getByText('list delete missing'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Config Not Found', expect.any(String)));
            expect(screen.queryByText(/^confirm-delete/)).toBeNull();
        });
    });

    describe('save', () => {
        it('offers the form only enabled VMCPs and active clients, plus the tree', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByText('Create Server Config'));
            expect(screen.getByText('form:new')).toBeTruthy();
            expect(captured.form?.virtualServers.map(v => v.id)).toEqual(['v1']);
            expect(captured.form?.oauthClients.map(c => c.id)).toEqual(['c1']);
            expect(captured.form?.servers.map(s => s.id)).toEqual(['mcp1']);
            expect(captured.form?.groupTree).toEqual([{ id: 'g1' }]);
        });

        it('create: calls the API with the form data, closes and reloads', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByText('Create Server Config'));
            const data = { name: 'New', mcp_server_id: 'mcp1' };
            gateway.createServerConfig.mockResolvedValue({});
            await act(async () => { await captured.form?.onSave(data); });
            expect(gateway.createServerConfig).toHaveBeenCalledWith(data);
            expect(gateway.updateServerConfig).not.toHaveBeenCalled();
            expect(toast.success).toHaveBeenCalledWith('Server Config Created', 'New has been created successfully.');
            expect(screen.queryByText('form:new')).toBeNull();
            expect(gateway.fetchServerConfigs).toHaveBeenCalledTimes(2);
        });

        // The Control Plane's update of a server config names no backend and
        // no OAuth client (they are fixed when the config is created) and
        // refuses a field it does not name.
        it('edit: sends what an update may change, and neither the backend nor the OAuth client', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByTitle('Edit'));
            gateway.updateServerConfig.mockResolvedValue({});
            await act(async () => {
                await captured.form?.onSave({ mcp_server_id: 'srv-1', virtual_server_id: 'v-1', oauth_client_id: 'oc-1', name: 'Renamed', group_ids: ['g1'], priority: 5, is_default: false, enabled: true });
            });
            expect(gateway.updateServerConfig).toHaveBeenCalledWith('a', { name: 'Renamed', group_ids: ['g1'], priority: 5, is_default: false, enabled: true });
        });

        it('edit: updates by the edited id', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByTitle('Edit'));
            expect(screen.getByText('form:a')).toBeTruthy();
            gateway.updateServerConfig.mockResolvedValue({});
            await act(async () => { await captured.form?.onSave({ name: 'Renamed' }); });
            expect(gateway.updateServerConfig).toHaveBeenCalledWith('a', { name: 'Renamed' });
            expect(toast.success).toHaveBeenCalledWith('Server Config Updated', 'Renamed has been updated successfully.');
            expect(screen.queryByText('form:a')).toBeNull();
        });

        it('a failure rejects to the form, which stays open with no success toast', async () => {
            await renderConfigs();
            fireEvent.click(screen.getByText('Create Server Config'));
            gateway.createServerConfig.mockRejectedValue(new Error('conflict'));
            await act(async () => {
                await expect(captured.form?.onSave({ name: 'x' })).rejects.toThrow('conflict');
            });
            expect(screen.getByText('form:new')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();
            fireEvent.click(screen.getByText('cancel form'));
            expect(screen.queryByText('form:new')).toBeNull();
        });
    });

    describe('delete', () => {
        const openDialog = async () => {
            await renderConfigs([config('a', { oauth_client_id: 'c1' })]);
            fireEvent.click(screen.getByTitle('Delete'));
            await screen.findByText('confirm-delete:Config a');
        };

        it('the dialog shows the binding and its usage before anything is deleted', async () => {
            await openDialog();
            expect(screen.getByText(/^delete-info:Real MCP → Live Client/)).toBeTruthy();
            expect(screen.getByText(/^delete-note:.*3/)).toBeTruthy();
            expect(gateway.deleteServerConfig).not.toHaveBeenCalled();
            fireEvent.click(screen.getByText('cancel delete'));
            expect(screen.queryByText(/^confirm-delete/)).toBeNull();
        });

        it('deletes by id, reloads and confirms', async () => {
            await openDialog();
            gateway.deleteServerConfig.mockResolvedValue({});
            fireEvent.click(screen.getByText('confirm delete'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Server Config Deleted', 'Config a has been deleted successfully.'));
            expect(gateway.deleteServerConfig).toHaveBeenCalledWith('a');
            expect(gateway.fetchServerConfigs).toHaveBeenCalledTimes(2);
            await waitFor(() => expect(screen.queryByText(/^confirm-delete/)).toBeNull());
        });

        it('a failure stays in the dialog, preferring the API detail', async () => {
            await openDialog();
            gateway.deleteServerConfig.mockRejectedValue(Object.assign(new Error('409'), { detail: 'in use by a group' }));
            fireEvent.click(screen.getByText('confirm delete'));
            expect(await screen.findByText('delete-error:in use by a group')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();
        });
    });
});

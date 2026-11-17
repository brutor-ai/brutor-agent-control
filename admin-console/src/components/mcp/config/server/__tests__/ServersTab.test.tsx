/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCP Servers tab — the operator's registration and lifecycle screen.
 *
 * Pinned here: what reaches the gateway on toggle / bulk toggle / create / edit
 * / delete / capability refresh, and what the operator is told when it fails.
 * Two paths matter most: a 409 on delete must explain the conflict rather than
 * report a generic failure, and a create response carrying `poisoned_tools` must
 * interrupt the operator immediately. ServerRow, ServerForm and the
 * modals have their own suites, so they are stubs that expose their callbacks.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';

import { env } from '../../../../../env';
import { thrownByClient } from '../../../../../test/serverAnswers';
import type { MCPServerConfig } from '../../../../../lib/types';

const gateway = {
    fetchServers: vi.fn(),
    createServer: vi.fn(),
    updateServer: vi.fn(),
    refreshServerCapabilities: vi.fn(),
    deleteServer: vi.fn(),
    errors: [] as unknown[],
    dismissError: vi.fn(),
    triggerServerHealthCheck: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };
const health = {
    isEnabled: false,
    intervalSeconds: 30,
    lastCheckTime: null as Date | null,
    isChecking: false,
    runHealthCheck: vi.fn(),
    healthCheckConfig: null as { mcp_settings?: { interval_seconds?: number } } | null,
    refreshConfig: vi.fn(),
};

interface RowProps {
    server: MCPServerConfig;
    isSelected: boolean;
    gatewayBaseUrl: string;
    onSelect: (selected: boolean) => void;
    onToggle: () => void;
    onEdit: () => void;
    onDelete: () => void;
    onHealthCheck?: (id: string) => Promise<void>;
    onRefreshCapabilities: (id: string) => Promise<void>;
    onShowOAuthInfo: (id: string) => void;
}
interface FormProps {
    server: MCPServerConfig | null;
    onSave: (data: MCPServerConfig) => Promise<unknown>;
    onCancel: () => void;
    onIconChange: () => Promise<void>;
}
const captured: { rows: Record<string, RowProps>; form: FormProps | null } = {
    rows: {},
    form: null,
};

vi.mock('../../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../../hooks/useMCPServerHealthCheck', () => ({ useMCPServerHealthCheck: () => health }));
vi.mock('../../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../ServerRow', () => ({
    default: (props: RowProps) => {
        captured.rows[props.server.id] = props;
        return (
            <tr>
                <td>
                    <span>row:{props.server.name}</span>
                    <span>{props.isSelected ? `selected:${props.server.id}` : ''}</span>
                    <button onClick={props.onToggle}>toggle {props.server.id}</button>
                    <button onClick={props.onEdit}>edit {props.server.id}</button>
                    <button onClick={props.onDelete}>delete {props.server.id}</button>
                    <button onClick={() => props.onSelect(true)}>select {props.server.id}</button>
                    <button onClick={() => props.onSelect(false)}>unselect {props.server.id}</button>
                    <button onClick={() => props.onShowOAuthInfo(props.server.id)}>oauth {props.server.id}</button>
                </td>
            </tr>
        );
    },
}));
vi.mock('../ServerForm', () => ({
    default: (props: FormProps) => {
        captured.form = props;
        return (
            <div>
                <span>form:{props.server ? props.server.id : 'new'}</span>
                <button onClick={props.onCancel}>cancel form</button>
            </div>
        );
    },
}));
vi.mock('../oauth/OAuthLoginModal', () => ({
    default: ({ server, onClose }: { server: MCPServerConfig; onClose: () => void }) => (
        <div>
            <span>oauth-modal:{server.id}</span>
            <button onClick={onClose}>close oauth</button>
        </div>
    ),
}));
vi.mock('../ServerDeletionConflictModal', () => ({
    default: ({ isOpen, serverName, errorDetail, inUse, onClose, onNavigateToVirtualServers }: {
        isOpen: boolean; serverName: string; errorDetail: string; onClose: () => void; onNavigateToVirtualServers?: () => void;
        inUse: { groups: Array<{ kind: string; rows: Array<{ name: string }> }> } | null;
    }) => (isOpen ? (
        <div>
            <span>conflict:{serverName}:{errorDetail}</span>
            {inUse?.groups.map((g) => <span key={g.kind}>in-use:{g.kind}:{g.rows.map((r) => r.name).join(',')}</span>)}
            <button onClick={onClose}>close conflict</button>
            {onNavigateToVirtualServers && <button onClick={onNavigateToVirtualServers}>go to virtual servers</button>}
        </div>
    ) : null),
}));
vi.mock('../../../../common/GenericDeleteConfirmationModal', () => ({
    default: ({ isOpen, itemName, errorMessage, onConfirm, onClose }: {
        isOpen: boolean; itemName: string; errorMessage?: string | null; onConfirm: () => void; onClose: () => void;
    }) => (isOpen ? (
        <div>
            <span>confirm-delete:{itemName}</span>
            {errorMessage && <span>delete-error:{errorMessage}</span>}
            <button onClick={onConfirm}>confirm delete</button>
            <button onClick={onClose}>cancel delete</button>
        </div>
    ) : null),
}));

import ServersTab from '../ServersTab';

function server(id: string, overrides: Partial<MCPServerConfig> = {}): MCPServerConfig {
    return {
        id,
        name: `Server ${id}`,
        description: `about ${id}`,
        base_url: `https://${id}.acme.test/mcp`,
        enabled: true,
        auth_type: 'none',
        ...overrides,
    } as unknown as MCPServerConfig;
}

async function renderLoaded(servers: MCPServerConfig[]) {
    gateway.fetchServers.mockResolvedValue(servers);
    render(<ServersTab />);
    await screen.findByText(/Manage registered MCP servers/);
}

describe('ServersTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
        gateway.errors = [];
        health.isEnabled = false;
        health.lastCheckTime = null;
        health.isChecking = false;
        health.healthCheckConfig = null;
        captured.rows = {};
        captured.form = null;
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('loading', () => {
        it('shows the count, and hands rows the proxy URL rather than the control plane', async () => {
            await renderLoaded([server('a'), server('b')]);
            expect(screen.getByText('Manage registered MCP servers (2 of 2)')).toBeTruthy();
            expect(captured.rows.a.gatewayBaseUrl).toBe(env.BRUTOR_GATEWAY_PUBLIC_URL);
        });

        it('offers the first-server path when nothing is registered, and survives a failed load', async () => {
            gateway.fetchServers.mockRejectedValue(new Error('boom'));
            render(<ServersTab />);
            expect(await screen.findByText('No servers configured')).toBeTruthy();
            fireEvent.click(screen.getByText('Add First Server'));
            expect(screen.getByText('form:new')).toBeTruthy();
        });

        it('Refresh refetches', async () => {
            await renderLoaded([server('a')]);
            gateway.fetchServers.mockResolvedValue([server('a'), server('b')]);
            fireEvent.click(screen.getByText('Refresh'));
            expect(await screen.findByText('row:Server b')).toBeTruthy();
            expect(gateway.fetchServers).toHaveBeenCalledTimes(2);
        });
    });

    describe('filters', () => {
        const estate = [
            server('alpha', { description: 'billing tools', auth_type: 'oauth' }),
            server('beta', { enabled: false, auth_type: 'bearer', capabilities: { tools: [] } } as unknown as Partial<MCPServerConfig>),
        ];

        it('search matches description and shows a no-match message', async () => {
            await renderLoaded(estate);
            const search = screen.getByPlaceholderText(/Search servers/);
            fireEvent.change(search, { target: { value: 'BILLING' } });
            expect(screen.getByText('row:Server alpha')).toBeTruthy();
            expect(screen.queryByText('row:Server beta')).toBeNull();
            expect(screen.getByText('Manage registered MCP servers (1 of 2)')).toBeTruthy();

            fireEvent.change(search, { target: { value: 'nothing-here' } });
            expect(screen.getByText('No servers match your filters')).toBeTruthy();
            expect(screen.queryByText('Add First Server')).toBeNull();
        });

        // description is nullable in the API (MCPServerInfo): a search that
        // misses name and id must not throw on null and take the screen down.
        it('search survives a server with no description', async () => {
            await renderLoaded([server('a', { description: null as unknown as string }), server('b')]);
            fireEvent.change(screen.getByPlaceholderText(/Search servers/), { target: { value: 'about b' } });
            expect(screen.getByText('row:Server b')).toBeTruthy();
            expect(screen.queryByText('row:Server a')).toBeNull();
        });

        it.each([
            ['enabled', 'alpha', 'beta'],
            ['disabled', 'beta', 'alpha'],
            ['oauth', 'alpha', 'beta'],
            ['bearer', 'beta', 'alpha'],
            ['discovered', 'beta', 'alpha'],
            ['none', 'alpha', 'beta'],
        ])('the %s filter keeps %s and hides %s', async (value, kept, hidden) => {
            await renderLoaded(estate);
            const select = screen.getAllByRole('combobox')
                .find(s => Array.from((s as HTMLSelectElement).options).some(o => o.value === value && value !== 'none'))
                ?? screen.getAllByRole('combobox')[2]; // "none" exists twice: this is the capabilities one
            fireEvent.change(select, { target: { value } });
            expect(screen.getByText(`row:Server ${kept}`)).toBeTruthy();
            expect(screen.queryByText(`row:Server ${hidden}`)).toBeNull();
        });

        it('the health filter only appears with health checks on, and treats missing status as unknown', async () => {
            health.isEnabled = true;
            health.healthCheckConfig = { mcp_settings: { interval_seconds: 45 } };
            await renderLoaded([
                server('up', { health_status: { status: 'healthy', consecutive_failures: 0 } }),
                server('never'),
                server('slow', { health_status: { status: 'degraded', consecutive_failures: 2 } }),
                server('down', { health_status: { status: 'unhealthy', consecutive_failures: 5 } }),
                server('odd', { health_status: { status: 'warning', consecutive_failures: 0 } }),
            ]);
            // The configured interval wins over the hook's default.
            expect(screen.getByText(/Health check every 45s/)).toBeTruthy();
            const select = screen.getAllByRole('combobox')[3];
            fireEvent.change(select, { target: { value: 'unknown' } });
            expect(screen.getByText('row:Server never')).toBeTruthy();
            expect(screen.queryByText('row:Server up')).toBeNull();
            fireEvent.change(select, { target: { value: 'healthy' } });
            expect(screen.getByText('row:Server up')).toBeTruthy();
            expect(screen.queryByText('row:Server never')).toBeNull();
            // One choice per status a server is stored with, and one for a status that is none of them.
            expect(Array.from((select as HTMLSelectElement).options).map((o) => [o.value, o.textContent])).toEqual([
                ['all', 'All Health'],
                ['healthy', 'Healthy'],
                ['degraded', 'Degraded'],
                ['unhealthy', 'Unhealthy'],
                ['unknown', 'Unknown'],
                ['unrecognised', 'Unknown status'],
            ]);
            const shownFor = (value: string) => {
                fireEvent.change(select, { target: { value } });
                return screen.getAllByText(/^row:Server /).map((row) => row.textContent);
            };
            expect(shownFor('degraded')).toEqual(['row:Server slow']);
            expect(shownFor('unhealthy')).toEqual(['row:Server down']);
            expect(shownFor('unknown')).toEqual(['row:Server never']);
            expect(shownFor('unrecognised')).toEqual(['row:Server odd']);
            expect(shownFor('all')).toHaveLength(5);
        });
    });

    describe('toggle', () => {
        it('sends the inverse of the current state and names the outcome', async () => {
            await renderLoaded([server('a', { enabled: true })]);
            gateway.updateServer.mockResolvedValue({});
            fireEvent.click(screen.getByText('toggle a'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Server Disabled', 'Server a has been disabled.'));
            expect(gateway.updateServer).toHaveBeenCalledWith('a', { enabled: false });
            expect(gateway.fetchServers).toHaveBeenCalledTimes(2);
        });

        it('reports the failure and does not claim success', async () => {
            await renderLoaded([server('a', { enabled: false })]);
            gateway.updateServer.mockRejectedValue(new Error('locked by policy'));
            fireEvent.click(screen.getByText('toggle a'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to Toggle Server', 'locked by policy'));
            expect(gateway.updateServer).toHaveBeenCalledWith('a', { enabled: true });
            expect(toast.success).not.toHaveBeenCalled();
        });
    });

    describe('bulk toggle', () => {
        it('applies to every selected server, clears the selection and pluralises', async () => {
            await renderLoaded([server('a'), server('b'), server('c')]);
            gateway.updateServer.mockResolvedValue({});
            fireEvent.click(screen.getByText('select a'));
            fireEvent.click(screen.getByText('select c'));
            expect(screen.getByText('2 servers selected')).toBeTruthy();

            fireEvent.click(screen.getByText('Disable Selected'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Servers Disabled', 'Successfully disabled 2 servers.'));
            expect(gateway.updateServer.mock.calls).toEqual([['a', { enabled: false }], ['c', { enabled: false }]]);
            expect(screen.queryByText('Enable Selected')).toBeNull();
        });

        it('select-all takes only what the filter shows, and unselecting one drops it', async () => {
            await renderLoaded([server('a', { description: 'keep' }), server('b', { description: 'other' })]);
            fireEvent.change(screen.getByPlaceholderText(/Search servers/), { target: { value: 'keep' } });
            const selectAll = screen.getAllByRole('checkbox')[0];
            fireEvent.click(selectAll);
            expect(screen.getByText('1 server selected')).toBeTruthy();
            expect(screen.getByText('selected:a')).toBeTruthy();

            fireEvent.click(selectAll);
            expect(screen.queryByText(/selected$/)).toBeNull();

            fireEvent.click(screen.getByText('select a'));
            fireEvent.click(screen.getByText('unselect a'));
            expect(screen.queryByText('Enable Selected')).toBeNull();
        });

        it('keeps the selection and reports when any update fails', async () => {
            await renderLoaded([server('a')]);
            gateway.updateServer.mockRejectedValue(new Error('nope'));
            fireEvent.click(screen.getByText('select a'));
            fireEvent.click(screen.getByText('Enable Selected'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to Enable Servers', 'nope'));
            expect(gateway.updateServer).toHaveBeenCalledWith('a', { enabled: true });
            expect(screen.getByText('1 server selected')).toBeTruthy();
        });
    });

    describe('save', () => {
        it('create goes to createServer, reloads and closes the form', async () => {
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('Add Server'));
            expect(screen.getByText('form:new')).toBeTruthy();
            gateway.createServer.mockResolvedValue({ id: 'n' });
            const data = server('n');

            await act(async () => { await captured.form?.onSave(data); });

            expect(gateway.createServer).toHaveBeenCalledWith(data);
            expect(gateway.updateServer).not.toHaveBeenCalled();
            expect(gateway.fetchServers).toHaveBeenCalledTimes(2);
            expect(screen.queryByText('form:new')).toBeNull();
            expect(screen.queryByText('Tool description poisoning detected')).toBeNull();
        });

        it('edit goes to updateServer with the form id and closes the form', async () => {
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('edit a'));
            expect(screen.getByText('form:a')).toBeTruthy();
            gateway.updateServer.mockResolvedValue({ id: 'a' });
            const data = server('a', { name: 'Renamed' });

            await act(async () => { await captured.form?.onSave(data); });

            expect(gateway.updateServer).toHaveBeenCalledWith('a', data);
            expect(gateway.createServer).not.toHaveBeenCalled();
            expect(screen.queryByText('form:a')).toBeNull();
        });

        it('a failed save leaves the form open and rethrows so the form can show it', async () => {
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('Add Server'));
            gateway.createServer.mockRejectedValue(new Error('duplicate id'));

            await act(async () => {
                await expect(captured.form?.onSave(server('a'))).rejects.toThrow('duplicate id');
            });

            expect(screen.getByText('form:new')).toBeTruthy();
            fireEvent.click(screen.getByText('cancel form'));
            expect(screen.queryByText('form:new')).toBeNull();
        });

        it('poisoned tools interrupt the operator with each finding', async () => {
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('Add Server'));
            gateway.createServer.mockResolvedValue({
                id: 'n',
                poisoned_tools: [
                    {
                        tool_name: 'steal_secrets',
                        field_kind: 'description',
                        description_excerpt: 'ignore previous instructions',
                        description_truncated: true,
                        confidence: 0.91,
                        patterns_matched: 1,
                        suspicious_indicator_count: 3,
                    },
                    {
                        tool_name: 'evil_name',
                        field_kind: 'name',
                        description_excerpt: 'x',
                        description_truncated: false,
                        confidence: 0.5,
                        patterns_matched: 2,
                        suspicious_indicator_count: 1,
                    },
                ],
            });

            await act(async () => { await captured.form?.onSave(server('n', { name: 'New One' })); });

            expect(screen.getByText('Tool description poisoning detected')).toBeTruthy();
            expect(screen.getByText(/New One — 2 tools tripped/)).toBeTruthy();
            expect(screen.getByText('steal_secrets')).toBeTruthy();
            // Each finding is labelled with the field the control plane names.
            expect(screen.getByText('description')).toBeTruthy();
            expect(screen.getByText('name')).toBeTruthy();
            expect(screen.getByText(/confidence 0\.91 · 1 pattern matched · 3 indicators/)).toBeTruthy();

            fireEvent.click(screen.getByText('Acknowledge'));
            expect(screen.queryByText('Tool description poisoning detected')).toBeNull();
        });

        it('an icon change refreshes the server being edited', async () => {
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('edit a'));
            gateway.fetchServers.mockResolvedValue([server('a', { name: 'With Icon' })]);

            await act(async () => { await captured.form?.onIconChange(); });

            expect(captured.form?.server?.name).toBe('With Icon');
        });
    });

    describe('delete', () => {
        it('nothing is deleted until confirmed, then deletes, reloads and toasts', async () => {
            await renderLoaded([server('a'), server('b')]);
            fireEvent.click(screen.getByText('delete a'));
            expect(await screen.findByText('confirm-delete:Server a')).toBeTruthy();
            expect(gateway.deleteServer).not.toHaveBeenCalled();

            gateway.deleteServer.mockResolvedValue(undefined);
            gateway.fetchServers.mockResolvedValue([server('b')]);
            fireEvent.click(screen.getByText('confirm delete'));

            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Server Deleted', 'Server a has been deleted successfully.'));
            expect(gateway.deleteServer).toHaveBeenCalledWith('a');
            expect(screen.queryByText('row:Server a')).toBeNull();
            expect(screen.queryByText('confirm-delete:Server a')).toBeNull();
        });

        it('cancel closes the dialog without calling the API', async () => {
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('delete a'));
            fireEvent.click(await screen.findByText('cancel delete'));
            expect(screen.queryByText('confirm-delete:Server a')).toBeNull();
            expect(gateway.deleteServer).not.toHaveBeenCalled();
        });

        it('a 409 says what the Control Plane answered, not a reason of the console\'s own', async () => {
            await renderLoaded([server('a'), server('b')]);
            fireEvent.click(screen.getByText('delete a'));
            await screen.findByText('confirm-delete:Server a');
            gateway.deleteServer.mockRejectedValue(Object.assign(new Error('The server is being deployed. Wait for the deployment to end.'), {
                status: 409,
            }));
            fireEvent.click(screen.getByText('confirm delete'));

            expect(await screen.findByText('conflict:Server a:The server is being deployed. Wait for the deployment to end.')).toBeTruthy();
            expect(toast.warning).toHaveBeenCalledWith('Cannot Delete Server', 'The server is being deployed. Wait for the deployment to end.');
            expect(toast.warning).not.toHaveBeenCalledWith('Cannot Delete Server', expect.stringContaining('in use by virtual servers'));
            expect(screen.queryByText('confirm-delete:Server a')).toBeNull();
            // The optimistic removal must be undone by the reload.
            expect(await screen.findByText('row:Server a')).toBeTruthy();

            fireEvent.click(screen.getByText('close conflict'));
            expect(screen.queryByText(/^conflict:/)).toBeNull();
        });

        it('a 409 that lists what uses the server hands the list to the conflict dialog', async () => {
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('delete a'));
            await screen.findByText('confirm-delete:Server a');
            gateway.deleteServer.mockRejectedValue(
                await thrownByClient(409, {
                    detail: {
                        message: 'MCP server Server a is in use: virtual servers: Support Desk.',
                        used_by: { virtual_servers: [{ id: 'vs-1', name: 'Support Desk' }] },
                    },
                }),
            );
            fireEvent.click(screen.getByText('confirm delete'));

            expect(await screen.findByText('conflict:Server a:MCP server Server a is in use: virtual servers: Support Desk.')).toBeTruthy();
            expect(screen.getByText('in-use:virtual_servers:Support Desk')).toBeTruthy();
        });

        it('the conflict offers the way to the virtual servers when the page gives one, and none otherwise', async () => {
            const refuse = () =>
                gateway.deleteServer.mockRejectedValue(Object.assign(new Error('used by vmcp-1'), { status: 409 }));

            // No way given: the conflict is explained, with no action that goes nowhere.
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('delete a'));
            await screen.findByText('confirm-delete:Server a');
            refuse();
            fireEvent.click(screen.getByText('confirm delete'));
            await screen.findByText('conflict:Server a:used by vmcp-1');
            expect(screen.queryByText('go to virtual servers')).toBeNull();
            cleanup();

            const onNavigateToVirtualServers = vi.fn();
            gateway.deleteServer.mockReset();
            render(<ServersTab onNavigateToVirtualServers={onNavigateToVirtualServers} />);
            await screen.findByText('row:Server a');
            fireEvent.click(screen.getByText('delete a'));
            await screen.findByText('confirm-delete:Server a');
            refuse();
            fireEvent.click(screen.getByText('confirm delete'));
            await screen.findByText('conflict:Server a:used by vmcp-1');
            fireEvent.click(screen.getByText('go to virtual servers'));
            expect(onNavigateToVirtualServers).toHaveBeenCalledTimes(1);
        });

        it('any other failure stays in the dialog and restores the row', async () => {
            await renderLoaded([server('a'), server('b')]);
            fireEvent.click(screen.getByText('delete a'));
            await screen.findByText('confirm-delete:Server a');
            gateway.deleteServer.mockRejectedValue(new Error('db down'));
            fireEvent.click(screen.getByText('confirm delete'));

            expect(await screen.findByText('delete-error:db down')).toBeTruthy();
            expect(screen.getByText('confirm-delete:Server a')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();
            expect(await screen.findByText('row:Server a')).toBeTruthy();
        });
    });

    describe('capability refresh', () => {
        const refresh = async (result: unknown) => {
            await renderLoaded([server('a')]);
            if (result instanceof Error) gateway.refreshServerCapabilities.mockRejectedValue(result);
            else gateway.refreshServerCapabilities.mockResolvedValue(result);
            await act(async () => { await captured.rows.a.onRefreshCapabilities('a'); });
            expect(gateway.refreshServerCapabilities).toHaveBeenCalledWith('a');
        };

        it('a skipped discovery is information, not an error', async () => {
            await refresh({ discovered: false, message: 'OAuth server: login required' });
            expect(toast.info).toHaveBeenCalledWith('Capability Refresh Skipped', 'OAuth server: login required');
            expect(toast.success).not.toHaveBeenCalled();
            expect(toast.error).not.toHaveBeenCalled();
        });

        it('reports the tool count when there is no message', async () => {
            await refresh({ discovered: true, tools_count: 4 });
            expect(toast.success).toHaveBeenCalledWith('Capabilities Refreshed', 'Server a: 4 tools.');
            expect(toast.warning).not.toHaveBeenCalled();
        });

        it('flags drift in the title and warns about poisoned tools', async () => {
            await refresh({ discovered: true, message: '2 changed', drift_detected: true, poisoned_tools: [{ tool_name: 'x' }] });
            expect(toast.success).toHaveBeenCalledWith('Capabilities Refreshed (drift detected)', 'Server a: 2 changed.');
            expect(toast.warning).toHaveBeenCalledWith(
                'Tool description flagged',
                '1 tool description(s) tripped the prompt-injection scanner. Review in Mission Control.',
            );
        });

        it('reports failure', async () => {
            await refresh(new Error('unreachable'));
            // What the Control Plane said, under the name of what failed.
            expect(toast.error).toHaveBeenCalledWith('Capability Refresh Failed', 'unreachable');
        });
    });

    describe('health checks', () => {
        it('no health controls with checks off and no history, and rows get no health action', async () => {
            await renderLoaded([server('a')]);
            expect(screen.queryByText('Check Now')).toBeNull();
            expect(captured.rows.a.onHealthCheck).toBeUndefined();
        });

        it('per-server check calls the API and reports the outcome', async () => {
            health.isEnabled = true;
            await renderLoaded([server('a')]);
            gateway.triggerServerHealthCheck.mockResolvedValueOnce({});
            await act(async () => { await captured.rows.a.onHealthCheck?.('a'); });
            expect(gateway.triggerServerHealthCheck).toHaveBeenCalledWith('a');
            expect(toast.success).toHaveBeenCalledWith('Health Check Complete', 'Health check completed for Server a.');

            gateway.triggerServerHealthCheck.mockRejectedValueOnce(new Error('The server did not answer in 10 s'));
            await act(async () => { await captured.rows.a.onHealthCheck?.('a'); });
            expect(toast.error).toHaveBeenCalledWith('Health Check Failed', 'The server did not answer in 10 s');
        });

        it('Check Now runs the check; it is disabled while one is in flight', async () => {
            health.isEnabled = true;
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('Check Now'));
            await waitFor(() => expect(health.runHealthCheck).toHaveBeenCalledTimes(1));
            cleanup();

            health.isChecking = true;
            await renderLoaded([server('a')]);
            expect(screen.getByText('Checking...').closest('button')?.disabled).toBe(true);
        });

        it('a completed check with checks now off still reloads the list and says disabled', async () => {
            health.lastCheckTime = new Date('2026-11-01T10:00:00Z');
            await renderLoaded([server('a')]);
            expect(screen.getByText('Health check disabled')).toBeTruthy();
            await waitFor(() => expect(gateway.fetchServers).toHaveBeenCalledTimes(2));
        });

        it('offers no health-check settings: the gateway does not act on them in this release', async () => {
            health.isEnabled = true;
            await renderLoaded([server('a')]);
            expect(screen.queryByTitle('Configure health check settings')).toBeNull();
        });
    });

    it('opens and closes the OAuth info modal for the row', async () => {
        await renderLoaded([server('a')]);
        fireEvent.click(screen.getByText('oauth a'));
        expect(screen.getByText('oauth-modal:a')).toBeTruthy();
        fireEvent.click(screen.getByText('close oauth'));
        expect(screen.queryByText('oauth-modal:a')).toBeNull();
    });

    describe('server errors', () => {
        const err = (id: string, operation: string, resource = 'mcp_servers') => ({
            id,
            message: `${id} failed`,
            severity: 'high',
            retryable: true,
            timestamp: new Date('2026-11-01T10:00:00Z'),
            context: { resource, operation },
        });

        it('shows only server errors; retrying a fetch reloads and says so', async () => {
            gateway.errors = [err('e1', 'fetch_servers'), err('e2', 'fetch_models', 'llm_models')];
            await renderLoaded([server('a')]);
            expect(screen.getByText(/e1 failed/)).toBeTruthy();
            expect(screen.queryByText(/e2 failed/)).toBeNull();

            fireEvent.click(screen.getByText('Retry'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Retry Successful', 'Servers have been reloaded.'));
            expect(gateway.fetchServers).toHaveBeenCalledTimes(2);
        });

        // If loadServers swallowed its own failure, a retry whose reload fails
        // would tell the operator "Retry Successful" with an empty list.
        it('a retry whose reload fails reports failure, not success', async () => {
            gateway.errors = [err('e1', 'fetch_servers')];
            await renderLoaded([server('a')]);
            gateway.fetchServers.mockRejectedValue(new Error('still down'));

            fireEvent.click(screen.getByText('Retry'));

            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Retry Failed', 'The retry operation failed. Please try again.'));
            expect(toast.success).not.toHaveBeenCalled();
        });

        it('an operation it cannot replay asks for a manual retry', async () => {
            gateway.errors = [err('e1', 'update_server')];
            await renderLoaded([server('a')]);
            fireEvent.click(screen.getByText('Retry'));
            await waitFor(() => expect(toast.info).toHaveBeenCalledWith('Manual Retry Required', 'This operation cannot be automatically retried.'));
            expect(gateway.fetchServers).toHaveBeenCalledTimes(1);
        });
    });
});

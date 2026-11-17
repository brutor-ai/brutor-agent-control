/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuth app clients tab: filters, and the create / update / delete paths.
 * Save must reject back to the form (the form is what shows the error) and a
 * failed delete must stay in the dialog. The form and row have or need no
 * screen-level detail here, so they are stubs exposing their callbacks.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';

import type { OAuthAppClient } from '../../../../../../lib/types';

const gateway = {
    fetchOAuthAppClients: vi.fn(),
    createOAuthAppClient: vi.fn(),
    updateOAuthAppClient: vi.fn(),
    deleteOAuthAppClient: vi.fn(),
    fetchIdPConfigs: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

interface FormProps {
    client: OAuthAppClient | null;
    idps: Array<{ id: string; name: string; enabled: boolean }>;
    onSave: (data: Record<string, unknown>) => Promise<void>;
    onCancel: () => void;
}
const captured: { form: FormProps | null } = { form: null };

vi.mock('../../../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../OAuthAppClientRow', () => ({
    default: ({ client, idpName, onEdit, onDelete }: {
        client: OAuthAppClient; idpName: string;
        onEdit: (c: OAuthAppClient) => void; onDelete: (id: string, name: string) => void;
    }) => (
        <div>
            <span>row:{client.name}:{idpName}</span>
            <button onClick={() => onEdit(client)}>edit {client.id}</button>
            <button onClick={() => onDelete(client.id, client.name)}>delete {client.id}</button>
            <button onClick={() => onDelete('ghost', 'Ghost')}>delete ghost</button>
        </div>
    ),
}));
vi.mock('../OAuthAppClientForm', () => ({
    default: (props: FormProps) => {
        captured.form = props;
        return (
            <div>
                <span>form:{props.client ? props.client.id : 'new'}</span>
                <button onClick={props.onCancel}>cancel form</button>
            </div>
        );
    },
}));
vi.mock('../../../../../common/GenericDeleteConfirmationModal', () => ({
    default: ({ itemName, itemInfo, customNote, errorMessage, inUse, onConfirm, onClose }: {
        itemName: string; itemInfo?: { description?: string }; customNote?: string;
        errorMessage?: string | null; onConfirm: () => void; onClose: () => void;
        inUse?: { groups: Array<{ kind: string; rows: Array<{ name: string }> }> } | null;
    }) => (
        <div>
            <span>confirm-delete:{itemName}</span>
            <span>delete-info:{itemInfo?.description}</span>
            <span>delete-note:{customNote}</span>
            {errorMessage && <span>delete-error:{errorMessage}</span>}
            {inUse?.groups.map((g) => <span key={g.kind}>in-use:{g.kind}:{g.rows.map((r) => r.name).join(',')}</span>)}
            <button onClick={onConfirm}>confirm delete</button>
            <button onClick={onClose}>cancel delete</button>
        </div>
    ),
}));

import OAuthAppClientTab from '../OAuthAppClientTab';
import { thrownByClient } from '../../../../../../test/serverAnswers';

function client(id: string, overrides: Record<string, unknown> = {}): OAuthAppClient {
    return {
        id,
        name: `Client ${id}`,
        client_id: `cid-${id}`,
        idp_id: 'idp1',
        app_type: 'webapp',
        client_type: 'confidential',
        ...overrides,
    } as unknown as OAuthAppClient;
}

function backend(clients: OAuthAppClient[]) {
    gateway.fetchOAuthAppClients.mockResolvedValue({ clients });
    gateway.fetchIdPConfigs.mockResolvedValue({ idps: { one: { id: 'idp1', name: 'Okta', enabled: true, secret: 'x' } } });
}

async function renderClients(clients: OAuthAppClient[] = [client('a')]) {
    backend(clients);
    render(<OAuthAppClientTab />);
    await screen.findByText('OAuth App Client Management');
}

describe('OAuthAppClientTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        vi.spyOn(console, 'log').mockImplementation(() => {});
        captured.form = null;
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('asks for disabled IdPs too, resolves their names, and falls back to the raw id', async () => {
        await renderClients([client('a'), client('b', { idp_id: 'unknown-idp' })]);
        expect(gateway.fetchIdPConfigs).toHaveBeenCalledWith({ enabled_only: false });
        expect(screen.getByText('row:Client a:Okta')).toBeTruthy();
        expect(screen.getByText('row:Client b:unknown-idp')).toBeTruthy();
    });

    it('a load failure shows inline and as a toast; the inline one dismisses', async () => {
        backend([]);
        gateway.fetchOAuthAppClients.mockRejectedValue(new Error('down'));
        render(<OAuthAppClientTab />);
        expect(await screen.findByText('down')).toBeTruthy();
        expect(toast.error).toHaveBeenCalledWith('Failed to Load OAuth Clients', 'down');
        fireEvent.click(screen.getByText('×'));
        expect(screen.queryByText('down')).toBeNull();
    });

    it('empty and no-match states differ; Refresh refetches', async () => {
        await renderClients([]);
        expect(screen.getByText('No OAuth app clients created')).toBeTruthy();
        fireEvent.change(screen.getByPlaceholderText(/Search by name/), { target: { value: 'x' } });
        expect(screen.getByText('No clients found')).toBeTruthy();
        fireEvent.click(screen.getByText('Refresh'));
        await waitFor(() => expect(gateway.fetchOAuthAppClients).toHaveBeenCalledTimes(2));
    });

    describe('filters', () => {
        const estate = [
            client('a', { name: 'Alpha' }),
            client('b', { name: 'Beta', app_type: 'cli', client_type: 'public', client_id: 'special-cid', idp_id: 'other' }),
        ];

        it('search covers name, client id and IdP name', async () => {
            await renderClients(estate);
            const box = screen.getByPlaceholderText(/Search by name/);
            fireEvent.change(box, { target: { value: 'SPECIAL-CID' } });
            expect(screen.getByText('row:Beta:other')).toBeTruthy();
            expect(screen.queryByText('row:Alpha:Okta')).toBeNull();
            fireEvent.change(box, { target: { value: 'okta' } });
            expect(screen.getByText('row:Alpha:Okta')).toBeTruthy();
            expect(screen.queryByText('row:Beta:other')).toBeNull();
        });

        it('app type and client type narrow the list', async () => {
            await renderClients(estate);
            const [appType, clientType] = screen.getAllByRole('combobox');
            fireEvent.change(appType, { target: { value: 'cli' } });
            expect(screen.queryByText('row:Alpha:Okta')).toBeNull();
            expect(screen.getByText('row:Beta:other')).toBeTruthy();
            fireEvent.change(appType, { target: { value: 'all' } });
            fireEvent.change(clientType, { target: { value: 'confidential' } });
            expect(screen.getByText('row:Alpha:Okta')).toBeTruthy();
            expect(screen.queryByText('row:Beta:other')).toBeNull();
        });
    });

    describe('save', () => {
        it('create: hands the form the IdPs, posts the data, closes and reloads', async () => {
            await renderClients();
            fireEvent.click(screen.getByText('Create OAuth App Client'));
            expect(screen.getByText('form:new')).toBeTruthy();
            // Only id, name and enabled are passed on: nothing else of the IdP config.
            expect(captured.form?.idps).toEqual([{ id: 'idp1', name: 'Okta', enabled: true }]);
            gateway.createOAuthAppClient.mockResolvedValue({});
            const data = { name: 'New' };

            await act(async () => { await captured.form?.onSave(data); });

            expect(gateway.createOAuthAppClient).toHaveBeenCalledWith(data);
            expect(gateway.updateOAuthAppClient).not.toHaveBeenCalled();
            expect(toast.success).toHaveBeenCalledWith('Client Created', 'New has been created successfully.');
            expect(screen.queryByText('form:new')).toBeNull();
            expect(gateway.fetchOAuthAppClients).toHaveBeenCalledTimes(2);
        });

        // The Control Plane's update of a client names no application type,
        // identity provider or client type (they are fixed when the client
        // is created) and refuses a field it does not name.
        it('edit: sends what an update may change, and not the three fields fixed at creation', async () => {
            await renderClients();
            fireEvent.click(screen.getByText('edit a'));
            gateway.updateOAuthAppClient.mockResolvedValue({});

            await act(async () => {
                await captured.form?.onSave({
                    name: 'Renamed', app_type: 'webapp', idp_id: 'idp-1', client_id: 'cid', redirect_uri: 'https://app.example.com/cb',
                    scopes: ['openid'], client_type: 'public', supports_pkce: true, allowed_grant_types: ['authorization_code'],
                });
            });

            expect(gateway.updateOAuthAppClient).toHaveBeenCalledWith('a', {
                name: 'Renamed', client_id: 'cid', redirect_uri: 'https://app.example.com/cb',
                scopes: ['openid'], supports_pkce: true, allowed_grant_types: ['authorization_code'],
            });
        });

        it('edit: updates by id', async () => {
            await renderClients();
            fireEvent.click(screen.getByText('edit a'));
            expect(screen.getByText('form:a')).toBeTruthy();
            gateway.updateOAuthAppClient.mockResolvedValue({});

            await act(async () => { await captured.form?.onSave({ name: 'Renamed' }); });

            expect(gateway.updateOAuthAppClient).toHaveBeenCalledWith('a', { name: 'Renamed' });
            expect(toast.success).toHaveBeenCalledWith('Client Updated', 'Renamed has been updated successfully.');
            expect(screen.queryByText('form:a')).toBeNull();
        });

        it('a failure rejects to the form, which stays open', async () => {
            await renderClients();
            fireEvent.click(screen.getByText('Create OAuth App Client'));
            gateway.createOAuthAppClient.mockRejectedValue(new Error('duplicate'));
            await act(async () => {
                await expect(captured.form?.onSave({ name: 'x' })).rejects.toThrow('duplicate');
            });
            expect(screen.getByText('form:new')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();
            fireEvent.click(screen.getByText('cancel form'));
            expect(screen.queryByText('form:new')).toBeNull();
        });
    });

    describe('delete', () => {
        const openDialog = async () => {
            await renderClients();
            fireEvent.click(screen.getByText('delete a'));
            await screen.findByText('confirm-delete:Client a');
        };

        it('describes the client before anything happens; cancel closes', async () => {
            await openDialog();
            expect(screen.getByText(/^delete-info:webapp \(confidential/)).toBeTruthy();
            expect(screen.getByText('delete-note:Client ID: cid-a')).toBeTruthy();
            expect(gateway.deleteOAuthAppClient).not.toHaveBeenCalled();
            fireEvent.click(screen.getByText('cancel delete'));
            expect(screen.queryByText(/^confirm-delete/)).toBeNull();
        });

        it('deletes by id, reloads and confirms', async () => {
            await openDialog();
            gateway.deleteOAuthAppClient.mockResolvedValue({});
            fireEvent.click(screen.getByText('confirm delete'));
            await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Client Deleted', 'Client a has been deleted successfully.'));
            expect(gateway.deleteOAuthAppClient).toHaveBeenCalledWith('a');
            expect(gateway.fetchOAuthAppClients).toHaveBeenCalledTimes(2);
            await waitFor(() => expect(screen.queryByText(/^confirm-delete/)).toBeNull());
        });

        it('a refusal stays in the dialog, with the server configurations that use the client', async () => {
            await openDialog();
            gateway.deleteOAuthAppClient.mockRejectedValue(
                await thrownByClient(409, {
                    detail: {
                        message: 'OAuth app client Client a is in use: server configurations: Jira access.',
                        used_by: { server_configs: [{ id: 'sc-1', name: 'Jira access' }] },
                    },
                }),
            );
            fireEvent.click(screen.getByText('confirm delete'));
            expect(await screen.findByText('delete-error:OAuth app client Client a is in use: server configurations: Jira access.')).toBeTruthy();
            expect(screen.getByText('in-use:server_configs:Jira access')).toBeTruthy();
            expect(toast.success).not.toHaveBeenCalled();
        });

        it('an id no longer in the list reports rather than opening the dialog', async () => {
            await renderClients();
            fireEvent.click(screen.getByText('delete ghost'));
            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Client Not Found', expect.any(String)));
            expect(screen.queryByText(/^confirm-delete/)).toBeNull();
        });
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Custom roles decide what each admin may do, so the permission list that goes
 * on the wire is the thing to pin, along with the rule that system roles are
 * read-only here.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const client = {
    fetchRoles: vi.fn(),
    fetchPermissions: vi.fn(),
    createRole: vi.fn(),
    updateRole: vi.fn(),
    deleteRole: vi.fn(),
};
const gateway = { gatewayClient: client, isAuthenticated: true };
const confirm = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

import RoleManagement from '../RoleManagement';
import { thrownByClient } from '../../../test/serverAnswers';

const perm = (name: string) => ({
    name,
    display_name: `Display ${name}`,
    description: `Allows ${name}`,
    category: name.split(':')[0],
});

const PERMISSIONS = {
    mcp: [perm('mcp:read'), perm('mcp:write')],
    zebra: [perm('zebra:graze')],
};
const SYSTEM = {
    id: 'r-sys',
    name: 'Tenant Admin',
    description: 'Everything',
    is_system: true,
    permission_count: 2,
    permissions: ['mcp:read', 'zebra:graze'],
};
const CUSTOM = {
    id: 'r-cus',
    name: 'Developer',
    description: 'Builds things',
    is_system: false,
    permission_count: 1,
    permissions: ['mcp:read'],
};

const click = (name: string | RegExp) => fireEvent.click(screen.getByRole('button', { name }));
const card = (name: string) => screen.getByRole('heading', { name }).closest('.bg-surface') as HTMLElement;
const nameBox = () => screen.getByPlaceholderText(/e\.g\., Developer/);
const permBox = (name: string) => screen.getByRole('checkbox', { name: new RegExp(`Display ${name}`) });

async function renderLoaded() {
    render(<RoleManagement />);
    await screen.findByRole('heading', { name: 'Developer' });
}

describe('RoleManagement', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
        confirm.mockResolvedValue(true);
        client.fetchRoles.mockResolvedValue({ roles: [SYSTEM, CUSTOM] });
        client.fetchPermissions.mockResolvedValue({ permissions_by_category: PERMISSIONS });
        client.createRole.mockResolvedValue({});
        client.updateRole.mockResolvedValue({});
        client.deleteRole.mockResolvedValue({});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('creating', () => {
        it('sends the name, description and exactly the permissions ticked', async () => {
            await renderLoaded();
            click('Create Custom Role');

            fireEvent.change(nameBox(), { target: { value: 'Viewer' } });
            fireEvent.change(screen.getByPlaceholderText(/Brief description/), { target: { value: 'Looks only' } });
            fireEvent.click(permBox('mcp:read'));
            fireEvent.click(permBox('zebra:graze'));
            fireEvent.click(permBox('zebra:graze'));
            expect(screen.getByText('Permissions (1 selected)')).toBeInTheDocument();

            click('Create Role');

            await waitFor(() =>
                expect(client.createRole).toHaveBeenCalledWith({
                    name: 'Viewer',
                    description: 'Looks only',
                    permissions: ['mcp:read'],
                }),
            );
            await waitFor(() => expect(screen.queryByRole('button', { name: 'Create Role' })).not.toBeInTheDocument());
            expect(client.fetchRoles).toHaveBeenCalledTimes(2);
            expect(client.updateRole).not.toHaveBeenCalled();
        });

        it('requires a name', async () => {
            await renderLoaded();
            click('Create Custom Role');

            expect(screen.getByRole('button', { name: 'Create Role' })).toBeDisabled();
            fireEvent.change(nameBox(), { target: { value: '   ' } });
            expect(screen.getByRole('button', { name: 'Create Role' })).toBeDisabled();

            fireEvent.click(screen.getByRole('button', { name: 'Create Role' }));
            expect(client.createRole).not.toHaveBeenCalled();
        });

        it('shows a refusal and keeps the form open', async () => {
            client.createRole.mockRejectedValue(new Error('role name exists'));
            await renderLoaded();
            click('Create Custom Role');
            fireEvent.change(nameBox(), { target: { value: 'Developer' } });

            click('Create Role');

            expect(await screen.findByText('role name exists')).toBeInTheDocument();
            expect(nameBox()).toHaveValue('Developer');
        });

        it('sends nothing when cancelled, and starts clean next time', async () => {
            await renderLoaded();
            click('Create Custom Role');
            fireEvent.change(nameBox(), { target: { value: 'Half done' } });

            click('Cancel');
            expect(client.createRole).not.toHaveBeenCalled();

            click('Create Custom Role');
            expect(nameBox()).toHaveValue('');
        });
    });

    describe('editing', () => {
        const edit = () => fireEvent.click(within(card('Developer')).getByRole('button', { name: 'Edit' }));

        it('starts from the role\'s values and sends the changed set', async () => {
            await renderLoaded();
            edit();

            expect(nameBox()).toHaveValue('Developer');
            expect(permBox('mcp:read')).toBeChecked();
            expect(permBox('mcp:write')).not.toBeChecked();

            fireEvent.click(permBox('mcp:read'));
            fireEvent.click(permBox('zebra:graze'));
            fireEvent.change(nameBox(), { target: { value: 'Grazer' } });
            click('Update Role');

            await waitFor(() =>
                expect(client.updateRole).toHaveBeenCalledWith('r-cus', {
                    name: 'Grazer',
                    description: 'Builds things',
                    permissions: ['zebra:graze'],
                }),
            );
            await waitFor(() => expect(screen.queryByRole('button', { name: 'Update Role' })).not.toBeInTheDocument());
            expect(client.createRole).not.toHaveBeenCalled();
        });

        it('sends a cleared description, so it actually clears', async () => {
            await renderLoaded();
            edit();

            fireEvent.change(screen.getByPlaceholderText(/Brief description/), { target: { value: '' } });
            click('Update Role');

            await waitFor(() =>
                expect(client.updateRole).toHaveBeenCalledWith('r-cus', expect.objectContaining({ description: '' })),
            );
        });

        it('sends an empty permission list when everything is removed', async () => {
            await renderLoaded();
            edit();

            fireEvent.click(permBox('mcp:read'));
            click('Update Role');

            await waitFor(() =>
                expect(client.updateRole).toHaveBeenCalledWith('r-cus', expect.objectContaining({ permissions: [] })),
            );
        });

        it('shows a refused update', async () => {
            client.updateRole.mockRejectedValue(new Error('permission unknown'));
            await renderLoaded();
            edit();

            click('Update Role');

            expect(await screen.findByText('permission unknown')).toBeInTheDocument();
        });
    });

    describe('permission categories', () => {
        const category = (label: string) =>
            screen.getByText(label).closest('.border') as HTMLElement;

        it('selects and clears a whole category without touching the others', async () => {
            await renderLoaded();
            click('Create Custom Role');
            fireEvent.change(nameBox(), { target: { value: 'R' } });
            fireEvent.click(permBox('zebra:graze'));

            fireEvent.click(within(category('MCP Servers')).getByRole('button', { name: 'Select All' }));
            expect(within(category('MCP Servers')).getByText('(2/2)')).toBeInTheDocument();
            expect(screen.getByText('Permissions (3 selected)')).toBeInTheDocument();

            fireEvent.click(within(category('MCP Servers')).getByRole('button', { name: 'Select All' }));
            expect(screen.getByText('Permissions (3 selected)')).toBeInTheDocument();

            fireEvent.click(within(category('MCP Servers')).getByRole('button', { name: 'Clear' }));
            expect(within(category('MCP Servers')).getByText('(0/2)')).toBeInTheDocument();

            click('Create Role');
            await waitFor(() =>
                expect(client.createRole).toHaveBeenCalledWith(
                    expect.objectContaining({ permissions: ['zebra:graze'] }),
                ),
            );
        });

        it('collapses and expands, and names unknown categories readably', async () => {
            await renderLoaded();
            click('Create Custom Role');
            expect(permBox('mcp:read')).toBeInTheDocument();

            fireEvent.click(within(category('MCP Servers')).getByRole('button', { name: /MCP Servers/ }));
            expect(screen.queryByRole('checkbox', { name: /Display mcp:read/ })).not.toBeInTheDocument();
            expect(screen.getByText('Zebra')).toBeInTheDocument();

            fireEvent.click(within(category('MCP Servers')).getByRole('button', { name: /MCP Servers/ }));
            expect(permBox('mcp:read')).toBeInTheDocument();
        });
    });

    describe('system roles', () => {
        it('cannot be edited or deleted here, only viewed', async () => {
            await renderLoaded();
            const sys = within(card('Tenant Admin'));

            expect(sys.getByText('System')).toBeInTheDocument();
            expect(sys.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument();
            expect(sys.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
        });

        it('lists a role\'s permissions grouped by category', async () => {
            await renderLoaded();

            fireEvent.click(within(card('Tenant Admin')).getByRole('button', { name: 'View' }));

            expect(screen.getByText('Tenant Admin - Permissions')).toBeInTheDocument();
            expect(screen.getByText('2 permissions assigned')).toBeInTheDocument();
            expect(screen.getByText('MCP Servers (1)')).toBeInTheDocument();
            expect(screen.getByText('Zebra (1)')).toBeInTheDocument();
            expect(screen.getByText('zebra:graze')).toBeInTheDocument();

            click('Close');
            expect(screen.queryByText('Tenant Admin - Permissions')).not.toBeInTheDocument();
        });
    });

    describe('deleting', () => {
        const del = () => fireEvent.click(within(card('Developer')).getByRole('button', { name: 'Delete' }));

        it('deletes after confirming, naming the role and the consequence', async () => {
            await renderLoaded();
            del();

            await waitFor(() => expect(client.deleteRole).toHaveBeenCalledWith('r-cus'));
            expect(confirm).toHaveBeenCalledWith(
                expect.objectContaining({
                    message: expect.stringMatching(/"Developer".*lose the permissions/) as string,
                }),
            );
            await waitFor(() => expect(client.fetchRoles).toHaveBeenCalledTimes(2));
        });

        it('does nothing when the operator backs out', async () => {
            confirm.mockResolvedValue(false);
            await renderLoaded();
            del();

            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(client.deleteRole).not.toHaveBeenCalled();
        });

        it('lists the administrators that hold a role the Control Plane refuses to delete', async () => {
            client.deleteRole.mockRejectedValue(
                await thrownByClient(409, {
                    detail: {
                        message: "Role 'Developer' is held by administrators and is not deleted.",
                        used_by: { administrators: [{ id: 'adm-1', name: 'alice' }, { id: 'adm-2', name: 'bob' }] },
                    },
                }),
            );
            await renderLoaded();
            del();

            expect(await screen.findByText("Role 'Developer' is held by administrators and is not deleted.")).toBeInTheDocument();
            const listed = screen.getByTestId('in-use-list');
            expect(within(listed).getByText('Administrators')).toBeInTheDocument();
            expect(within(listed).getByText('alice')).toBeInTheDocument();
            expect(within(listed).getByText('bob')).toBeInTheDocument();
        });

        it('shows a refused delete', async () => {
            client.deleteRole.mockRejectedValue(new Error('role still assigned'));
            await renderLoaded();
            del();

            expect(await screen.findByText('role still assigned')).toBeInTheDocument();
        });
    });

    it('shows why the roles could not load', async () => {
        client.fetchPermissions.mockRejectedValue(new Error('forbidden'));
        render(<RoleManagement />);

        expect(await screen.findByText('forbidden')).toBeInTheDocument();
        expect(screen.queryByRole('heading', { name: 'Developer' })).not.toBeInTheDocument();
    });
});

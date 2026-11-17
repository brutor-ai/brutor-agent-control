/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Who administers this tenant. The screen's sharp edges are the tenant the new
 * admin lands in (the server 403s a tenant admin naming any tenant but their
 * own), the password checks before anything is sent, and the delete
 * confirmation. A locked administrator is shown as locked, and can be unlocked
 * by a viewer who may update administrators, and by nobody else.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const client = {
    fetchTenantAdminUsers: vi.fn(),
    fetchRoles: vi.fn(),
    createTenantAdminUser: vi.fn(),
    updateTenantAdminUser: vi.fn(),
    assignRoleToAdmin: vi.fn(),
    deleteTenantAdminUser: vi.fn(),
    unlockTenantAdminUser: vi.fn(),
};
const gateway = { gatewayClient: client, isAuthenticated: true };
/** What the signed-in administrator may do. Unlocking needs `admin:user:update`, as the control plane's route does. */
const granted = new Set<string>();
const auth: {
    user: { tenant_id?: string; admin_type?: string } | null;
    hasPermission: (permission: string) => boolean;
} = {
    user: { tenant_id: 'acme', admin_type: 'tenant' },
    hasPermission: (permission) => granted.has(permission),
};
const confirm = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../hooks/useAuth', () => ({ useAuth: () => auth }));
vi.mock('../../common/ConfirmationDialog', () => ({ useConfirm: () => confirm }));

import AdminUserManagement from '../AdminUserManagement';

const ALICE = {
    id: 'u-1',
    username: 'alice',
    email: 'alice@acme.test',
    full_name: 'Alice A',
    tenant_id: 'acme',
    role_id: 'r-admin',
    role_name: 'Tenant Admin',
    permissions: ['a:b', 'c:d'],
    is_active: true,
    is_locked: false,
    last_login: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: null,
};
const BOB = {
    ...ALICE,
    id: 'u-2',
    username: 'bob',
    email: 'bob@acme.test',
    full_name: null,
    is_active: false,
    is_locked: true,
    last_login: '2026-06-01T10:00:00Z',
};
const ROLES = [
    { id: 'r-admin', name: 'Tenant Admin', is_system: true, permission_count: 40 },
    { id: 'r-view', name: 'Viewer', is_system: false, permission_count: 3 },
];

const click = (name: string | RegExp) => fireEvent.click(screen.getByRole('button', { name }));
const fill = (placeholder: string, value: string) =>
    fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value } });
const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;
const inRow = (name: string, title: string) => within(row(name)).getByTitle(title);

async function renderLoaded() {
    render(<AdminUserManagement />);
    await screen.findByText('alice');
}

function fillCreateForm() {
    fill('Enter username', 'carol');
    fill('Enter email', 'carol@acme.test');
    fill('Enter password', 'correct-horse');
    fill('Confirm password', 'correct-horse');
}

/** The modal's own submit, not the page button of the same family. */
const createButton = () => screen.getByRole('button', { name: 'Create User' });

describe('AdminUserManagement', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
        auth.user = { tenant_id: 'acme', admin_type: 'tenant' };
        granted.clear();
        granted.add('admin:user:update');
        confirm.mockResolvedValue(true);
        client.fetchTenantAdminUsers.mockResolvedValue({ admins: [ALICE, BOB] });
        client.fetchRoles.mockResolvedValue({ roles: ROLES });
        client.createTenantAdminUser.mockResolvedValue({});
        client.updateTenantAdminUser.mockResolvedValue({});
        client.assignRoleToAdmin.mockResolvedValue({});
        client.deleteTenantAdminUser.mockResolvedValue({});
        client.unlockTenantAdminUser.mockResolvedValue({ ...BOB, is_locked: false });
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('listing', () => {
        it('shows each admin\'s role and state', async () => {
            await renderLoaded();

            expect(within(row('alice')).getByText('Tenant Admin')).toBeInTheDocument();
            expect(within(row('alice')).getByText('(2 perms)')).toBeInTheDocument();
            expect(within(row('alice')).getByText('Active')).toBeInTheDocument();
            expect(within(row('alice')).getByText('Never')).toBeInTheDocument();
            expect(within(row('bob')).getByText('Inactive')).toBeInTheDocument();
            expect(within(row('bob')).getByText('Locked')).toBeInTheDocument();
        });

        it('shows why the list could not load', async () => {
            client.fetchRoles.mockRejectedValue(new Error('no permission'));
            render(<AdminUserManagement />);

            expect(await screen.findByText('no permission')).toBeInTheDocument();
            expect(screen.getByText(/No admin users found/)).toBeInTheDocument();
        });
    });

    describe('creating', () => {
        it('creates the admin in the caller\'s own tenant, with the fields entered', async () => {
            await renderLoaded();
            click('Create Admin User');
            fillCreateForm();
            fill('Enter full name (optional)', 'Carol C');
            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'r-view' } });

            fireEvent.click(createButton());

            await waitFor(() =>
                expect(client.createTenantAdminUser).toHaveBeenCalledWith({
                    tenant_id: 'acme',
                    username: 'carol',
                    email: 'carol@acme.test',
                    password: 'correct-horse',
                    full_name: 'Carol C',
                    role_id: 'r-view',
                }),
            );
            await waitFor(() => expect(screen.queryByText('Confirm Password *')).not.toBeInTheDocument());
            expect(client.fetchTenantAdminUsers).toHaveBeenCalledTimes(2);
        });

        it('leaves the role out when none is picked, so the server applies its default', async () => {
            await renderLoaded();
            click('Create Admin User');
            fillCreateForm();

            fireEvent.click(createButton());

            await waitFor(() => expect(client.createTenantAdminUser).toHaveBeenCalledTimes(1));
            const body = client.createTenantAdminUser.mock.calls[0][0] as Record<string, unknown>;
            expect(body).not.toHaveProperty('role_id');
            expect(body.full_name).toBeUndefined();
        });

        it('does not send the new admin of an administrator of no tenant to the default tenant', async () => {
            // There is no fallback to `default` for a system administrator.
            // (The screen is opened from Users, which a system
            // administrator does not have.)
            auth.user = { admin_type: 'system' };
            await renderLoaded();
            click('Create Admin User');
            fillCreateForm();

            fireEvent.click(createButton());

            expect(await screen.findByText(/Could not determine which tenant/)).toBeInTheDocument();
            expect(client.createTenantAdminUser).not.toHaveBeenCalled();
        });

        it('refuses to guess the tenant when the session does not say', async () => {
            auth.user = { admin_type: 'tenant' };
            await renderLoaded();
            click('Create Admin User');
            fillCreateForm();

            fireEvent.click(createButton());

            expect(await screen.findByText(/Could not determine which tenant/)).toBeInTheDocument();
            expect(client.createTenantAdminUser).not.toHaveBeenCalled();
        });

        it.each<[string, { username?: string; email?: string; password?: string; confirm?: string }]>([
            ['a short username', { username: 'ab' }],
            ['a malformed email', { email: 'not-an-email' }],
            ['a short password', { password: 'short', confirm: 'short' }],
            ['a blank password of eight spaces', { password: '        ', confirm: '        ' }],
            ['mismatched passwords', { confirm: 'different-horse' }],
        ])('will not submit with %s', async (_label, over) => {
            await renderLoaded();
            click('Create Admin User');
            fillCreateForm();
            if (over.username) fill('Enter username', over.username);
            if (over.email) fill('Enter email', over.email);
            if (over.password) fill('Enter password', over.password);
            if (over.confirm) fill('Confirm password', over.confirm);

            expect(createButton()).toBeDisabled();
            fireEvent.click(createButton());
            expect(client.createTenantAdminUser).not.toHaveBeenCalled();
        });

        it('says a password of spaces is refused, in the words of the rule', async () => {
            await renderLoaded();
            click('Create Admin User');
            fill('Enter password', '          ');
            fill('Confirm password', '          ');
            expect(screen.getByText('Must be at least 8 characters and not blank')).toHaveClass('text-red-600');
            expect(createButton()).toBeDisabled();
        });

        it('tells the operator what is wrong with the password as they type', async () => {
            await renderLoaded();
            click('Create Admin User');
            expect(screen.getByText('Must be at least 8 characters and not blank')).toBeInTheDocument();

            fill('Enter password', 'short');
            fill('Confirm password', 'other');
            expect(screen.getByText('Must be at least 8 characters and not blank')).toHaveClass('text-red-600');
            expect(screen.getByText('Passwords do not match')).toBeInTheDocument();

            fill('Enter password', 'correct-horse');
            fill('Confirm password', 'correct-horse');
            expect(screen.getAllByText('✓ Passwords match').length).toBeGreaterThan(0);
            expect(screen.queryByText('Passwords do not match')).not.toBeInTheDocument();
        });

        it('reveals the passwords on request', async () => {
            await renderLoaded();
            click('Create Admin User');
            const password = screen.getByPlaceholderText('Enter password');
            const confirmBox = screen.getByPlaceholderText('Confirm password');
            expect(password).toHaveAttribute('type', 'password');

            fireEvent.click(password.parentElement?.querySelector('button') as HTMLButtonElement);
            fireEvent.click(confirmBox.parentElement?.querySelector('button') as HTMLButtonElement);

            expect(password).toHaveAttribute('type', 'text');
            expect(confirmBox).toHaveAttribute('type', 'text');
        });

        it('shows a server refusal and keeps what was typed', async () => {
            client.createTenantAdminUser.mockRejectedValue(new Error('username already taken'));
            await renderLoaded();
            click('Create Admin User');
            fillCreateForm();

            fireEvent.click(createButton());

            expect(await screen.findByText('username already taken')).toBeInTheDocument();
            expect(screen.getByPlaceholderText('Enter username')).toHaveValue('carol');
        });

        it('sends nothing when cancelled', async () => {
            await renderLoaded();
            click('Create Admin User');
            fillCreateForm();

            click('Cancel');

            expect(screen.queryByPlaceholderText('Enter username')).not.toBeInTheDocument();
            expect(client.createTenantAdminUser).not.toHaveBeenCalled();
        });
    });

    describe('editing', () => {
        const modal = () => screen.getByText('Edit Admin User').parentElement as HTMLElement;
        const box = (index: number) => modal().querySelectorAll('input')[index];

        it('sends the edited fields for that admin, and reloads', async () => {
            await renderLoaded();
            fireEvent.click(inRow('alice', 'Edit User'));
            expect(box(0)).toHaveValue('alice@acme.test');

            fireEvent.change(box(0), { target: { value: 'new@acme.test' } });
            fireEvent.change(box(1), { target: { value: 'Alice B' } });
            fireEvent.click(within(modal()).getByRole('checkbox'));
            click('Update User');

            await waitFor(() =>
                expect(client.updateTenantAdminUser).toHaveBeenCalledWith('u-1', {
                    email: 'new@acme.test',
                    full_name: 'Alice B',
                    is_active: false,
                }),
            );
            await waitFor(() => expect(screen.queryByText('Edit Admin User')).not.toBeInTheDocument());
            expect(client.fetchTenantAdminUsers).toHaveBeenCalledTimes(2);
        });

        it('sends a blanked name, so it actually clears', async () => {
            await renderLoaded();
            fireEvent.click(inRow('alice', 'Edit User'));

            fireEvent.change(box(1), { target: { value: '' } });
            click('Update User');

            await waitFor(() =>
                expect(client.updateTenantAdminUser).toHaveBeenCalledWith(
                    'u-1',
                    expect.objectContaining({ full_name: '' }),
                ),
            );
        });

        it('shows a refused update', async () => {
            client.updateTenantAdminUser.mockRejectedValue(new Error('email in use'));
            await renderLoaded();
            fireEvent.click(inRow('alice', 'Edit User'));

            click('Update User');

            expect(await screen.findByText('email in use')).toBeInTheDocument();
        });

        it('sends nothing when cancelled', async () => {
            await renderLoaded();
            fireEvent.click(inRow('bob', 'Edit User'));

            click('Cancel');

            expect(screen.queryByText('Edit Admin User')).not.toBeInTheDocument();
            expect(client.updateTenantAdminUser).not.toHaveBeenCalled();
        });
    });

    describe('changing a role', () => {
        const submitRole = () =>
            within(screen.getByText('Change User Role').parentElement as HTMLElement).getByRole('button', {
                name: 'Change Role',
            });

        it('assigns the chosen role to that admin', async () => {
            await renderLoaded();
            fireEvent.click(inRow('alice', 'Change Role'));
            expect(screen.getByRole('combobox')).toHaveValue('r-admin');

            fireEvent.change(screen.getByRole('combobox'), { target: { value: 'r-view' } });
            fireEvent.click(submitRole());

            await waitFor(() =>
                expect(client.assignRoleToAdmin).toHaveBeenCalledWith('u-1', { role_id: 'r-view' }),
            );
            await waitFor(() => expect(screen.queryByText('Change User Role')).not.toBeInTheDocument());
        });

        it('cannot be submitted without a role', async () => {
            await renderLoaded();
            fireEvent.click(inRow('alice', 'Change Role'));

            fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } });

            expect(submitRole()).toBeDisabled();
            click('Cancel');
            expect(client.assignRoleToAdmin).not.toHaveBeenCalled();
        });

        it('shows a refused change', async () => {
            client.assignRoleToAdmin.mockRejectedValue(new Error('role not in tenant'));
            await renderLoaded();
            fireEvent.click(inRow('alice', 'Change Role'));

            fireEvent.click(submitRole());

            expect(await screen.findByText('role not in tenant')).toBeInTheDocument();
        });
    });

    describe('deleting', () => {
        it('deletes only after confirming, naming the admin', async () => {
            await renderLoaded();

            fireEvent.click(inRow('bob', 'Delete User'));

            await waitFor(() => expect(client.deleteTenantAdminUser).toHaveBeenCalledWith('u-2'));
            expect(confirm).toHaveBeenCalledWith(
                expect.objectContaining({ title: 'Delete admin user?', message: expect.stringContaining('"bob"') as string }),
            );
            await waitFor(() => expect(client.fetchTenantAdminUsers).toHaveBeenCalledTimes(2));
        });

        it('does nothing when the operator backs out', async () => {
            confirm.mockResolvedValue(false);
            await renderLoaded();

            fireEvent.click(inRow('bob', 'Delete User'));

            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(client.deleteTenantAdminUser).not.toHaveBeenCalled();
        });

        it('shows a refused delete', async () => {
            client.deleteTenantAdminUser.mockRejectedValue(new Error('cannot delete the last admin'));
            await renderLoaded();

            fireEvent.click(inRow('alice', 'Delete User'));

            expect(await screen.findByText('cannot delete the last admin')).toBeInTheDocument();
        });
    });

    describe('unlocking', () => {
        const unlock = (name: string) => within(row(name)).queryByTitle('Unlock User');

        it('is offered on a locked admin, and on no other', async () => {
            await renderLoaded();

            expect(unlock('bob')).toBeInTheDocument();
            expect(within(row('alice')).queryByText('Locked')).not.toBeInTheDocument();
            expect(unlock('alice')).not.toBeInTheDocument();
        });

        it.each<[string, string[]]>([
            ['no permission at all', []],
            ['every admin-user permission but update', ['admin:user:read', 'admin:user:create', 'admin:user:delete']],
        ])('is not offered to a viewer with %s; the state still shows', async (_label, permissions) => {
            granted.clear();
            permissions.forEach((permission) => granted.add(permission));
            await renderLoaded();

            expect(within(row('bob')).getByText('Locked')).toBeInTheDocument();
            expect(unlock('bob')).not.toBeInTheDocument();
            expect(screen.queryByTitle('Unlock User')).not.toBeInTheDocument();
        });

        it('unlocks only after confirming, naming the admin; reloads the list and says so', async () => {
            await renderLoaded();
            client.fetchTenantAdminUsers.mockResolvedValue({ admins: [ALICE, { ...BOB, is_locked: false }] });

            fireEvent.click(unlock('bob') as HTMLElement);

            await waitFor(() => expect(client.unlockTenantAdminUser).toHaveBeenCalledWith('u-2'));
            expect(client.unlockTenantAdminUser).toHaveBeenCalledTimes(1);
            expect(confirm).toHaveBeenCalledWith(
                expect.objectContaining({
                    title: 'Unlock admin user?',
                    message: expect.stringContaining('"bob"') as string,
                    confirmLabel: 'Unlock',
                }),
            );
            expect(await screen.findByText('"bob" is unlocked and can sign in again.')).toBeInTheDocument();
            expect(client.fetchTenantAdminUsers).toHaveBeenCalledTimes(2);
            // The row as the control plane now has it.
            expect(within(row('bob')).queryByText('Locked')).not.toBeInTheDocument();
            expect(unlock('bob')).not.toBeInTheDocument();
        });

        it('does nothing when the operator backs out', async () => {
            confirm.mockResolvedValue(false);
            await renderLoaded();

            fireEvent.click(unlock('bob') as HTMLElement);

            await waitFor(() => expect(confirm).toHaveBeenCalled());
            expect(client.unlockTenantAdminUser).not.toHaveBeenCalled();
            expect(client.fetchTenantAdminUsers).toHaveBeenCalledTimes(1);
            expect(within(row('bob')).getByText('Locked')).toBeInTheDocument();
        });

        it('shows a refused unlock, and the admin stays locked', async () => {
            client.unlockTenantAdminUser.mockRejectedValue(new Error('Cannot unlock admin from another tenant'));
            await renderLoaded();

            fireEvent.click(unlock('bob') as HTMLElement);

            expect(await screen.findByText('Cannot unlock admin from another tenant')).toBeInTheDocument();
            expect(screen.queryByText(/is unlocked/)).not.toBeInTheDocument();
            expect(within(row('bob')).getByText('Locked')).toBeInTheDocument();
            expect(unlock('bob')).toBeInTheDocument();
        });

        it('a later action clears the report of the unlock', async () => {
            await renderLoaded();
            fireEvent.click(unlock('bob') as HTMLElement);
            await screen.findByText('"bob" is unlocked and can sign in again.');

            fireEvent.click(inRow('alice', 'Delete User'));

            await waitFor(() => expect(client.deleteTenantAdminUser).toHaveBeenCalledWith('u-1'));
            await waitFor(() => expect(screen.queryByText(/is unlocked/)).not.toBeInTheDocument());
        });
    });
});

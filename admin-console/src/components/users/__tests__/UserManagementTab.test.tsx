/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * User Management shell: each tab must reach its own screen. The panels are
 * stubbed because each has its own tests. What can break here is the routing,
 * and the caller's identity reaching the admin-users screen, which uses it to
 * stop an admin editing themselves.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../EndUsersListTab', () => ({ default: () => <div>panel: end users</div> }));
vi.mock('../EndUserGroupsTab', () => ({ default: () => <div>panel: groups</div> }));
vi.mock('../TenantAdminProfile', () => ({ default: () => <div>panel: profile</div> }));
vi.mock('../AdminUserAndRoleManagement', () => ({
    default: ({ currentUser }: { currentUser?: { username: string } }) => (
        <div>panel: admins for {currentUser?.username ?? 'nobody'}</div>
    ),
}));

import UserManagementTab from '../UserManagementTab';
import { WithBuiltinSlots } from '../../../test/builtinSlots';

describe('UserManagementTab', () => {
    afterEach(cleanup);

    it('opens on end users', () => {
        render(<UserManagementTab />, { wrapper: WithBuiltinSlots });
        expect(screen.getByText('panel: end users')).toBeInTheDocument();
    });

    it.each([
        ['User Groups', 'panel: groups'],
        ['Tenant Admin Profile', 'panel: profile'],
        ['Admin Users & Roles', 'panel: admins for nobody'],
    ])('%s shows only its own panel', (tab: string, panel: string) => {
        render(<UserManagementTab />, { wrapper: WithBuiltinSlots });
        fireEvent.click(screen.getByRole('button', { name: tab }));

        expect(screen.getByText(panel)).toBeInTheDocument();
        expect(screen.queryByText('panel: end users')).not.toBeInTheDocument();
    });

    it('comes back to end users', () => {
        render(<UserManagementTab />, { wrapper: WithBuiltinSlots });
        fireEvent.click(screen.getByRole('button', { name: 'User Groups' }));
        fireEvent.click(screen.getByRole('button', { name: 'End-Users' }));
        expect(screen.getByText('panel: end users')).toBeInTheDocument();
    });

    it('passes the signed-in admin through to the admin-users screen', () => {
        render(<UserManagementTab currentUser={{ username: 'ada' }} />, { wrapper: WithBuiltinSlots });
        fireEvent.click(screen.getByRole('button', { name: 'Admin Users & Roles' }));
        expect(screen.getByText('panel: admins for ada')).toBeInTheDocument();
    });

    it('shows who is signed in with their role or roles', () => {
        const { unmount } = render(<UserManagementTab currentUser={{ username: 'ada', role_name: 'Owner' }} />, { wrapper: WithBuiltinSlots });
        expect(screen.getByText(/Logged in as ada \(Owner\)/)).toBeInTheDocument();
        unmount();

        render(<UserManagementTab currentUser={{ username: 'bo', roles: ['a', 'b'] }} />, { wrapper: WithBuiltinSlots });
        expect(screen.getByText(/Logged in as bo \(a, b\)/)).toBeInTheDocument();
    });

    it('claims no identity when there is none', () => {
        render(<UserManagementTab />, { wrapper: WithBuiltinSlots });
        expect(screen.queryByText('RBAC Enabled')).not.toBeInTheDocument();
    });

    // ── Its tabs ─────────────────────────────────────────────────────────────
    const tabLabels = () => screen.getAllByRole('button').map((b) => b.textContent);

    it('shows its own tabs and works', () => {
        render(<UserManagementTab />);
        expect(tabLabels()).toEqual(['End-Users', 'User Groups', 'Admin Users & Roles', 'Tenant Admin Profile']);
        expect(screen.getByText('panel: end users')).toBeInTheDocument();
        fireEvent.click(screen.getByText('User Groups'));
        expect(screen.getByText('panel: groups')).toBeInTheDocument();
    });
});

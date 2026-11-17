/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Admin Users & Roles shell: each tab reaches its own screen, and the signed-in
 * admin reaches the user list, which uses it to guard self-edits. Panels are
 * stubbed because each has its own tests.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../AdminUserManagement', () => ({
    default: ({ currentUser }: { currentUser?: { username: string } }) => (
        <div>panel: admins for {currentUser?.username ?? 'nobody'}</div>
    ),
}));
vi.mock('../RoleManagement', () => ({ default: () => <div>panel: roles</div> }));
vi.mock('../TenantAdminSessions', () => ({ default: () => <div>panel: sessions</div> }));

import AdminUserAndRoleManagement from '../AdminUserAndRoleManagement';

describe('AdminUserAndRoleManagement', () => {
    afterEach(cleanup);

    it('opens on admin users and passes the signed-in admin through', () => {
        render(<AdminUserAndRoleManagement currentUser={{ username: 'ada' }} />);
        expect(screen.getByText('panel: admins for ada')).toBeInTheDocument();
    });

    it.each([
        ['Roles & Permissions', 'panel: roles'],
        ['Tenant Admin Sessions', 'panel: sessions'],
    ])('%s shows only its own panel', (tab, panel) => {
        render(<AdminUserAndRoleManagement />);
        fireEvent.click(screen.getByRole('button', { name: tab }));

        expect(screen.getByText(panel)).toBeInTheDocument();
        expect(screen.queryByText('panel: admins for nobody')).not.toBeInTheDocument();
    });

    it('comes back to admin users', () => {
        render(<AdminUserAndRoleManagement />);
        fireEvent.click(screen.getByRole('button', { name: 'Roles & Permissions' }));
        fireEvent.click(screen.getByRole('button', { name: 'Admin Users' }));
        expect(screen.getByText('panel: admins for nobody')).toBeInTheDocument();
    });
});

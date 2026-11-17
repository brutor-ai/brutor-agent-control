/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Users, Shield, Activity } from 'lucide-react';
import AdminUserManagement from './AdminUserManagement';
import RoleManagement from './RoleManagement';
import TenantAdminSessions from './TenantAdminSessions';

interface AdminUserAndRoleManagementProps {
    currentUser?: any;
}

/**
 * Manage the tenant's admin user list and role definitions.
 *
 * Scope contract: everything rendered here manipulates OTHER admin
 * users / role configurations — it is not the logged-in admin's own
 * settings. Self-scoped settings (profile, MFA, password change)
 * live in `TenantAdminProfile`, a sibling tab under User Management.
 * The split is intentional: a "Security" tab next to "Admin Users"
 * would conflate "manage other admins" with "my own 2FA".
 */
const AdminUserAndRoleManagement: React.FC<AdminUserAndRoleManagementProps> = ({ currentUser }) => {
    const [activeSubTab, setActiveSubTab] = useState<'users' | 'roles' | 'sessions'>('users');

    const tabButtonCls = (active: boolean) =>
        `flex items-center space-x-2 px-4 py-3 border-b-2 transition-colors ${
            active
                ? 'border-primary-500 text-primary-600 bg-primary-50'
                : 'border-transparent text-secondary hover:text-default hover:bg-surface-alt'
        }`;

    return (
        <div className="h-full app-column">
            {/* Sub-tabs Navigation */}
            <div className="flex border-b border-default bg-surface px-6">
                <button
                    onClick={() => setActiveSubTab('users')}
                    className={tabButtonCls(activeSubTab === 'users')}
                >
                    <Users className="w-4 h-4" />
                    <span className="font-medium">Admin Users</span>
                </button>
                <button
                    onClick={() => setActiveSubTab('roles')}
                    className={tabButtonCls(activeSubTab === 'roles')}
                >
                    <Shield className="w-4 h-4" />
                    <span className="font-medium">Roles & Permissions</span>
                </button>
                <button
                    onClick={() => setActiveSubTab('sessions')}
                    className={tabButtonCls(activeSubTab === 'sessions')}
                >
                    <Activity className="w-4 h-4" />
                    <span className="font-medium">Tenant Admin Sessions</span>
                </button>
            </div>

            {/* Sub-tab Content */}
            <div className="scroll-region">
                {activeSubTab === 'users' && <AdminUserManagement currentUser={currentUser} />}
                {activeSubTab === 'roles' && <RoleManagement />}
                {activeSubTab === 'sessions' && <TenantAdminSessions />}
            </div>
        </div>
    );
};

export default AdminUserAndRoleManagement;

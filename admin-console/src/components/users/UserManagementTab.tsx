/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState } from 'react';
import { Users, UsersRound, Shield, UserCircle } from 'lucide-react';
import AdminUserAndRoleManagement from './AdminUserAndRoleManagement';
import EndUserGroupsTab from './EndUserGroupsTab';
import EndUsersListTab from './EndUsersListTab';
import TenantAdminProfile from './TenantAdminProfile';

interface UserManagementTabProps {
    currentUser?: any;
}

type SubTabId =
    | 'end_users'
    | 'end_user_groups'
    | 'admin_users'
    | 'my_profile';

// "Tenant Admin Profile" sits at this level (not nested under
// "Admin Users & Roles") because it manages a different scope —
// the CURRENTLY LOGGED-IN admin's own profile + 2FA — whereas
// "Admin Users & Roles" manages OTHER admins and role defs.
// Bundling them under one tab confuses the two scopes.
const SUB_TABS: Array<{
    id: SubTabId;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
}> = [
    { id: 'end_users', label: 'End-Users', icon: Users },
    { id: 'end_user_groups', label: 'User Groups', icon: UsersRound },
    { id: 'admin_users', label: 'Admin Users & Roles', icon: Shield },
    { id: 'my_profile', label: 'Tenant Admin Profile', icon: UserCircle },
];

const UserManagementTab: React.FC<UserManagementTabProps> = ({ currentUser }) => {
    const [activeSubTab, setActiveSubTab] = useState<SubTabId>('end_users');

    const renderSubTabContent = () => {
        switch (activeSubTab) {
            case 'end_users':
                return <EndUsersListTab />;
            case 'end_user_groups':
                return <EndUserGroupsTab />;
            case 'admin_users':
                return <AdminUserAndRoleManagement currentUser={currentUser} />;
            case 'my_profile':
                return <TenantAdminProfile />;
            default:
                return <EndUsersListTab />;
        }
    };

    return (
        <div className="p-6">
            <div className="mb-6">
                <h1 className="text-2xl font-bold text-default mb-2">User Management</h1>
                <p className="text-secondary">Manage end users, admin users and their roles</p>
                {currentUser && (
                    <div className="flex items-center space-x-2 mt-2">
                        <Shield className="w-4 h-4 text-green-600" />
                        <span className="text-sm text-green-700 font-medium">RBAC Enabled</span>
                        <span className="text-sm text-secondary">
                            - Logged in as {currentUser.username}
                            {currentUser.role_name && ` (${currentUser.role_name})`}
                            {currentUser.roles && ` (${currentUser.roles.join(', ')})`}
                        </span>
                    </div>
                )}
            </div>

            {/* Sub-Navigation Tabs */}
            <div className="mb-6">
                <div className="border-b border-default">
                    <nav className="-mb-px flex space-x-8" aria-label="User management sections">
                        {SUB_TABS.map(({ id, label, icon: Icon }) => (
                            <button
                                key={id}
                                onClick={() => setActiveSubTab(id)}
                                className={`py-2 px-1 border-b-2 font-medium text-sm flex items-center space-x-2 ${
                                    activeSubTab === id
                                        ? 'border-primary-500 text-primary'
                                        : 'border-transparent text-muted hover:text-secondary hover:border-strong'
                                }`}
                            >
                                <Icon className="w-4 h-4" />
                                <span>{label}</span>
                            </button>
                        ))}
                    </nav>
                </div>
            </div>

            {/* Sub-Tab Content */}
            <div className="bg-surface rounded-lg border p-0">
                {renderSubTabContent()}
            </div>
        </div>
    );
};

export default UserManagementTab;
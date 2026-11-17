/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import type { ApiSendableBody } from '../../lib/api/controlPlaneTypes';
import type { components } from '../../lib/generated/control-plane-api';

import { TABLE_TH_TYPE } from '../ui/tableStyles';
import { UserPlus, Shield, AlertCircle, CheckCircle, Loader, Trash2, Edit, Lock, Unlock, Eye, EyeOff, X, Check } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useAuth } from '../../hooks/useAuth';
import { useConfirm } from '../common/ConfirmationDialog';
import { UserAvatar } from '../ui/UserAvatar';
import { ADMIN_USER_UPDATE } from '../../lib/accountLock';
import { errorKind, errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { PASSWORD_RULE_HINT, passwordMeetsRule } from '../../lib/passwordRule';

interface AdminUser {
    id: string;
    username: string;
    email: string;
    full_name: string | null;
    tenant_id: string;
    role_id: string;
    role_name: string;
    permissions: string[];
    is_active: boolean;
    is_locked: boolean;
    avatar_url?: string;
    totp_enabled?: boolean;
    last_login: string | null;
    created_at: string;
    updated_at: string | null;
}

/**
 * Aliased to the generated schema instead of re-declared.
 *
 * A response shape asserted locally is a shape nothing verifies, and
 * hand-written copies in two components drift apart. Deriving it from the Control Plane's
 * own response model means a server-side rename breaks the build here.
 */
type TenantRole = components['schemas']['RoleResponse'];

interface AdminUserManagementProps {
    currentUser?: any;
}

const AdminUserManagement: React.FC<AdminUserManagementProps> = ({ currentUser: _currentUser }) => {
    const { gatewayClient, isAuthenticated } = useGateway();
    const confirm = useConfirm();
    // `user.tenant_id` carries the tenant the logged-in admin is
    // scoped to — used to fill the create-user payload's tenant_id
    // field. The backend rejects mismatching tenant_id from a tenant
    // admin with 403 "Tenant admins can only create admins for their
    // own tenant", so this HAS to reflect the real caller scope, not
    // a hardcoded value.
    const { user: currentAdmin, hasPermission } = useAuth();
    // Unlocking is the one action of a row the control plane's route asks a
    // permission of its own for; a viewer without it is not offered it.
    const canUnlock = hasPermission(ADMIN_USER_UPDATE);

    const [users, setUsers] = useState<AdminUser[]>([]);
    const [roles, setRoles] = useState<TenantRole[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    /** What the last action did, when it has something to say (an unlock). Gone at the next load of the list. */
    const [notice, setNotice] = useState('');
    const [showCreateUser, setShowCreateUser] = useState(false);
    const [showEditUser, setShowEditUser] = useState<string | null>(null);
    const [showRoleChange, setShowRoleChange] = useState<string | null>(null);

    // New user form state
    const [newUser, setNewUser] = useState({
        username: '',
        email: '',
        password: '',
        confirmPassword: '',
        full_name: '',
        role_id: '' // Single role ID
    });

    // Edit user form state
    const [editUser, setEditUser] = useState({
        email: '',
        full_name: '',
        is_active: true
    });

    // Role change state
    const [roleChange, setRoleChange] = useState({
        role_id: ''
    });

    const [creating, setCreating] = useState(false);
    const [updating, setUpdating] = useState(false);
    const [validationErrors, setValidationErrors] = useState<{[key: string]: string}>({});
    const [showPassword, setShowPassword] = useState(false);
    const [showConfirmPassword, setShowConfirmPassword] = useState(false);

    // Check if passwords match and are valid
    const passwordsMatch = newUser.password && newUser.confirmPassword && newUser.password === newUser.confirmPassword;
    const passwordValid = passwordMeetsRule(newUser.password);
    const confirmPasswordTouched = newUser.confirmPassword.length > 0;

    // Check if form can be submitted
    const canSubmit = newUser.username.trim().length >= 3 &&
        /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(newUser.email) &&
        passwordValid &&
        passwordsMatch;

    useEffect(() => {
        if (isAuthenticated && gatewayClient) {
            runBackground(fetchData(), 'data');
        }
    }, [isAuthenticated, gatewayClient]);

    const fetchData = async () => {
        if (!gatewayClient) return;

        try {
            setLoading(true);
            setError('');
            setNotice('');

            const [usersResponse, rolesResponse] = await Promise.all([
                gatewayClient.fetchTenantAdminUsers(),
                gatewayClient.fetchRoles()
            ]);

            setUsers(usersResponse.admins || []);
            setRoles(rolesResponse.roles || []);
        } catch (err) {
            console.error('Failed to fetch data:', errorKind(err));
            setError(errorMessage(err, 'Failed to load data'));
        } finally {
            setLoading(false);
        }
    };

    const validateForm = (): boolean => {
        const errors: {[key: string]: string} = {};

        if (!newUser.username.trim()) {
            errors.username = 'Username is required';
        } else if (newUser.username.length < 3) {
            errors.username = 'Username must be at least 3 characters';
        }

        if (!newUser.email.trim()) {
            errors.email = 'Email is required';
        } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(newUser.email)) {
            errors.email = 'Invalid email format';
        }

        if (!newUser.password) {
            errors.password = 'Password is required';
        } else if (!passwordMeetsRule(newUser.password)) {
            errors.password = `Password must be ${PASSWORD_RULE_HINT}`;
        }

        if (!newUser.confirmPassword) {
            errors.confirmPassword = 'Please confirm your password';
        } else if (newUser.password !== newUser.confirmPassword) {
            errors.confirmPassword = 'Passwords do not match';
        }

        setValidationErrors(errors);
        return Object.keys(errors).length === 0;
    };

    const handleCreateUser = async () => {
        if (!validateForm()) return;
        if (!gatewayClient) return;

        try {
            setCreating(true);
            setError('');

            // The tenant of the new admin is the tenant of the admin who
            // creates it (the backend rejects anything else with "Tenant
            // admins can only create admins for their own tenant"). This
            // screen is opened from Users, which only a tenant administrator
            // has; an administrator of no tenant manages tenants' admins
            // where the tenants are managed. A session that names no tenant
            // gets no guess.
            const targetTenantId = currentAdmin?.tenant_id;
            if (!targetTenantId) {
                setError(
                    'Could not determine which tenant to create the admin in. '
                    + 'Log out and back in, then try again.',
                );
                setCreating(false);
                return;
            }

            const userData: any = {
                tenant_id: targetTenantId,
                username: newUser.username,
                email: newUser.email,
                password: newUser.password,
                full_name: newUser.full_name || undefined
            };

            // Only include role_id if user selected a custom role
            if (newUser.role_id) {
                userData.role_id = newUser.role_id;
            }

            await gatewayClient.createTenantAdminUser(userData);

            // Reset form
            setNewUser({
                username: '',
                email: '',
                password: '',
                confirmPassword: '',
                full_name: '',
                role_id: ''
            });
            setShowCreateUser(false);
            setValidationErrors({});
            setShowPassword(false);
            setShowConfirmPassword(false);

            // Refresh list
            await fetchData();
        } catch (err) {
            console.error('Failed to create user:', errorKind(err));
            setError(errorMessage(err, 'Failed to create user'));
        } finally {
            setCreating(false);
        }
    };

    const handleUpdateUser = async (userId: string) => {
        if (!gatewayClient) return;

        try {
            setUpdating(true);
            setError('');

            const updateData: ApiSendableBody<'/v1/admin/users/tenant/{admin_id}', 'patch'> = {};
            if (editUser.email) updateData.email = editUser.email;
            // Always sent: the server treats an absent field as "unchanged", so a
            // blanked name would otherwise silently keep the old one.
            updateData.full_name = editUser.full_name;
            updateData.is_active = editUser.is_active;

            await gatewayClient.updateTenantAdminUser(userId, updateData);

            setShowEditUser(null);
            await fetchData();
        } catch (err) {
            console.error('Failed to update user:', errorKind(err));
            setError(errorMessage(err, 'Failed to update user'));
        } finally {
            setUpdating(false);
        }
    };

    const handleChangeRole = async (userId: string) => {
        if (!gatewayClient || !roleChange.role_id) return;

        try {
            setUpdating(true);
            setError('');

            await gatewayClient.assignRoleToAdmin(userId, { role_id: roleChange.role_id });

            setShowRoleChange(null);
            setRoleChange({ role_id: '' });
            await fetchData();
        } catch (err) {
            console.error('Failed to change role:', errorKind(err));
            setError(errorMessage(err, 'Failed to change role'));
        } finally {
            setUpdating(false);
        }
    };

    const handleDeleteUser = async (userId: string, username: string) => {
        if (!gatewayClient) return;

        if (!(await confirm({
            title: 'Delete admin user?',
            message: `"${username}" will lose access to the admin console. All audit-log history remains.`,
            confirmLabel: 'Delete user',
            note: 'This action cannot be undone.',
        }))) {
            return;
        }

        try {
            setError('');
            await gatewayClient.deleteTenantAdminUser(userId);
            await fetchData();
        } catch (err) {
            console.error('Failed to delete user:', errorKind(err));
            setError(errorMessage(err, 'Failed to delete user'));
        }
    };

    const handleUnlockUser = async (userId: string, username: string) => {
        if (!gatewayClient) return;

        if (!(await confirm({
            title: 'Unlock admin user?',
            message: `"${username}" will be able to sign in again: the lock and the count of failed sign-ins are cleared.`,
            variant: 'warning',
            confirmLabel: 'Unlock',
        }))) {
            return;
        }

        try {
            setError('');
            await gatewayClient.unlockTenantAdminUser(userId);
            // The row as the control plane now has it.
            await fetchData();
            setNotice(`"${username}" is unlocked and can sign in again.`);
        } catch (err) {
            console.error('Failed to unlock user:', errorKind(err));
            setError(errorMessage(err, 'Failed to unlock user'));
        }
    };

    const openEditModal = (user: AdminUser) => {
        setEditUser({
            email: user.email,
            full_name: user.full_name || '',
            is_active: user.is_active
        });
        setShowEditUser(user.id);
    };

    const openRoleChangeModal = (user: AdminUser) => {
        setRoleChange({
            role_id: user.role_id
        });
        setShowRoleChange(user.id);
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader className="w-8 h-8 animate-spin text-primary-600" />
                <span className="ml-3 text-secondary">Loading admin users...</span>
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-2xl font-bold text-default">Admin Users</h2>
                    <p className="text-secondary mt-1">Manage administrator accounts and their roles</p>
                </div>
                <button
                    onClick={() => setShowCreateUser(true)}
                    className="flex items-center space-x-2 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                >
                    <UserPlus className="w-4 h-4" />
                    <span>Create Admin User</span>
                </button>
            </div>

            {/* Error Message */}
            {error && (
                <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start space-x-3">
                    <AlertCircle className="w-5 h-5 text-red-600 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <h3 className="text-sm font-medium text-red-900">Error</h3>
                        <p className="text-sm text-red-700 mt-1">{error}</p>
                    </div>
                </div>
            )}

            {/* What the last action did */}
            {notice && (
                <div role="status" className="bg-green-50 border border-green-200 rounded-lg p-4 flex items-start space-x-3">
                    <CheckCircle className="w-5 h-5 text-green-600 mt-0.5" />
                    <p className="text-sm text-green-800 flex-1 min-h-0 min-w-0">{notice}</p>
                </div>
            )}

            {/* Users Table */}
            <div className="bg-surface rounded-lg border border-default overflow-hidden">
                <table className="min-w-full divide-y divide-default">
                    <thead className="bg-surface-alt">
                    <tr>
                        <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                            User
                        </th>
                        <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                            Role
                        </th>
                        <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                            Status
                        </th>
                        <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                            Last Login
                        </th>
                        <th className={`${TABLE_TH_TYPE} px-6 py-3 text-right`}>
                            Actions
                        </th>
                    </tr>
                    </thead>
                    <tbody className="bg-surface divide-y divide-default">
                    {users.length === 0 ? (
                        <tr>
                            <td colSpan={5} className="px-6 py-12 text-center text-muted">
                                No admin users found. Create your first admin user to get started.
                            </td>
                        </tr>
                    ) : (
                        users.map((user) => (
                            <tr key={user.id} className="hover:bg-surface-alt">
                                <td className="px-6 py-4 whitespace-nowrap">
                                    <div className="flex items-center">
                                        <UserAvatar avatarUrl={user.avatar_url} name={user.full_name || user.username} size="md" />
                                        <div className="ml-4">
                                            <div className="text-sm font-medium text-default">{user.username}</div>
                                            <div className="text-sm text-muted">{user.email}</div>
                                            {user.full_name && (
                                                <div className="text-xs text-muted">{user.full_name}</div>
                                            )}
                                        </div>
                                    </div>
                                </td>
                                <td className="px-6 py-4 whitespace-nowrap">
                                    <div className="flex items-center space-x-2">
                                        <Shield className="w-4 h-4 text-muted" />
                                        <span className="text-sm text-default">{user.role_name}</span>
                                        <span className="text-xs text-muted">({user.permissions.length} perms)</span>
                                    </div>
                                </td>
                                <td className="px-6 py-4 whitespace-nowrap">
                                    <div className="flex flex-col space-y-1">
                                        {user.is_active ? (
                                            <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
                                                    <CheckCircle className="w-3 h-3 mr-1" />
                                                    Active
                                                </span>
                                        ) : (
                                            <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-surface-raised text-default">
                                                    Inactive
                                                </span>
                                        )}
                                        {user.is_locked && (
                                            <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800">
                                                    <Lock className="w-3 h-3 mr-1" />
                                                    Locked
                                                </span>
                                        )}
                                    </div>
                                </td>
                                <td className="px-6 py-4 whitespace-nowrap text-sm text-muted">
                                    {user.last_login ? new Date(user.last_login).toLocaleString() : 'Never'}
                                </td>
                                <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                                    <div className="flex items-center justify-end space-x-2">
                                        {canUnlock && user.is_locked && (
                                            <button
                                                onClick={asyncHandler(() => handleUnlockUser(user.id, user.username), 'click')}
                                                className="text-amber-600 hover:text-amber-900"
                                                title="Unlock User"
                                            >
                                                <Unlock className="w-4 h-4" />
                                            </button>
                                        )}
                                        <button
                                            onClick={() => openRoleChangeModal(user)}
                                            className="text-primary-600 hover:text-primary-900"
                                            title="Change Role"
                                        >
                                            <Shield className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={() => openEditModal(user)}
                                            className="text-blue-600 hover:text-blue-900"
                                            title="Edit User"
                                        >
                                            <Edit className="w-4 h-4" />
                                        </button>
                                        <button
                                            onClick={asyncHandler(() => handleDeleteUser(user.id, user.username), 'click')}
                                            className="text-red-600 hover:text-red-900"
                                            title="Delete User"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                </td>
                            </tr>
                        ))
                    )}
                    </tbody>
                </table>
            </div>

            {/* Create User Modal */}
            {showCreateUser && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                    <div className="bg-surface rounded-lg p-6 w-full max-w-md">
                        <h3 className="text-lg font-semibold mb-4">Create Admin User</h3>

                        <div className="space-y-4">
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Username *
                                </label>
                                <input
                                    type="text"
                                    value={newUser.username}
                                    onChange={(e) => setNewUser({ ...newUser, username: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                    placeholder="Enter username"
                                />
                                {validationErrors.username && (
                                    <p className="text-red-600 text-sm mt-1">{validationErrors.username}</p>
                                )}
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Email *
                                </label>
                                <input
                                    type="email"
                                    value={newUser.email}
                                    onChange={(e) => setNewUser({ ...newUser, email: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                    placeholder="Enter email"
                                />
                                {validationErrors.email && (
                                    <p className="text-red-600 text-sm mt-1">{validationErrors.email}</p>
                                )}
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Password *
                                </label>
                                <div className="relative">
                                    <input
                                        type={showPassword ? "text" : "password"}
                                        value={newUser.password}
                                        onChange={(e) => setNewUser({ ...newUser, password: e.target.value })}
                                        className={`w-full px-3 py-2 pr-20 border rounded-lg focus:ring-2 focus:ring-primary-500 ${
                                            newUser.password && !passwordValid
                                                ? 'border-red-300'
                                                : newUser.password && passwordValid && confirmPasswordTouched
                                                    ? passwordsMatch
                                                        ? 'border-green-300'
                                                        : 'border-red-300'
                                                    : 'border-strong'
                                        }`}
                                        placeholder="Enter password"
                                    />
                                    <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center space-x-1">
                                        {newUser.password && confirmPasswordTouched && (
                                            passwordsMatch && passwordValid ? (
                                                <Check className="w-4 h-4 text-green-600" />
                                            ) : (
                                                <X className="w-4 h-4 text-red-600" />
                                            )
                                        )}
                                        <button
                                            type="button"
                                            onClick={() => setShowPassword(!showPassword)}
                                            className="text-muted hover:text-secondary"
                                        >
                                            {showPassword ? (
                                                <EyeOff className="w-4 h-4" />
                                            ) : (
                                                <Eye className="w-4 h-4" />
                                            )}
                                        </button>
                                    </div>
                                </div>
                                {validationErrors.password && (
                                    <p className="text-red-600 text-sm mt-1">{validationErrors.password}</p>
                                )}
                                {!validationErrors.password && newUser.password && !passwordValid && (
                                    <p className="text-red-600 text-sm mt-1">{`Must be ${PASSWORD_RULE_HINT}`}</p>
                                )}
                                {!validationErrors.password && !newUser.password && (
                                    <p className="text-xs text-muted mt-1">{`Must be ${PASSWORD_RULE_HINT}`}</p>
                                )}
                                {passwordValid && confirmPasswordTouched && passwordsMatch && (
                                    <p className="text-green-600 text-sm mt-1">✓ Passwords match</p>
                                )}
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Confirm Password *
                                </label>
                                <div className="relative">
                                    <input
                                        type={showConfirmPassword ? "text" : "password"}
                                        value={newUser.confirmPassword}
                                        onChange={(e) => setNewUser({ ...newUser, confirmPassword: e.target.value })}
                                        className={`w-full px-3 py-2 pr-20 border rounded-lg focus:ring-2 focus:ring-primary-500 ${
                                            confirmPasswordTouched
                                                ? passwordsMatch && passwordValid
                                                    ? 'border-green-300'
                                                    : 'border-red-300'
                                                : 'border-strong'
                                        }`}
                                        placeholder="Confirm password"
                                    />
                                    <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center space-x-1">
                                        {confirmPasswordTouched && (
                                            passwordsMatch && passwordValid ? (
                                                <Check className="w-4 h-4 text-green-600" />
                                            ) : (
                                                <X className="w-4 h-4 text-red-600" />
                                            )
                                        )}
                                        <button
                                            type="button"
                                            onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                                            className="text-muted hover:text-secondary"
                                        >
                                            {showConfirmPassword ? (
                                                <EyeOff className="w-4 h-4" />
                                            ) : (
                                                <Eye className="w-4 h-4" />
                                            )}
                                        </button>
                                    </div>
                                </div>
                                {validationErrors.confirmPassword && (
                                    <p className="text-red-600 text-sm mt-1">{validationErrors.confirmPassword}</p>
                                )}
                                {!validationErrors.confirmPassword && confirmPasswordTouched && !passwordsMatch && (
                                    <p className="text-red-600 text-sm mt-1">Passwords do not match</p>
                                )}
                                {confirmPasswordTouched && passwordsMatch && passwordValid && (
                                    <p className="text-green-600 text-sm mt-1">✓ Passwords match</p>
                                )}
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Full Name
                                </label>
                                <input
                                    type="text"
                                    value={newUser.full_name}
                                    onChange={(e) => setNewUser({ ...newUser, full_name: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                    placeholder="Enter full name (optional)"
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Role (optional)
                                </label>
                                <select
                                    value={newUser.role_id}
                                    onChange={(e) => setNewUser({ ...newUser, role_id: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                >
                                    <option value="">Default (Tenant Admin)</option>
                                    {roles.map((role) => (
                                        <option key={role.id} value={role.id}>
                                            {role.name} {role.is_system && '(System)'} - {role.permission_count} permissions
                                        </option>
                                    ))}
                                </select>
                                <p className="text-xs text-muted mt-1">
                                    Leave empty to assign default "Tenant Admin" role
                                </p>
                            </div>
                        </div>

                        <div className="flex space-x-3 mt-6">
                            <button
                                onClick={asyncHandler(handleCreateUser, 'click')}
                                disabled={creating || !canSubmit}
                                className={`flex-1 px-4 py-2 rounded-lg transition-colors min-h-0 min-w-0 ${
                                    creating || !canSubmit
                                        ? 'bg-surface-inset text-muted cursor-not-allowed'
                                        : 'bg-primary-600 text-white hover:bg-primary-700'
                                }`}
                            >
                                {creating ? 'Creating...' : 'Create User'}
                            </button>
                            <button
                                onClick={() => {
                                    setShowCreateUser(false);
                                    setValidationErrors({});
                                    setShowPassword(false);
                                    setShowConfirmPassword(false);
                                }}
                                className="px-4 py-2 border border-strong rounded-lg hover:bg-surface-alt"
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Edit User Modal */}
            {showEditUser && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                    <div className="bg-surface rounded-lg p-6 w-full max-w-md">
                        <h3 className="text-lg font-semibold mb-4">Edit Admin User</h3>

                        <div className="space-y-4">
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Email
                                </label>
                                <input
                                    type="email"
                                    value={editUser.email}
                                    onChange={(e) => setEditUser({ ...editUser, email: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                />
                            </div>

                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Full Name
                                </label>
                                <input
                                    type="text"
                                    value={editUser.full_name}
                                    onChange={(e) => setEditUser({ ...editUser, full_name: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                />
                            </div>

                            <div>
                                <label className="flex items-center space-x-2">
                                    <input
                                        type="checkbox"
                                        checked={editUser.is_active}
                                        onChange={(e) => setEditUser({ ...editUser, is_active: e.target.checked })}
                                        className="rounded border-strong"
                                    />
                                    <span className="text-sm font-medium text-secondary">Active</span>
                                </label>
                            </div>
                        </div>

                        <div className="flex space-x-3 mt-6">
                            <button
                                onClick={asyncHandler(() => handleUpdateUser(showEditUser), 'click')}
                                disabled={updating}
                                className="flex-1 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 min-h-0 min-w-0"
                            >
                                {updating ? 'Updating...' : 'Update User'}
                            </button>
                            <button
                                onClick={() => setShowEditUser(null)}
                                className="px-4 py-2 border border-strong rounded-lg hover:bg-surface-alt"
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Change Role Modal */}
            {showRoleChange && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
                    <div className="bg-surface rounded-lg p-6 w-full max-w-md">
                        <h3 className="text-lg font-semibold mb-4">Change User Role</h3>

                        <div className="space-y-4">
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">
                                    Select Role
                                </label>
                                <select
                                    value={roleChange.role_id}
                                    onChange={(e) => setRoleChange({ role_id: e.target.value })}
                                    className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                >
                                    <option value="">Select a role...</option>
                                    {roles.map((role) => (
                                        <option key={role.id} value={role.id}>
                                            {role.name} {role.is_system && '(System)'} - {role.permission_count} permissions
                                        </option>
                                    ))}
                                </select>
                            </div>
                        </div>

                        <div className="flex space-x-3 mt-6">
                            <button
                                onClick={asyncHandler(() => handleChangeRole(showRoleChange), 'click')}
                                disabled={updating || !roleChange.role_id}
                                className="flex-1 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 min-h-0 min-w-0"
                            >
                                {updating ? 'Changing...' : 'Change Role'}
                            </button>
                            <button
                                onClick={() => {
                                    setShowRoleChange(null);
                                    setRoleChange({ role_id: '' });
                                }}
                                className="px-4 py-2 border border-strong rounded-lg hover:bg-surface-alt"
                            >
                                Cancel
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default AdminUserManagement;
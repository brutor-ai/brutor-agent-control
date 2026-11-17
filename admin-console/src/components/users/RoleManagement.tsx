/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect } from 'react';
import type { components } from '../../lib/generated/control-plane-api';

import { Shield, Plus, Edit, Trash2, CheckCircle, AlertCircle, Loader, ChevronDown, ChevronRight } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useConfirm } from '../common/ConfirmationDialog';
import { errorKind, errorMessage } from '../../lib/narrow';
import { inUseRefusalOf, type InUseRefusal } from '../../lib/inUseRefusal';
import InUseList from '../common/InUseList';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

/**
 * Aliased to the generated schema instead of re-declared.
 *
 * A response shape asserted locally is a shape nothing verifies, and
 * hand-written copies in two components drift apart. Deriving it from the Control Plane's
 * own response model means a server-side rename breaks the build here.
 */
type TenantRole = components['schemas']['RoleResponse'];

interface Permission {
    name: string;
    display_name: string;
    description: string;
    category: string;
}

interface PermissionsByCategory {
    [category: string]: Permission[];
}

const RoleManagement: React.FC = () => {
    const { gatewayClient, isAuthenticated } = useGateway();
    const confirm = useConfirm();

    const [roles, setRoles] = useState<TenantRole[]>([]);
    const [permissions, setPermissions] = useState<PermissionsByCategory>({});
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    /** What holds a role whose delete was refused: listed under the error. */
    const [inUse, setInUse] = useState<InUseRefusal | null>(null);
    const [showCreateRole, setShowCreateRole] = useState(false);
    const [showEditRole, setShowEditRole] = useState<string | null>(null);
    const [showViewPermissions, setShowViewPermissions] = useState<string | null>(null);

    // Form state
    const [roleForm, setRoleForm] = useState({
        name: '',
        description: '',
        permissions: [] as string[]
    });

    const [creating, setCreating] = useState(false);
    const [updating, setUpdating] = useState(false);
    const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());

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
            setInUse(null);

            const [rolesResponse, permissionsResponse] = await Promise.all([
                gatewayClient.fetchRoles(),
                gatewayClient.fetchPermissions()
            ]);

            setRoles(rolesResponse.roles || []);
            setPermissions(permissionsResponse.permissions_by_category || {});

            // Expand all categories by default
            setExpandedCategories(new Set(Object.keys(permissionsResponse.permissions_by_category || {})));
        } catch (err) {
            console.error('Failed to fetch data:', errorKind(err));
            setError(errorMessage(err, 'Failed to load data'));
        } finally {
            setLoading(false);
        }
    };

    const handleCreateRole = async () => {
        if (!gatewayClient || !roleForm.name.trim()) return;

        try {
            setCreating(true);
            setError('');
            setInUse(null);

            const data = {
                name: roleForm.name,
                description: roleForm.description || undefined,
                permissions: roleForm.permissions
            };

            await gatewayClient.createRole(data);

            // Reset form
            setRoleForm({ name: '', description: '', permissions: [] });
            setShowCreateRole(false);

            // Refresh list
            await fetchData();
        } catch (err) {
            console.error('Failed to create role:', errorKind(err));
            setError(errorMessage(err, 'Failed to create role'));
        } finally {
            setCreating(false);
        }
    };

    const handleUpdateRole = async (roleId: string) => {
        if (!gatewayClient) return;

        try {
            setUpdating(true);
            setError('');
            setInUse(null);

            const data = {
                name: roleForm.name || undefined,
                // Sent even when blank: the server reads `undefined` as "unchanged",
                // so a cleared description would otherwise silently come back.
                description: roleForm.description,
                permissions: roleForm.permissions
            };

            await gatewayClient.updateRole(roleId, data);

            setShowEditRole(null);
            setRoleForm({ name: '', description: '', permissions: [] });
            await fetchData();
        } catch (err) {
            console.error('Failed to update role:', errorKind(err));
            setError(errorMessage(err, 'Failed to update role'));
        } finally {
            setUpdating(false);
        }
    };

    const handleDeleteRole = async (roleId: string, roleName: string) => {
        if (!gatewayClient) return;

        if (!(await confirm({
            title: 'Delete role?',
            message: `"${roleName}" will be removed. Any users with this role will lose the permissions granted by it.`,
            confirmLabel: 'Delete role',
            note: 'This cannot be undone.',
        }))) {
            return;
        }

        try {
            setError('');
            setInUse(null);
            await gatewayClient.deleteRole(roleId);
            await fetchData();
        } catch (err) {
            console.error('Failed to delete role:', errorKind(err));
            setError(errorMessage(err, 'Failed to delete role'));
            setInUse(inUseRefusalOf(err));
        }
    };

    const openEditModal = (role: TenantRole) => {
        setRoleForm({
            name: role.name,
            description: role.description || '',
            permissions: [...role.permissions]
        });
        setShowEditRole(role.id);
    };

    const togglePermission = (permissionName: string) => {
        setRoleForm(prev => ({
            ...prev,
            permissions: prev.permissions.includes(permissionName)
                ? prev.permissions.filter(p => p !== permissionName)
                : [...prev.permissions, permissionName]
        }));
    };

    const toggleCategory = (category: string) => {
        setExpandedCategories(prev => {
            const newSet = new Set(prev);
            if (newSet.has(category)) {
                newSet.delete(category);
            } else {
                newSet.add(category);
            }
            return newSet;
        });
    };

    const selectAllInCategory = (category: string) => {
        const categoryPerms = permissions[category].map(p => p.name);
        setRoleForm(prev => ({
            ...prev,
            permissions: [...new Set([...prev.permissions, ...categoryPerms])]
        }));
    };

    const deselectAllInCategory = (category: string) => {
        const categoryPerms = new Set(permissions[category].map(p => p.name));
        setRoleForm(prev => ({
            ...prev,
            permissions: prev.permissions.filter(p => !categoryPerms.has(p))
        }));
    };

    const getCategoryDisplayName = (category: string): string => {
        const names: {[key: string]: string} = {
            'mcp': 'MCP Servers',
            'virtual': 'Virtual Servers',
            'transport': 'Transport/Proxy',
            'proxy': 'Proxy Management',
            'registry': 'Registry',
            'oauth': 'OAuth',
            'server': 'Server Config',
            'idp': 'Identity Providers',
            'admin': 'Admin Users',
            'enduser': 'End Users',
            'usergroup': 'Resource Groups',
            'roles': 'Roles',
            'permissions': 'Permissions',
            'gateway': 'Gateway',
            'docker': 'Docker',
            'ai': 'AI Models',
            'llm': 'LLM Router',
            'system': 'System'
        };
        return names[category] || category.charAt(0).toUpperCase() + category.slice(1);
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader className="w-8 h-8 animate-spin text-primary-600" />
                <span className="ml-3 text-secondary">Loading roles and permissions...</span>
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-2xl font-bold text-default">Roles & Permissions</h2>
                    <p className="text-secondary mt-1">Manage custom roles and their permissions</p>
                </div>
                <button
                    onClick={() => {
                        setRoleForm({ name: '', description: '', permissions: [] });
                        setShowCreateRole(true);
                    }}
                    className="flex items-center space-x-2 px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
                >
                    <Plus className="w-4 h-4" />
                    <span>Create Custom Role</span>
                </button>
            </div>

            {/* Error Message */}
            {error && (
                <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start space-x-3">
                    <AlertCircle className="w-5 h-5 text-red-600 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <h3 className="text-sm font-medium text-red-900">Error</h3>
                        <p className="text-sm text-red-700 mt-1">{error}</p>
                        {inUse && inUse.groups.length > 0 && (
                            <div className="mt-2">
                                <InUseList groups={inUse.groups} />
                            </div>
                        )}
                    </div>
                </div>
            )}

            {/* Roles Grid */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {roles.map((role) => (
                    <div
                        key={role.id}
                        className="bg-surface border border-default rounded-lg p-4 hover:shadow-md transition-shadow"
                    >
                        <div className="flex items-start justify-between mb-3">
                            <div className="flex items-center space-x-2">
                                <Shield className={`w-5 h-5 ${role.is_system ? 'text-blue-600' : 'text-primary-600'}`} />
                                <h3 className="font-semibold text-default">{role.name}</h3>
                            </div>
                            {role.is_system && (
                                <span className="px-2 py-1 text-xs font-medium bg-blue-100 text-blue-800 rounded">
                                    System
                                </span>
                            )}
                        </div>

                        {role.description && (
                            <p className="text-sm text-secondary mb-3">{role.description}</p>
                        )}

                        <div className="flex items-center justify-between text-sm text-muted mb-3">
                            <span>{role.permission_count} permissions</span>
                            <button
                                onClick={() => setShowViewPermissions(role.id)}
                                className="text-primary-600 hover:text-primary-700"
                            >
                                View
                            </button>
                        </div>

                        {!role.is_system && (
                            <div className="flex space-x-2 pt-3 border-t border-default">
                                <button
                                    onClick={() => openEditModal(role)}
                                    className="flex-1 flex items-center justify-center space-x-1 px-3 py-2 text-sm bg-primary-50 text-primary-700 rounded hover:bg-primary-100 min-h-0 min-w-0"
                                >
                                    <Edit className="w-3 h-3" />
                                    <span>Edit</span>
                                </button>
                                <button
                                    onClick={asyncHandler(() => handleDeleteRole(role.id, role.name), 'click')}
                                    className="flex-1 flex items-center justify-center space-x-1 px-3 py-2 text-sm bg-red-50 text-red-700 rounded hover:bg-red-100 min-h-0 min-w-0"
                                >
                                    <Trash2 className="w-3 h-3" />
                                    <span>Delete</span>
                                </button>
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {/* Create/Edit Role Modal */}
            {(showCreateRole || showEditRole) && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg w-full max-w-4xl max-h-[90vh] overflow-hidden flex flex-col">
                        <div className="p-6 border-b border-default">
                            <h3 className="text-lg font-semibold">
                                {showCreateRole ? 'Create Custom Role' : 'Edit Role'}
                            </h3>
                        </div>

                        <div className="scroll-region p-6">
                            <div className="space-y-6">
                                {/* Basic Info */}
                                <div className="space-y-4">
                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Role Name *
                                        </label>
                                        <input
                                            type="text"
                                            value={roleForm.name}
                                            onChange={(e) => setRoleForm({ ...roleForm, name: e.target.value })}
                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                            placeholder="e.g., Developer, Viewer, Manager"
                                        />
                                    </div>

                                    <div>
                                        <label className="block text-sm font-medium text-secondary mb-1">
                                            Description
                                        </label>
                                        <textarea
                                            value={roleForm.description}
                                            onChange={(e) => setRoleForm({ ...roleForm, description: e.target.value })}
                                            rows={2}
                                            className="w-full px-3 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500"
                                            placeholder="Brief description of this role"
                                        />
                                    </div>
                                </div>

                                {/* Permissions Selection */}
                                <div>
                                    <div className="flex items-center justify-between mb-3">
                                        <h4 className="text-sm font-medium text-default">
                                            Permissions ({roleForm.permissions.length} selected)
                                        </h4>
                                    </div>

                                    <div className="space-y-3">
                                        {Object.entries(permissions).map(([category, perms]) => {
                                            const isExpanded = expandedCategories.has(category);
                                            const categoryPerms = perms.map(p => p.name);
                                            const selectedInCategory = roleForm.permissions.filter(p => categoryPerms.includes(p)).length;

                                            return (
                                                <div key={category} className="border border-default rounded-lg">
                                                    <div className="bg-surface-alt px-4 py-3 flex items-center justify-between">
                                                        <button
                                                            onClick={() => toggleCategory(category)}
                                                            className="flex items-center space-x-2 flex-1 text-left min-h-0 min-w-0"
                                                        >
                                                            {isExpanded ? (
                                                                <ChevronDown className="w-4 h-4 text-muted" />
                                                            ) : (
                                                                <ChevronRight className="w-4 h-4 text-muted" />
                                                            )}
                                                            <span className="font-medium text-default">
                                                                {getCategoryDisplayName(category)}
                                                            </span>
                                                            <span className="text-sm text-muted">
                                                                ({selectedInCategory}/{perms.length})
                                                            </span>
                                                        </button>
                                                        <div className="flex space-x-2">
                                                            <button
                                                                onClick={() => selectAllInCategory(category)}
                                                                className="text-xs text-primary-600 hover:text-primary-700"
                                                            >
                                                                Select All
                                                            </button>
                                                            <button
                                                                onClick={() => deselectAllInCategory(category)}
                                                                className="text-xs text-secondary hover:text-secondary"
                                                            >
                                                                Clear
                                                            </button>
                                                        </div>
                                                    </div>

                                                    {isExpanded && (
                                                        <div className="p-4 space-y-2">
                                                            {perms.map((perm) => (
                                                                <label
                                                                    key={perm.name}
                                                                    className="flex items-start space-x-3 p-2 rounded hover:bg-surface-alt cursor-pointer"
                                                                >
                                                                    <input
                                                                        type="checkbox"
                                                                        checked={roleForm.permissions.includes(perm.name)}
                                                                        onChange={() => togglePermission(perm.name)}
                                                                        className="mt-1 rounded border-strong"
                                                                    />
                                                                    <div className="flex-1 min-h-0 min-w-0">
                                                                        <div className="text-sm font-medium text-default">
                                                                            {perm.display_name}
                                                                        </div>
                                                                        <div className="text-xs text-muted">
                                                                            {perm.description}
                                                                        </div>
                                                                        <div className="text-xs text-muted font-mono">
                                                                            {perm.name}
                                                                        </div>
                                                                    </div>
                                                                </label>
                                                            ))}
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div className="p-6 border-t border-default flex justify-end space-x-3">
                            <button
                                onClick={() => {
                                    setShowCreateRole(false);
                                    setShowEditRole(null);
                                    setRoleForm({ name: '', description: '', permissions: [] });
                                }}
                                className="px-4 py-2 border border-strong rounded-lg hover:bg-surface-alt"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={asyncHandler(() => showCreateRole ? handleCreateRole() : handleUpdateRole(showEditRole!), 'click')}
                                disabled={creating || updating || !roleForm.name.trim()}
                                className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
                            >
                                {creating || updating ? 'Saving...' : showCreateRole ? 'Create Role' : 'Update Role'}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* View Permissions Modal */}
            {showViewPermissions && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col">
                        {(() => {
                            const role = roles.find(r => r.id === showViewPermissions);
                            if (!role) return null;

                            // Group permissions by category
                            const permsByCategory: {[key: string]: string[]} = {};
                            role.permissions.forEach(perm => {
                                const category = perm.split(':')[0];
                                if (!permsByCategory[category]) {
                                    permsByCategory[category] = [];
                                }
                                permsByCategory[category].push(perm);
                            });

                            return (
                                <>
                                    <div className="p-6 border-b border-default">
                                        <h3 className="text-lg font-semibold">{role.name} - Permissions</h3>
                                        <p className="text-sm text-secondary mt-1">
                                            {role.permission_count} permissions assigned
                                        </p>
                                    </div>

                                    <div className="scroll-region p-6">
                                        <div className="space-y-4">
                                            {Object.entries(permsByCategory).map(([category, perms]) => (
                                                <div key={category}>
                                                    <h4 className="font-medium text-default mb-2">
                                                        {getCategoryDisplayName(category)} ({perms.length})
                                                    </h4>
                                                    <div className="space-y-1">
                                                        {perms.map(perm => (
                                                            <div
                                                                key={perm}
                                                                className="flex items-center space-x-2 text-sm text-secondary py-1"
                                                            >
                                                                <CheckCircle className="w-4 h-4 text-green-600" />
                                                                <span className="font-mono text-xs">{perm}</span>
                                                            </div>
                                                        ))}
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    </div>

                                    <div className="p-6 border-t border-default flex justify-end">
                                        <button
                                            onClick={() => setShowViewPermissions(null)}
                                            className="px-4 py-2 bg-surface-raised text-secondary rounded-lg hover:bg-surface-inset"
                                        >
                                            Close
                                        </button>
                                    </div>
                                </>
                            );
                        })()}
                    </div>
                </div>
            )}
        </div>
    );
};

export default RoleManagement;
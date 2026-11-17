/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback, useRef } from 'react';
import type { ApiResponse } from '../../lib/api/controlPlaneTypes';

import { TABLE_TH_TYPE } from '../ui/tableStyles';
import {
    Users,
    Mail,
    CheckCircle,
    XCircle,
    Search,
    Filter,
    Eye,
    EyeOff,
    Trash2,
    RefreshCw,
    Download,
    Lock,
    Unlock,
    AlertCircle,
    Loader
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { UserAvatar } from '../ui/UserAvatar';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import { useSlot } from '../../app/slots';
import { errorKind, errorMessage, failureSentence } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';
import { PASSWORD_RULE_HINT, passwordMeetsRule } from '../../lib/passwordRule';

// One page of end-users. The table is not virtualised, so there is a cap;
// paging and the total make it a page boundary rather than a silent
// truncation, which would leave every user past it unmanageable — not
// disableable, not deletable, not inspectable — with nothing on screen to
// say so.
const PAGE_SIZE = 100;

/**
 * Derived from the endpoint's own response type rather than
 * re-declared.
 *
 * The hand-written version marked `email` REQUIRED where the server may send
 * null, so any render that read `user.email.toLowerCase()` would have thrown
 * on a legitimate response. Deriving the shape from the route makes that
 * nullability visible at the call site.
 */
type EndUser = ApiResponse<'/v1/admin/end-users/{user_id}', 'get'>;

const EndUsersListTab: React.FC = () => {
    const {
        fetchEndUsers,
        fetchEndUserDetails,
        updateEndUserStatus,
        deleteEndUser,
        exportEndUsers,
        gatewayClient,
    } = useGateway();

    const [endUsers, setEndUsers] = useState<EndUser[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    // Search is a server query, so it reaches users beyond the current
    // page; debounced so a typed username isn't one request per character.
    const debouncedSearchTerm = useDebouncedValue(searchTerm, 300);
    const [filterStatus, setFilterStatus] = useState<'all' | 'active' | 'inactive'>('all');
    const [offset, setOffset] = useState(0);
    const [total, setTotal] = useState(0);
    const [selectedUser, setSelectedUser] = useState<EndUser | null>(null);
    const [showUserDetails, setShowUserDetails] = useState(false);
    const [loadingDetails, setLoadingDetails] = useState(false);

    // Actions on the list that this page does not own: each is contributed to
    // the `end-users.actions` slot (Send Message is). The dialog of the one
    // that is open is the only one mounted.
    const listActions = useSlot('end-users.actions');
    const [openListAction, setOpenListAction] = useState<string | null>(null);

    // Create user modal state
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [createForm, setCreateForm] = useState({ username: '', email: '', password: '', display_name: '', role_id: '', group_ids: [] as string[], send_welcome_email: true });
    const [createLoading, setCreateLoading] = useState(false);
    const [createError, setCreateError] = useState('');
    const [showCreatePassword, setShowCreatePassword] = useState(false);
    // Optional photo selected during user create. The admin
    // endpoint is two-step (create returns user_id, then a
    // multipart POST sets the photo) so we keep the File on the
    // client until create succeeds.
    const [createPhotoFile, setCreatePhotoFile] = useState<File | null>(null);
    const [createPhotoPreview, setCreatePhotoPreview] = useState<string | null>(null);

    // Delete modal state
    const [showDeleteModal, setShowDeleteModal] = useState(false);
    const [userToDelete, setUserToDelete] = useState<EndUser | null>(null);
    const [isDeleting, setIsDeleting] = useState(false);
    const [deleteError, setDeleteError] = useState<string | null>(null);

    const loadSeqRef = useRef(0);

    const loadEndUsers = useCallback(async () => {
        const seq = ++loadSeqRef.current;
        setLoading(true);
        setError('');
        try {
            const response = await fetchEndUsers({
                // `active_only` is the only status filter the control plane
                // serves; "inactive only" has no server-side equivalent, so it
                // stays a page-scoped refinement and the footer says as much.
                search: debouncedSearchTerm.trim() || undefined,
                active_only: filterStatus === 'active' ? true : undefined,
                limit: PAGE_SIZE,
                offset,
            });
            if (seq !== loadSeqRef.current) return;
            const rows = response.users || [];
            const count = response.pagination?.total ?? rows.length;
            // Past the end of the list (the last row of the last page was just
            // deleted): step back rather than show "Showing 101–100 of 100".
            if (rows.length === 0 && offset > 0 && count > 0) {
                setOffset(Math.max(0, offset - PAGE_SIZE));
                return;
            }
            setEndUsers(rows);
            setTotal(count);
        } catch (err) {
            if (seq !== loadSeqRef.current) return;
            console.error('Failed to fetch end users:', errorKind(err));
            setError(errorMessage(err, 'Failed to load end users'));
        } finally {
            if (seq === loadSeqRef.current) setLoading(false);
        }
    }, [fetchEndUsers, debouncedSearchTerm, filterStatus, offset]);

    useEffect(() => {
        runBackground(loadEndUsers(), 'end users');
    }, [loadEndUsers]);

    // Narrowing the query while on page 3 would otherwise ask for rows
    // 200-299 of a result set that may hold 4, showing an empty table.
    useEffect(() => {
        setOffset(0);
    }, [debouncedSearchTerm, filterStatus]);

    const loadUserDetails = async (userId: number) => {
        setLoadingDetails(true);
        try {
            const response = await fetchEndUserDetails(userId);
            setSelectedUser(response);
            setShowUserDetails(true);
        } catch (err) {
            console.error('Failed to fetch user details:', errorKind(err));
            setError(errorMessage(err, 'Failed to load user details'));
        } finally {
            setLoadingDetails(false);
        }
    };

    const toggleUserStatus = async (userId: number, currentStatus: boolean) => {
        try {
            await updateEndUserStatus(userId, !currentStatus);
            await loadEndUsers();
        } catch (err) {
            console.error('Failed to toggle user status:', errorKind(err));
            setError(errorMessage(err, 'Failed to update user status'));
        }
    };

    const handleDeleteUser = async (user: EndUser) => {
        setDeleteError(null);
        setUserToDelete(user);
        setShowDeleteModal(true);
    };

    const confirmDeleteUser = async () => {
        if (!userToDelete) return;

        setIsDeleting(true);
        try {
            await deleteEndUser(userToDelete.id);
            await loadEndUsers();
            if (selectedUser?.id === userToDelete.id) {
                setShowUserDetails(false);
                setSelectedUser(null);
            }
            setShowDeleteModal(false);
            setUserToDelete(null);
        } catch (err: any) {
            console.error('Failed to delete user:', errorKind(err));
            setDeleteError(err.detail || err.message || 'Failed to delete user');
        } finally {
            setIsDeleting(false);
        }
    };

    const handleExportUsers = async () => {
        try {
            const blob = await exportEndUsers('csv');
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `end-users-${new Date().toISOString()}.csv`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        } catch (err) {
            console.error('Failed to export users:', errorKind(err));
            setError(errorMessage(err, 'Failed to export users'));
        }
    };

    // Search and "active only" are applied by the server across the whole
    // tenant. Only "inactive only" is left to the client, because the control
    // plane exposes no equivalent parameter — it therefore narrows the loaded
    // page, not the tenant, which the footer discloses.
    const filteredUsers = filterStatus === 'inactive'
        ? endUsers.filter(user => !user.is_active)
        : endUsers;

    const formatDate = (dateString?: string | null) => {
        if (!dateString) return 'Never';
        try {
            return new Date(dateString).toLocaleString();
        } catch {
            return 'Invalid date';
        }
    };

    if (loading) {
        return (
            <div className="p-6 max-w-7xl mx-auto">
                <div className="text-center py-12">
                    <Loader className="w-12 h-12 animate-spin text-primary mx-auto mb-4" />
                    <p className="text-secondary">Loading end users...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="p-6 max-w-7xl mx-auto">
            {/* Header */}
            <div className="mb-6">
                <div className="flex justify-between items-center mb-4">
                    <div>
                        <h1 className="text-2xl font-bold text-default mb-2">End User Management</h1>
                        <p className="text-secondary">
                            Manage end users, you can disable and remove users here
                        </p>
                    </div>
                    <div className="flex items-center space-x-2">
                        <button
                            onClick={() => { setCreateForm({ username: '', email: '', password: '', display_name: '', role_id: '', group_ids: [], send_welcome_email: true }); setCreateError(''); setCreatePhotoFile(null); setCreatePhotoPreview(null); setShowCreateModal(true); }}
                            className="flex items-center space-x-2 px-4 py-2 text-white bg-green-600 rounded-md hover:bg-green-700"
                        >
                            <Users className="w-4 h-4" />
                            <span>Create User</span>
                        </button>
                        {listActions.map((action) => (
                            <action.Button key={action.id} onOpen={() => setOpenListAction(action.id)} />
                        ))}
                        <button
                            onClick={asyncHandler(handleExportUsers, 'click')}
                            className="flex items-center space-x-2 px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt"
                        >
                            <Download className="w-4 h-4" />
                            <span>Export</span>
                        </button>
                        <button
                            onClick={asyncHandler(loadEndUsers, 'click')}
                            className="flex items-center space-x-2 px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt"
                        >
                            <RefreshCw className="w-4 h-4" />
                            <span>Refresh</span>
                        </button>
                    </div>
                </div>

                {/* Stats Cards */}
                <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
                    <div className="bg-surface border rounded-lg p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-secondary">Total Users</p>
                                <p className="text-2xl font-bold text-default">{total}</p>
                            </div>
                            <Users className="w-8 h-8 text-primary" />
                        </div>
                    </div>
                    <div className="bg-surface border rounded-lg p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-secondary">Active Users</p>
                                <p className="text-2xl font-bold text-green-600">
                                    {endUsers.filter(u => u.is_active).length}
                                </p>
                            </div>
                            <CheckCircle className="w-8 h-8 text-green-600" />
                        </div>
                    </div>
                    <div className="bg-surface border rounded-lg p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-secondary">Verified Users</p>
                                <p className="text-2xl font-bold text-primary">
                                    {endUsers.filter(u => u.is_verified).length}
                                </p>
                            </div>
                            <Mail className="w-8 h-8 text-primary" />
                        </div>
                    </div>
                    <div className="bg-surface border rounded-lg p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-secondary">Inactive Users</p>
                                <p className="text-2xl font-bold text-secondary">
                                    {endUsers.filter(u => !u.is_active).length}
                                </p>
                            </div>
                            <XCircle className="w-8 h-8 text-secondary" />
                        </div>
                    </div>
                </div>

                {/* Search and Filters */}
                <div className="flex items-center space-x-4 mb-4">
                    <div className="flex-1 relative min-h-0 min-w-0">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                        <input
                            type="text"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            placeholder="Search by username, email, or display name..."
                            className="w-full pl-10 pr-4 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        />
                    </div>
                    <div className="flex items-center space-x-2">
                        <Filter className="w-4 h-4 text-muted" />
                        <select
                            value={filterStatus}
                            onChange={(e) => setFilterStatus(e.target.value as any)}
                            className="px-3 py-2 border border-strong rounded-md focus:outline-none focus:ring-2 focus:ring-primary"
                        >
                            <option value="all">All Users</option>
                            <option value="active">Active Only</option>
                            <option value="inactive">Inactive Only</option>
                        </select>
                    </div>
                </div>
            </div>

            {/* Error Display */}
            {error && (
                <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-md">
                    <div className="flex items-center space-x-2">
                        <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0" />
                        <span className="text-red-700">{error}</span>
                        <button
                            onClick={() => setError('')}
                            className="ml-auto text-red-600 hover:text-red-800"
                        >
                            ×
                        </button>
                    </div>
                </div>
            )}

            {/* Users Table */}
            <div className="bg-surface rounded-lg border overflow-hidden">
                {filteredUsers.length === 0 ? (
                    <div className="text-center py-12">
                        <Users className="w-12 h-12 text-muted mx-auto mb-4" />
                        <h3 className="text-lg font-medium text-default mb-2">
                            {searchTerm || filterStatus !== 'all' ? 'No users found' : 'No end users yet'}
                        </h3>
                        <p className="text-secondary">
                            {searchTerm || filterStatus !== 'all'
                                ? 'Try adjusting your search or filters'
                                : 'End users will appear here once they register'}
                        </p>
                    </div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-default">
                            <thead className="bg-surface-alt">
                            <tr>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    User
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Status
                                </th>
                                <th className={`${TABLE_TH_TYPE} px-6 py-3 text-left`}>
                                    Created
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
                            {filteredUsers.map((user) => (
                                <tr key={user.id} className="hover:bg-surface-alt">
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <div className="flex items-center">
                                            <UserAvatar avatarUrl={user.avatar_url} name={user.display_name || user.username} size="md" />
                                            <div className="ml-4">
                                                <div className="text-sm font-medium text-default">
                                                    {user.display_name || user.username}
                                                </div>
                                                <div className="text-sm text-muted">{user.email}</div>
                                                {user.display_name && (
                                                    <div className="text-xs text-muted">@{user.username}</div>
                                                )}
                                            </div>
                                        </div>
                                    </td>
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <div className="flex flex-col space-y-1">
                                            <div className="flex items-center space-x-2">
                                                {user.is_active ? (
                                                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
                              <CheckCircle className="w-3 h-3 mr-1" />
                              Active
                            </span>
                                                ) : (
                                                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-surface-raised text-default">
                              <XCircle className="w-3 h-3 mr-1" />
                              Inactive
                            </span>
                                                )}
                                            </div>
                                            <div className="flex items-center space-x-2">
                                                {user.is_verified ? (
                                                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-800">
                              <Mail className="w-3 h-3 mr-1" />
                              Verified
                            </span>
                                                ) : (
                                                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800">
                              <Mail className="w-3 h-3 mr-1" />
                              Unverified
                            </span>
                                                )}
                                            </div>
                                        </div>
                                    </td>
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <div className="text-sm text-default">
                                            {formatDate(user.created_at)}
                                        </div>
                                    </td>
                                    <td className="px-6 py-4 whitespace-nowrap">
                                        <div className="text-sm text-default">
                                            {formatDate(user.last_login)}
                                        </div>
                                    </td>
                                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                                        <div className="flex items-center justify-end space-x-2">
                                            <button
                                                onClick={asyncHandler(() => loadUserDetails(user.id), 'click')}
                                                className="text-primary hover:text-primary-900"
                                                title="View Details"
                                            >
                                                <Eye className="w-4 h-4" />
                                            </button>
                                            <button
                                                onClick={asyncHandler(() => toggleUserStatus(user.id, user.is_active), 'click')}
                                                className={`${
                                                    user.is_active ? 'text-yellow-600 hover:text-yellow-900' : 'text-green-600 hover:text-green-900'
                                                }`}
                                                title={user.is_active ? 'Deactivate' : 'Activate'}
                                            >
                                                {user.is_active ? <Lock className="w-4 h-4" /> : <Unlock className="w-4 h-4" />}
                                            </button>
                                            <button
                                                onClick={asyncHandler(() => handleDeleteUser(user), 'click')}
                                                className="text-red-600 hover:text-red-900"
                                                title="Delete User"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                            ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* Paging. `total` is the server's count for the active query, so a
                truncated list is never silent: the operator can always see that
                there are more users and step to them. */}
            {(total > PAGE_SIZE || offset > 0) && (
                <div className="flex items-center justify-between mt-4 text-sm text-secondary">
                    <span>
                        Showing {total === 0 ? 0 : offset + 1}–{Math.min(offset + endUsers.length, total)} of {total}
                        {filterStatus === 'inactive' && ' (inactive filter applies to this page only)'}
                    </span>
                    <div className="flex items-center gap-2">
                        <button
                            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                            disabled={offset === 0 || loading}
                            className="px-3 py-1 border border-strong rounded-md disabled:opacity-40 hover:bg-surface-alt"
                        >
                            Previous
                        </button>
                        <button
                            onClick={() => setOffset(offset + PAGE_SIZE)}
                            disabled={offset + endUsers.length >= total || loading}
                            className="px-3 py-1 border border-strong rounded-md disabled:opacity-40 hover:bg-surface-alt"
                        >
                            Next
                        </button>
                    </div>
                </div>
            )}

            {/* User Details Modal */}
            {showUserDetails && selectedUser && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-4xl w-full max-h-[90vh] overflow-y-auto">
                        <div className="flex items-center justify-between px-6 py-4 border-b">
                            <h3 className="text-lg font-semibold text-default">User Details</h3>
                            <button
                                onClick={() => setShowUserDetails(false)}
                                className="text-muted hover:text-secondary"
                            >
                                ×
                            </button>
                        </div>

                        {loadingDetails ? (
                            <div className="p-12 text-center">
                                <Loader className="w-8 h-8 animate-spin text-primary mx-auto mb-4" />
                                <p className="text-secondary">Loading user details...</p>
                            </div>
                        ) : (
                            <div className="p-6 space-y-6">
                                {/* User Info */}
                                <div>
                                    <h4 className="text-md font-semibold text-default mb-4">User Information</h4>
                                    <dl className="grid grid-cols-2 gap-4">
                                        <div>
                                            <dt className="text-sm font-medium text-muted">Username</dt>
                                            <dd className="text-sm text-default">{selectedUser.username}</dd>
                                        </div>
                                        <div>
                                            <dt className="text-sm font-medium text-muted">Email</dt>
                                            <dd className="text-sm text-default">{selectedUser.email}</dd>
                                        </div>
                                        <div>
                                            <dt className="text-sm font-medium text-muted">Display Name</dt>
                                            <dd className="text-sm text-default">{selectedUser.display_name || 'Not set'}</dd>
                                        </div>
                                        <div>
                                            <dt className="text-sm font-medium text-muted">Status</dt>
                                            <dd className="text-sm text-default">
                                                {selectedUser.is_active ? 'Active' : 'Inactive'} • {selectedUser.is_verified ? 'Verified' : 'Unverified'}
                                            </dd>
                                        </div>
                                    </dl>
                                </div>
                            </div>
                        )}

                        <div className="flex justify-end space-x-3 px-6 py-4 border-t bg-surface-alt">
                            <button
                                onClick={() => setShowUserDetails(false)}
                                className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt"
                            >
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Delete Confirmation Modal */}
            <GenericDeleteConfirmationModal
                isOpen={showDeleteModal}
                onClose={() => {
                    if (!isDeleting) {
                        setShowDeleteModal(false);
                        setUserToDelete(null);
                        setDeleteError(null);
                    }
                }}
                onConfirm={asyncHandler(confirmDeleteUser, 'confirm')}
                itemType="end-user"
                itemName={userToDelete?.username || ''}
                itemInfo={{
                    id: userToDelete?.id.toString() || '',
                    description: `${userToDelete?.email}${userToDelete?.display_name ? ` - ${userToDelete.display_name}` : ''}`,
                    enabled: userToDelete?.is_active
                }}
                isDeleting={isDeleting}
                errorMessage={deleteError}
            />

            {/* The dialog of the contributed action that is open */}
            {listActions
                .filter((action) => action.id === openListAction)
                .map((action) => (
                    <action.Dialog key={action.id} onClose={() => setOpenListAction(null)} />
                ))}

            {/* Create User Modal */}
            {showCreateModal && (
                <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
                    <div className="bg-surface rounded-lg border border-default shadow-xl w-full max-w-md p-6">
                        <h2 className="text-lg font-semibold text-default mb-4">Create End User</h2>
                        {createError && (
                            <div className="mb-4 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                                <p className="text-sm text-red-700 dark:text-red-400">{createError}</p>
                            </div>
                        )}
                        <form onSubmit={asyncHandler(async (e) => {
                            e.preventDefault();
                            // The Control Plane's rule, before anything is sent.
                            if (!passwordMeetsRule(createForm.password)) {
                                setCreateError(`Password must be ${PASSWORD_RULE_HINT}.`);
                                return;
                            }
                            setCreateLoading(true);
                            setCreateError('');
                            try {
                                const resp = await gatewayClient?.createEndUser(createForm);
                                if (resp?.success) {
                                    // Two-step photo upload — post create
                                    // succeeds, multipart POST the file to
                                    // the user-specific photo endpoint.
                                    // Failure here is non-fatal; the user
                                    // exists, the admin can re-upload from
                                    // the user-detail screen.
                                    const newUserId = resp?.user?.id;
                                    if (createPhotoFile && newUserId) {
                                        try {
                                            await gatewayClient?.uploadEndUserPhoto(newUserId, createPhotoFile);
                                        } catch (photoErr) {
                                             
                                            console.warn('User created but photo upload failed:', errorKind(photoErr));
                                            // The user exists: said first, then why the photo is not there.
                                            setCreateError(failureSentence('User created, but photo upload failed — try again from the user detail screen', photoErr));
                                            // Don't close modal so admin sees the warning.
                                            runBackground(loadEndUsers(), 'end users');
                                            return;
                                        }
                                    }
                                    setShowCreateModal(false);
                                    runBackground(loadEndUsers(), 'end users');
                                } else {
                                    setCreateError(resp?.detail || 'Failed to create user');
                                }
                            } catch (err) {
                                setCreateError(errorMessage(err, 'Failed to create user'));
                            } finally {
                                setCreateLoading(false);
                            }
                        }, 'submit')} className="space-y-3">
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">Username *</label>
                                <input
                                    type="text"
                                    value={createForm.username}
                                    onChange={e => setCreateForm(f => ({ ...f, username: e.target.value }))}
                                    className="w-full px-3 py-2 bg-surface border border-strong rounded-md text-default placeholder-muted focus:ring-2 focus:ring-primary focus:border-transparent"
                                    placeholder="johndoe"
                                    required
                                    minLength={3}
                                />
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">Email *</label>
                                <input
                                    type="email"
                                    value={createForm.email}
                                    onChange={e => setCreateForm(f => ({ ...f, email: e.target.value }))}
                                    className="w-full px-3 py-2 bg-surface border border-strong rounded-md text-default placeholder-muted focus:ring-2 focus:ring-primary focus:border-transparent"
                                    placeholder="john@example.com"
                                    required
                                />
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">Password *</label>
                                <div className="relative">
                                    <input
                                        type={showCreatePassword ? 'text' : 'password'}
                                        value={createForm.password}
                                        onChange={e => setCreateForm(f => ({ ...f, password: e.target.value }))}
                                        className="w-full px-3 py-2 pr-10 bg-surface border border-strong rounded-md text-default placeholder-muted focus:ring-2 focus:ring-primary focus:border-transparent"
                                        placeholder="Min. 8 characters"
                                        required
                                        minLength={8}
                                    />
                                    <button
                                        type="button"
                                        onClick={() => setShowCreatePassword(!showCreatePassword)}
                                        className="absolute inset-y-0 right-0 pr-3 flex items-center text-muted hover:text-secondary"
                                    >
                                        {showCreatePassword ? (
                                            <EyeOff className="h-4 w-4" />
                                        ) : (
                                            <Eye className="h-4 w-4" />
                                        )}
                                    </button>
                                </div>
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">Display Name</label>
                                <input
                                    type="text"
                                    value={createForm.display_name}
                                    onChange={e => setCreateForm(f => ({ ...f, display_name: e.target.value }))}
                                    className="w-full px-3 py-2 bg-surface border border-strong rounded-md text-default placeholder-muted focus:ring-2 focus:ring-primary focus:border-transparent"
                                    placeholder="John Doe"
                                />
                            </div>
                            <div>
                                <label className="block text-sm font-medium text-secondary mb-1">Profile Photo (Optional)</label>
                                <div className="flex items-center gap-3">
                                    {createPhotoPreview ? (
                                        <img
                                            src={createPhotoPreview}
                                            alt="Preview"
                                            className="w-12 h-12 rounded-full object-cover border border-strong"
                                        />
                                    ) : (
                                        <div className="w-12 h-12 rounded-full bg-gradient-to-br from-blue-100 to-purple-100 flex items-center justify-center text-blue-700 font-semibold">
                                            {(createForm.display_name || createForm.username)?.[0]?.toUpperCase() || '?'}
                                        </div>
                                    )}
                                    <input
                                        type="file"
                                        accept="image/jpeg,image/png,image/webp,image/gif"
                                        disabled={createLoading}
                                        onChange={(e) => {
                                            const f = e.target.files?.[0] ?? null;
                                            if (!f) {
                                                setCreatePhotoFile(null);
                                                setCreatePhotoPreview(null);
                                                return;
                                            }
                                            if (f.size > 5 * 1024 * 1024) {
                                                setCreateError('Photo must be under 5 MB');
                                                return;
                                            }
                                            if (!['image/jpeg', 'image/png', 'image/webp', 'image/gif'].includes(f.type)) {
                                                setCreateError(`Unsupported image type: ${f.type || 'unknown'}`);
                                                return;
                                            }
                                            setCreateError('');
                                            setCreatePhotoFile(f);
                                            const reader = new FileReader();
                                            reader.onload = () => setCreatePhotoPreview(String(reader.result ?? ''));
                                            reader.readAsDataURL(f);
                                        }}
                                        className="text-sm text-secondary"
                                    />
                                    {createPhotoFile && (
                                        <button
                                            type="button"
                                            onClick={() => { setCreatePhotoFile(null); setCreatePhotoPreview(null); }}
                                            className="text-xs text-muted hover:text-default underline"
                                        >
                                            Clear
                                        </button>
                                    )}
                                </div>
                            </div>
                            <div className="flex items-center gap-2 pt-1">
                                {/* The server defaults this to true, so a box that isn't
                                    sent emails the user even when it is unticked. It also
                                    never includes the password. */}
                                <input
                                    type="checkbox"
                                    id="send-welcome"
                                    checked={createForm.send_welcome_email}
                                    onChange={e => setCreateForm(f => ({ ...f, send_welcome_email: e.target.checked }))}
                                    className="w-4 h-4 rounded"
                                />
                                <label htmlFor="send-welcome" className="text-sm text-secondary">Send welcome email</label>
                            </div>
                            <div className="flex justify-end gap-2 pt-2">
                                <button
                                    type="button"
                                    onClick={() => setShowCreateModal(false)}
                                    className="px-4 py-2 text-secondary bg-surface border border-strong rounded-md hover:bg-surface-alt"
                                >
                                    Cancel
                                </button>
                                <button
                                    type="submit"
                                    disabled={createLoading}
                                    className="px-4 py-2 text-white bg-primary-600 rounded-md hover:bg-primary-700 disabled:opacity-50"
                                >
                                    {createLoading ? 'Creating...' : 'Create User'}
                                </button>
                            </div>
                        </form>
                    </div>
                </div>
            )}
        </div>
    );
};

export default EndUsersListTab;
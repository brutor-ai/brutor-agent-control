/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
    Users,
    UsersRound,
    User,
    UserPlus,
    UserMinus,
    Loader,
    Plus,
    Search,
    X,
    CheckCircle,
    Download,
    AlertCircle,
} from 'lucide-react';
import { ResourceGroup, GroupMember, formatGroupLabel } from '../../../lib/groupTypes';
import { useGateway } from '../../../hooks/useGateway';
import { useToast } from '../../common/Toast';
import GenericDeleteConfirmationModal from '../../common/GenericDeleteConfirmationModal';
import GroupMembersTableView from './GroupMembersTableView';
import GroupMemberUsageModal from './GroupMemberUsageModal';
import { useSlot } from '../../../app/slots';
import { useConfirm } from '../../common/ConfirmationDialog';
import { errorKind, errorMessage } from '../../../lib/narrow';
import { asyncHandler, runBackground } from '../../../lib/asyncSafe';
import { useReadFailures } from '../../../hooks/useRead';
import ReadFailureNotice from '../../common/ReadFailureNotice';

interface Props {
    group: ResourceGroup;
    onUpdate: () => void;
}

const GroupMembersTab: React.FC<Props> = ({ group, onUpdate }) => {
    const {
        fetchGroupMembers,
        removeGroupMember,
        addGroupMembers,
        fetchEndUsers,
        gatewayClient,
    } = useGateway();
    const toast = useToast();
    const confirm = useConfirm();

    const [members, setMembers] = useState<GroupMember[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [searchTerm, setSearchTerm] = useState('');
    const [removingId, setRemovingId] = useState<string | null>(null);
    const [deleteError, setDeleteError] = useState<string | null>(null);
    const [showAddModal, setShowAddModal] = useState(false);
    const [selectedMembers, setSelectedMembers] = useState<Set<string>>(new Set());
    // Actions on the members that this tab does not own: each is contributed
    // to the `resource-group.member-actions` slot (Send Message is). The
    // dialog of the one that is open is the only one mounted.
    const memberActions = useSlot('resource-group.member-actions');
    const [openMemberAction, setOpenMemberAction] = useState<string | null>(null);

    // Add members modal state
    const [availableUsers, setAvailableUsers] = useState<any[]>([]);
    const [userSearchTerm, setUserSearchTerm] = useState('');
    const [loadingUsers, setLoadingUsers] = useState(false);
    const [selectedUsersToAdd, setSelectedUsersToAdd] = useState<Set<string>>(new Set());
    const [addingMembers, setAddingMembers] = useState(false);
    // When enabled, the add-members call asks the backend to promote this
    // group to primary for each user. For users with no primary yet, the
    // backend auto-sets it regardless (first-join invariant).
    const [makeNewPrimary, setMakeNewPrimary] = useState(false);
    // User whose primary-group change is currently in flight.
    const [settingPrimaryId, setSettingPrimaryId] = useState<string | null>(null);

    // Per-selection summary of who already has a primary group.
    // Drives the checkbox's autocheck/disabled state and the
    // contextual hint shown beneath it. Recomputed when the
    // selection or the available-users list changes.
    //
    // `allLackPrimary`: every selected user has primary_group_id =
    //   null. In that case the backend's first-join rule will
    //   promote this group regardless of the checkbox — we mirror
    //   that by force-checking + disabling so the UI matches
    //   reality. Admin doesn't think "I unchecked it but the
    //   system ignored me".
    //
    // `someLackPrimary`: at least one selected user has no
    //   primary. We leave the checkbox interactive but surface
    //   that the no-primary subset will be promoted no matter
    //   what — same first-join rule, just a less stark form.
    const selectedPrimarySummary = useMemo(() => {
        if (selectedUsersToAdd.size === 0) {
            return { allLackPrimary: false, someLackPrimary: false, noPrimaryCount: 0 };
        }
        const selected = availableUsers.filter((u) =>
            selectedUsersToAdd.has(u.id),
        );
        if (selected.length === 0) {
            return { allLackPrimary: false, someLackPrimary: false, noPrimaryCount: 0 };
        }
        const noPrimary = selected.filter((u) => !u.primary_group_id);
        return {
            allLackPrimary: noPrimary.length === selected.length,
            someLackPrimary: noPrimary.length > 0 && noPrimary.length < selected.length,
            noPrimaryCount: noPrimary.length,
        };
    }, [selectedUsersToAdd, availableUsers]);

    // Effective checkbox state. When every selected user lacks a
    // primary, force-check so the UI matches what the backend
    // will actually do — and pass `make_primary: true` to spare
    // the admin the "wait, why did it set primary anyway?"
    // confusion.
    const effectiveMakePrimary = selectedPrimarySummary.allLackPrimary || makeNewPrimary;

    // Remove member confirmation state
    const [removeConfirmation, setRemoveConfirmation] = useState<{
        userId: string;
        username: string;
        email?: string;
    } | null>(null);

    // Per-member Usage modal — renders when an operator clicks
    // the BarChart3 icon on a row. Carries the full identity
    // tuple so the modal header reads naturally without a
    // second member-list lookup.
    const [usageMember, setUsageMember] = useState<GroupMember | null>(null);

    // Bound EndUserGroups for this Resource Group.
    // Operators can grant access either by adding individual users
    // (above) OR by binding a whole user group.
    const [boundUserGroups, setBoundUserGroups] = useState<any[]>([]);
    const [loadingUserGroups, setLoadingUserGroups] = useState(false);
    const [readFailure, reads] = useReadFailures<'userGroups'>();
    const [showAddUserGroupModal, setShowAddUserGroupModal] = useState(false);
    const [availableUserGroups, setAvailableUserGroups] = useState<any[]>([]);
    const [selectedUserGroupsToAdd, setSelectedUserGroupsToAdd] = useState<Set<string>>(
        new Set(),
    );
    const [userGroupSearchTerm, setUserGroupSearchTerm] = useState('');

    useEffect(() => {
        runBackground(loadMembers(), 'members');
        runBackground(loadBoundUserGroups(), 'bound user groups');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [group.id]);

    const loadMembers = async () => {
        try {
            setLoading(true);
            setError('');
            // Default API call returns direct + inherited members (from parent, not descendants)
            const response = await fetchGroupMembers(group.id);
            setMembers(response.members || []);
        } catch (err) {
            console.error('Failed to load members:', errorKind(err));
            setError(errorMessage(err, 'Failed to load members'));
        } finally {
            setLoading(false);
        }
    };

    const loadBoundUserGroups = useCallback(async () => {
        try {
            setLoadingUserGroups(true);
            const r = await gatewayClient.fetchResourceGroupBoundUserGroups(group.id);
            setBoundUserGroups(r.end_user_groups || []);
            reads.loaded('userGroups');
        } catch (err) {
            reads.failed('userGroups', 'Failed to load the user groups bound to this group', err);
        } finally {
            setLoadingUserGroups(false);
        }
    }, [gatewayClient, group.id, reads]);

    const openAddUserGroupModal = async () => {
        setShowAddUserGroupModal(true);
        setSelectedUserGroupsToAdd(new Set());
        try {
            const res = await gatewayClient.fetchEndUserGroups({ limit: 500 });
            // Exclude already-bound groups.
            const boundIds = new Set(boundUserGroups.map((g) => g.id));
            setAvailableUserGroups(
                (res.groups || []).filter((g: any) => !boundIds.has(g.id)),
            );
        } catch (err) {
            console.error('Failed to load available user groups:', errorKind(err));
            setAvailableUserGroups([]);
            toast.error('Failed to load the user groups', errorMessage(err, '') || undefined);
        }
    };

    const handleBindUserGroups = async () => {
        if (selectedUserGroupsToAdd.size === 0) return;
        try {
            await gatewayClient.bindResourceGroupToUserGroups(
                group.id,
                Array.from(selectedUserGroupsToAdd),
            );
            toast.success(
                'User groups bound',
                `${selectedUserGroupsToAdd.size} user group(s) attached to ${formatGroupLabel(group)}.`,
            );
            setShowAddUserGroupModal(false);
            setSelectedUserGroupsToAdd(new Set());
            await loadBoundUserGroups();
        } catch (err) {
            toast.error(
                'Failed to bind user groups',
                errorMessage(err, 'Unable to bind user groups'),
            );
        }
    };

    const handleUnbindUserGroup = async (ugId: string, name: string) => {
        try {
            await gatewayClient.unbindResourceGroupFromUserGroups(group.id, [ugId]);
            toast.success(
                'User group unbound',
                `Removed "${name}" from ${formatGroupLabel(group)}.`,
            );
            await loadBoundUserGroups();
        } catch (err) {
            toast.error(
                'Failed to unbind user group',
                errorMessage(err, 'Unable to unbind user group'),
            );
        }
    };

    const filteredAvailableUserGroups = availableUserGroups.filter((g) => {
        if (!userGroupSearchTerm.trim()) return true;
        const needle = userGroupSearchTerm.toLowerCase();
        return (
            (g.name || '').toLowerCase().includes(needle) ||
            (g.display_name || '').toLowerCase().includes(needle)
        );
    });

    // Set this group as the user's primary (active runtime) resource group.
    // Backend validates the user is already a member; we optimistically
    // flip the local `is_primary` flag on that user and rely on a full
    // reload to correct any stale state.
    const handleSetPrimary = async (userId: string, username: string) => {
        try {
            setSettingPrimaryId(userId);
            await gatewayClient.setUserPrimaryGroup(userId, group.id);
            toast.success(
                'Primary group updated',
                `${username}'s primary group is now ${formatGroupLabel(group)}.`,
            );
            await loadMembers();
        } catch (err) {
            console.error('Failed to set primary group:', errorKind(err));
            toast.error(
                'Failed to set primary group',
                errorMessage(err, 'Unable to update the user\'s primary group.'),
            );
        } finally {
            setSettingPrimaryId(null);
        }
    };

    const handleRemoveMember = (userId: string, username: string) => {
        // Show confirmation modal
        setDeleteError(null);
        const member = members.find(m => m.user_id === userId);
        setRemoveConfirmation({
            userId,
            username,
            email: member?.email
        });
    };

    const confirmRemoveMember = async () => {
        if (!removeConfirmation) return;

        const { userId, username } = removeConfirmation;

        try {
            setRemovingId(userId);
            setError('');
            await removeGroupMember(group.id, userId);
            await loadMembers();
            onUpdate();

            // Show success toast
            toast.success('Member Removed', `${username} has been removed from ${formatGroupLabel(group)}.`);

            // Close modal
            setRemoveConfirmation(null);
        } catch (err: any) {
            console.error('Failed to remove member:', errorKind(err));
            setDeleteError(err.detail || err.message || 'Failed to remove member');
        } finally {
            setRemovingId(null);
        }
    };

    const handleSelectMember = (userId: string) => {
        const newSelected = new Set(selectedMembers);
        if (newSelected.has(userId)) {
            newSelected.delete(userId);
        } else {
            newSelected.add(userId);
        }
        setSelectedMembers(newSelected);
    };

    const handleSelectAll = () => {
        // Only select direct members (not inherited)
        const directMembers = filteredMembers.filter(m => !m.is_inherited);

        if (selectedMembers.size === directMembers.length && directMembers.length > 0) {
            setSelectedMembers(new Set());
        } else {
            setSelectedMembers(new Set(directMembers.map(m => m.user_id)));
        }
    };

    const handleBulkRemove = async () => {
        if (selectedMembers.size === 0) return;
        if (!(await confirm({
            title: 'Remove members?',
            message: `${selectedMembers.size} member(s) will be removed from "${formatGroupLabel(group)}". They keep any access inherited from other groups.`,
            confirmLabel: `Remove ${selectedMembers.size} member(s)`,
        }))) return;

        const ids = Array.from(selectedMembers);
        const usernameById = new Map(members.map(m => [m.user_id, m.username]));

        // Fan out the per-member DELETEs concurrently. allSettled
        // lets a single 403 / 404 surface as a partial-failure
        // toast instead of aborting the whole batch — the user
        // wants the ones that succeeded reflected even if a few
        // fail (e.g. concurrent edits, stale group state).
        const results = await Promise.allSettled(
            ids.map(id => removeGroupMember(group.id, id)),
        );
        const failures = results
            .map((r, i) => (r.status === 'rejected'
                ? { id: ids[i], username: usernameById.get(ids[i]) || ids[i], err: (r.reason)?.message || String(r.reason) }
                : null))
            .filter((x): x is { id: string; username: string; err: string } => x !== null);
        const successCount = ids.length - failures.length;

        await loadMembers();
        onUpdate();
        setSelectedMembers(new Set());

        if (failures.length === 0) {
            toast.success(
                'Members removed',
                `${successCount} member(s) removed from ${formatGroupLabel(group)}.`,
            );
        } else if (successCount === 0) {
            toast.error(
                'Bulk remove failed',
                `Could not remove any of the ${failures.length} selected member(s). First error: ${failures[0].err}`,
            );
        } else {
            toast.error(
                'Partial bulk remove',
                `Removed ${successCount} of ${ids.length}. ${failures.length} failed (e.g. ${failures[0].username}: ${failures[0].err}).`,
            );
        }
    };

    // Load available users for adding to group
    const loadAvailableUsers = async (search?: string) => {
        try {
            setLoadingUsers(true);
            const response = await fetchEndUsers({
                search: search || '',
                active_only: true,
                limit: 50
            });

            // Filter out users who are already members
            const existingUserIds = new Set(members.map(m => m.user_id));
            const filteredUsers = response.users.filter(u => !existingUserIds.has(u.id));

            setAvailableUsers(filteredUsers);
        } catch (err) {
            console.error('Failed to load users:', errorKind(err));
            setError(errorMessage(err, 'Failed to load users'));
        } finally {
            setLoadingUsers(false);
        }
    };

    // Handle opening add members modal.
    //
    // Resets ALL transient modal state, including `makeNewPrimary`.
    // A close path that left the checkbox state in place would
    // re-render it checked on the next open, and a submit would
    // silently send `make_primary: true` when the admin assumes a
    // clean slate. Resetting here makes the modal's initial state
    // authoritative regardless of how it was closed.
    const handleOpenAddModal = () => {
        setShowAddModal(true);
        setUserSearchTerm('');
        setSelectedUsersToAdd(new Set());
        setMakeNewPrimary(false);
        runBackground(loadAvailableUsers(), 'available users');
    };

    // Single helper for every modal-close path, the X-button included,
    // so each resets the whole of the modal's state (see
    // `handleOpenAddModal`).
    const handleCloseAddModal = () => {
        setShowAddModal(false);
        setSelectedUsersToAdd(new Set());
        setMakeNewPrimary(false);
        setUserSearchTerm('');
    };

    // Handle adding selected users to group
    const handleAddMembers = async () => {
        if (selectedUsersToAdd.size === 0) return;

        try {
            setAddingMembers(true);
            setError('');

            const userIds = Array.from(selectedUsersToAdd);
            // Use `effectiveMakePrimary` so the all-lack-primary
            // case sends `true` explicitly. Functionally identical
            // to the backend's first-join rule, but keeps the
            // request consistent with what the admin sees in the UI.
            const result = await addGroupMembers(group.id, userIds, 'member', effectiveMakePrimary);

            await loadMembers();
            setShowAddModal(false);
            setSelectedUsersToAdd(new Set());
            setMakeNewPrimary(false);

            const addedCount = result?.added?.length || 0;
            const skipped = result?.skipped || [];

            if (addedCount > 0 && skipped.length === 0) {
                toast.success('Members Added', `${addedCount} ${addedCount === 1 ? 'member' : 'members'} added to ${formatGroupLabel(group)}.`);
            } else if (addedCount > 0 && skipped.length > 0) {
                const skippedReasons = skipped.map(s => `${s.username}: ${s.reason}`).join('\n');
                toast.warning('Partially Added',
                    `${addedCount} added, ${skipped.length} skipped:\n${skippedReasons}`);
            } else if (skipped.length > 0) {
                const skippedReasons = skipped.map(s => `${s.username}: ${s.reason}`).join('\n');
                toast.warning('No Members Added',
                    `All ${skipped.length} ${skipped.length === 1 ? 'user was' : 'users were'} skipped:\n${skippedReasons}`);
            }
        } catch (err) {
            console.error('Failed to add members:', errorKind(err));
            setError(errorMessage(err, 'Failed to add members'));
            toast.error('Failed to Add Members', errorMessage(err, 'Unable to add members to the group.'));
        } finally {
            setAddingMembers(false);
        }
    };

    // Toggle user selection for adding
    const handleToggleUserSelection = (userId: string) => {
        const newSelected = new Set(selectedUsersToAdd);
        if (newSelected.has(userId)) {
            newSelected.delete(userId);
        } else {
            newSelected.add(userId);
        }
        setSelectedUsersToAdd(newSelected);
    };

    // Filter members by search term
    const filteredMembers = members.filter(member =>
        member.username.toLowerCase().includes(searchTerm.toLowerCase()) ||
        member.email?.toLowerCase().includes(searchTerm.toLowerCase()) ||
        member.display_name?.toLowerCase().includes(searchTerm.toLowerCase())
    );

    return (
        <div className="p-6 space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h2 className="text-lg font-semibold text-default">Group Members</h2>
                    <p className="text-sm text-secondary mt-1">
                        Manage end users who belong to this group
                    </p>
                </div>
                <div className="flex items-center space-x-2">
                    {selectedMembers.size > 0 && (
                        <button
                            onClick={asyncHandler(handleBulkRemove, 'click')}
                            className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors flex items-center space-x-2"
                        >
                            <UserMinus className="w-4 h-4" />
                            <span>Remove ({selectedMembers.size})</span>
                        </button>
                    )}
                    {memberActions.map((action) => (
                        <action.Button
                            key={action.id}
                            group={group}
                            memberCount={members.length}
                            onOpen={() => setOpenMemberAction(action.id)}
                        />
                    ))}
                    <button
                        onClick={handleOpenAddModal}
                        className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors flex items-center space-x-2"
                    >
                        <UserPlus className="w-4 h-4" />
                        <span>Add Members</span>
                    </button>
                </div>
            </div>

            {/* Error Message */}
            {error && (
                <div className="flex items-start space-x-3 p-4 bg-surface-alt border border-default rounded-lg">
                    <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
                    <div className="flex-1 min-h-0 min-w-0">
                        <p className="text-sm font-medium text-red-900">Error</p>
                        <p className="text-sm text-red-700 mt-1">{error}</p>
                    </div>
                </div>
            )}

            {/* Simple Statistics Summary */}
            <div className="flex items-center justify-between p-4 bg-surface-alt border border-default rounded-lg">
                <div className="flex items-center space-x-4">
                    <Users className="w-5 h-5 text-secondary" />
                    <div>
                        <p className="text-sm font-medium text-default">
                            <span className="text-lg font-semibold text-primary-600">{members.length}</span> {members.length === 1 ? 'member' : 'members'}
                        </p>
                    </div>
                </div>
            </div>

            {/* Bound End-User Groups.
                Operators reach this tab to ask "who can use this RG?".
                Showing the user-group bindings here (in addition to
                individual members) reflects the actual access reality:
                an EndUserGroup attached to this RG grants access to every
                current member of that group. */}
            <div className="bg-surface border border-default rounded-lg p-4">
                <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center space-x-2">
                        <UsersRound className="w-4 h-4 text-secondary" />
                        <h3 className="text-sm font-semibold text-default">
                            Bound User Groups ({boundUserGroups.length})
                        </h3>
                    </div>
                    <button
                        onClick={asyncHandler(openAddUserGroupModal, 'click')}
                        className="px-3 py-1.5 border rounded-md text-sm flex items-center space-x-1 hover:bg-surface-raised"
                    >
                        <Plus className="w-4 h-4" />
                        <span>Bind User Group</span>
                    </button>
                </div>
                {loadingUserGroups ? (
                    <div className="text-sm text-muted">Loading…</div>
                ) : readFailure('userGroups') && boundUserGroups.length === 0 ? (
                    <ReadFailureNotice compact failure={readFailure('userGroups')} onRetry={() => runBackground(loadBoundUserGroups(), 'user groups')} />
                ) : boundUserGroups.length === 0 ? (
                    <p className="text-sm text-muted italic">
                        No user groups bound. Bind an EndUserGroup here to grant
                        every current member access to this Resource Group.
                    </p>
                ) : (
                    <ul className="divide-y border rounded">
                        {boundUserGroups.map((g) => (
                            <li
                                key={g.id}
                                className="flex items-center justify-between px-3 py-2"
                            >
                                <div className="min-w-0">
                                    <div className="flex items-center space-x-2">
                                        <span className="text-sm font-medium text-default truncate">
                                            {g.display_name || g.name}
                                        </span>
                                        <span
                                            className={
                                                'inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium ' +
                                                'bg-surface-alt text-secondary border'
                                            }
                                        >
                                            {!g.source || g.source === 'manual' ? 'MANUAL' : g.source}
                                        </span>
                                        <span className="text-xs text-muted">
                                            {g.member_count} member{g.member_count === 1 ? '' : 's'}
                                        </span>
                                    </div>
                                </div>
                                <button
                                    onClick={asyncHandler(() => handleUnbindUserGroup(g.id, g.display_name || g.name), 'click')}
                                    className="p-1 text-muted hover:text-red-600"
                                    aria-label={`Unbind ${g.name}`}
                                    title="Unbind from this Resource Group"
                                >
                                    <X className="w-4 h-4" />
                                </button>
                            </li>
                        ))}
                    </ul>
                )}
            </div>

            {/* Filters and Actions */}
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center space-y-3 sm:space-y-0 sm:space-x-4">
                {/* Search */}
                <div className="flex-1 relative min-h-0 min-w-0">
                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                    <input
                        type="text"
                        value={searchTerm}
                        onChange={(e) => setSearchTerm(e.target.value)}
                        placeholder="Search by name, email, or username..."
                        className="w-full pl-10 pr-10 py-2 border border-strong rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
                    />
                    {searchTerm && (
                        <button
                            onClick={() => setSearchTerm('')}
                            className="absolute right-3 top-1/2 transform -translate-y-1/2 text-muted hover:text-secondary"
                        >
                            <X className="w-4 h-4" />
                        </button>
                    )}
                </div>

                {/* Export Button */}
                <button className="px-4 py-2 bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors flex items-center space-x-2">
                    <Download className="w-4 h-4 text-secondary" />
                    <span className="text-sm font-medium text-secondary">Export</span>
                </button>
            </div>

            {/* Members Table */}
            {loading ? (
                <div className="flex items-center justify-center h-48">
                    <div className="text-center">
                        <Loader className="w-8 h-8 text-primary-600 animate-spin mx-auto mb-2" />
                        <p className="text-sm text-secondary">Loading members...</p>
                    </div>
                </div>
            ) : filteredMembers.length === 0 ? (
                <div className="text-center py-12 bg-surface-alt rounded-lg border-2 border-dashed border-strong">
                    <Users className="w-12 h-12 text-muted mx-auto mb-3" />
                    <p className="text-default font-medium mb-1">
                        {searchTerm
                            ? 'No members found matching your search'
                            : 'No members yet'}
                    </p>
                    {!searchTerm && (
                        <p className="text-sm text-muted mt-2">
                            Click "Add Members" to add end users to this group
                        </p>
                    )}
                </div>
            ) : (
                <GroupMembersTableView
                    members={filteredMembers}
                    selectedMembers={selectedMembers}
                    onSelectMember={handleSelectMember}
                    onSelectAll={handleSelectAll}
                    onRemove={handleRemoveMember}
                    removingId={removingId}
                    onSetPrimary={asyncHandler(handleSetPrimary, 'set primary')}
                    settingPrimaryId={settingPrimaryId}
                    onShowUsage={(member) => setUsageMember(member)}
                />
            )}

            {/* Add Members Modal */}
            {showAddModal && (
                <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
                    <div className="bg-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[80vh] overflow-y-auto">
                        <div className="flex items-center justify-between p-6 border-b border-default">
                            <h2 className="text-xl font-semibold text-default">
                                Add Members
                            </h2>
                            <button
                                onClick={handleCloseAddModal}
                                className="text-muted hover:text-secondary transition-colors"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <div className="p-6">
                            {/* Search Users */}
                            <div className="mb-4">
                                <label className="block text-sm font-medium text-secondary mb-2">
                                    Search Users
                                </label>
                                <div className="relative">
                                    <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted" />
                                    <input
                                        type="text"
                                        value={userSearchTerm}
                                        onChange={(e) => {
                                            setUserSearchTerm(e.target.value);
                                            runBackground(loadAvailableUsers(e.target.value), 'available users');
                                        }}
                                        placeholder="Search by name or email..."
                                        className="w-full pl-10 pr-4 py-2 border border-strong rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                                    />
                                </div>
                            </div>

                            {/* User List */}
                            <div className="border border-default rounded-lg max-h-96 overflow-y-auto">
                                {loadingUsers ? (
                                    <div className="p-8 text-center">
                                        <Loader className="w-6 h-6 animate-spin text-primary mx-auto mb-2" />
                                        <p className="text-sm text-secondary">Loading users...</p>
                                    </div>
                                ) : availableUsers.length === 0 ? (
                                    <div className="p-8 text-center">
                                        <User className="w-12 h-12 text-muted mx-auto mb-3" />
                                        <p className="text-sm text-secondary">
                                            {userSearchTerm ? 'No users found' : 'No available users to add'}
                                        </p>
                                        <p className="text-xs text-muted mt-1">
                                            {userSearchTerm ? 'Try a different search term' : 'All users are already members'}
                                        </p>
                                    </div>
                                ) : (
                                    <div className="divide-y divide-default">
                                        {availableUsers.map((user) => (
                                            <div
                                                key={user.id}
                                                className={`p-3 hover:bg-surface-alt cursor-pointer transition-colors ${
                                                    selectedUsersToAdd.has(user.id) ? 'bg-primary-50' : ''
                                                }`}
                                                onClick={() => handleToggleUserSelection(user.id)}
                                            >
                                                <div className="flex items-center space-x-3">
                                                    <input
                                                        type="checkbox"
                                                        checked={selectedUsersToAdd.has(user.id)}
                                                        onChange={() => handleToggleUserSelection(user.id)}
                                                        className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary"
                                                        onClick={(e) => e.stopPropagation()}
                                                    />
                                                    <div className="flex-1 min-w-0 min-h-0">
                                                        <p className="text-sm font-medium text-default">
                                                            {user.username}
                                                        </p>
                                                        {user.email && (
                                                            <p className="text-xs text-secondary truncate">
                                                                {user.email}
                                                            </p>
                                                        )}
                                                    </div>
                                                    {selectedUsersToAdd.has(user.id) && (
                                                        <CheckCircle className="w-5 h-5 text-primary-600 flex-shrink-0" />
                                                    )}
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {/* Selected Count */}
                            {selectedUsersToAdd.size > 0 && (
                                <div className="mt-4 p-3 bg-primary-50 border border-primary-200 rounded-lg">
                                    <p className="text-sm text-primary-900">
                                        <span className="font-semibold">{selectedUsersToAdd.size}</span> {selectedUsersToAdd.size === 1 ? 'user' : 'users'} selected
                                    </p>
                                </div>
                            )}
                        </div>

                        {/* Primary-group option */}
                        <div className="px-6 py-3 border-t border-default bg-surface space-y-2">
                            <label
                                className={`flex items-start gap-3 text-sm ${
                                    selectedPrimarySummary.allLackPrimary
                                        ? 'cursor-default'
                                        : 'cursor-pointer'
                                }`}
                            >
                                <input
                                    type="checkbox"
                                    checked={effectiveMakePrimary}
                                    disabled={selectedPrimarySummary.allLackPrimary}
                                    onChange={(e) => setMakeNewPrimary(e.target.checked)}
                                    className="mt-0.5 rounded border-strong text-primary-600 focus:ring-primary-500 disabled:opacity-60"
                                />
                                <span className="text-secondary">
                                    <span className="font-medium text-default">
                                        Make this the primary group for the selected users
                                    </span>
                                    <br />
                                    {/*
                                       Substantive explanation of what "primary" means
                                       end-to-end. The previous one-liner left admins
                                       guessing whether this was a presentation default,
                                       a billing scope, or something else entirely.
                                    */}
                                    <span className="text-xs text-muted">
                                        <span className="font-medium">What "primary" means:</span>{' '}
                                        the workspace a user lands in by default when they
                                        sign into the portal. It scopes which AI models, MCP
                                        servers, knowledge bases, and chat history they see
                                        first — they can still switch to any of their other
                                        groups via the workspace switcher in the portal
                                        header. Usage, budget, and governance for actions
                                        taken in a session are attributed to whichever group
                                        is active at the time.
                                    </span>
                                </span>
                            </label>

                            {/* Per-selection contextual hint. Renders only when
                               selected users include someone with no primary
                               group yet, since that's the case where the
                               checkbox's effect diverges from the literal
                               checkbox state. */}
                            {selectedPrimarySummary.allLackPrimary && (
                                <div className="flex items-start gap-2 p-2 rounded-md bg-amber-50 border border-amber-200 text-xs text-amber-800">
                                    <AlertCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                                    <span>
                                        Every selected user is being added to their first
                                        group, so this group becomes their primary
                                        automatically — the checkbox is locked on for that
                                        reason. You can change a user's primary later from
                                        the members list (star icon) or from their portal
                                        workspace switcher.
                                    </span>
                                </div>
                            )}
                            {selectedPrimarySummary.someLackPrimary && (
                                <div className="flex items-start gap-2 p-2 rounded-md bg-amber-50 border border-amber-200 text-xs text-amber-800">
                                    <AlertCircle className="w-3.5 h-3.5 mt-0.5 flex-shrink-0" />
                                    <span>
                                        {selectedPrimarySummary.noPrimaryCount} of the selected
                                        users have no primary group yet — for those users this
                                        group becomes primary regardless of the checkbox above.
                                        Users who already have a primary group keep it unless
                                        the box is checked.
                                    </span>
                                </div>
                            )}
                        </div>

                        {/* Modal Footer */}
                        <div className="flex items-center justify-end space-x-3 p-6 border-t border-default bg-surface-alt">
                            <button
                                onClick={handleCloseAddModal}
                                disabled={addingMembers}
                                className="px-4 py-2 text-secondary bg-surface border border-strong rounded-lg hover:bg-surface-alt transition-colors disabled:opacity-50"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={asyncHandler(handleAddMembers, 'click')}
                                disabled={selectedUsersToAdd.size === 0 || addingMembers}
                                className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center space-x-2"
                            >
                                {addingMembers ? (
                                    <>
                                        <Loader className="w-4 h-4 animate-spin" />
                                        <span>Adding...</span>
                                    </>
                                ) : (
                                    <>
                                        <UserPlus className="w-4 h-4" />
                                        <span>Add {selectedUsersToAdd.size > 0 ? `(${selectedUsersToAdd.size})` : 'Members'}</span>
                                    </>
                                )}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Remove Member Confirmation Modal */}
            {removeConfirmation && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => {
                        setRemoveConfirmation(null);
                        setDeleteError(null);
                    }}
                    onConfirm={asyncHandler(confirmRemoveMember, 'confirm')}
                    action="remove"
                    itemType="member"
                    itemName={removeConfirmation.username}
                    itemInfo={{
                        id: removeConfirmation.userId,
                        description: removeConfirmation.email || 'Group member',
                        enabled: true,
                    }}
                    customWarning={`This will remove ${removeConfirmation.username} from ${formatGroupLabel(group)}.`}
                    customNote="The user account will not be deleted, only their group membership will be removed."
                    isDeleting={removingId === removeConfirmation.userId}
                    errorMessage={deleteError}
                />
            )}

            {memberActions
                .filter((action) => action.id === openMemberAction)
                .map((action) => (
                    <action.Dialog key={action.id} group={group} onClose={() => setOpenMemberAction(null)} />
                ))}

            {usageMember && (
                <GroupMemberUsageModal
                    groupId={group.id}
                    groupName={formatGroupLabel(group)}
                    userId={usageMember.user_id}
                    username={usageMember.username}
                    displayName={usageMember.display_name}
                    onClose={() => setUsageMember(null)}
                />
            )}

            {/* Bind-user-group picker. */}
            {showAddUserGroupModal && (
                <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
                    <div className="bg-surface rounded-lg border w-full max-w-lg">
                        <div className="flex items-center justify-between px-4 py-3 border-b">
                            <h4 className="font-semibold">
                                Bind a User Group to {formatGroupLabel(group)}
                            </h4>
                            <button
                                onClick={() => setShowAddUserGroupModal(false)}
                                className="text-muted hover:text-secondary"
                                aria-label="Close"
                            >
                                <X className="w-5 h-5" />
                            </button>
                        </div>
                        <div className="p-4 space-y-3">
                            <input
                                type="text"
                                value={userGroupSearchTerm}
                                onChange={(e) => setUserGroupSearchTerm(e.target.value)}
                                placeholder="Search user groups…"
                                className="w-full px-3 py-2 border rounded text-sm bg-surface"
                                autoFocus
                            />
                            <div className="max-h-72 overflow-y-auto border rounded">
                                {filteredAvailableUserGroups.length === 0 ? (
                                    <p className="p-3 text-sm text-muted italic">
                                        {availableUserGroups.length === 0
                                            ? 'No user groups available. Create one in User Management → User Groups first.'
                                            : 'No user groups match the search.'}
                                    </p>
                                ) : (
                                    filteredAvailableUserGroups.map((g: any) => (
                                        <label
                                            key={g.id}
                                            className="flex items-center px-3 py-2 hover:bg-surface-raised cursor-pointer text-sm border-b last:border-b-0"
                                        >
                                            <input
                                                type="checkbox"
                                                checked={selectedUserGroupsToAdd.has(g.id)}
                                                onChange={(e) => {
                                                    const next = new Set(
                                                        selectedUserGroupsToAdd,
                                                    );
                                                    if (e.target.checked) {
                                                        next.add(g.id);
                                                    } else {
                                                        next.delete(g.id);
                                                    }
                                                    setSelectedUserGroupsToAdd(next);
                                                }}
                                                className="mr-2"
                                            />
                                            <div className="min-w-0 flex-1 min-h-0">
                                                <div className="font-medium truncate">
                                                    {g.display_name || g.name}
                                                </div>
                                                <div className="text-xs text-muted truncate">
                                                    {g.source.toUpperCase()} ·{' '}
                                                    {g.member_count} member
                                                    {g.member_count === 1 ? '' : 's'}
                                                </div>
                                            </div>
                                        </label>
                                    ))
                                )}
                            </div>
                        </div>
                        <div className="px-4 py-3 border-t flex justify-end space-x-2">
                            <button
                                onClick={() => setShowAddUserGroupModal(false)}
                                className="px-3 py-2 border rounded text-sm"
                            >
                                Cancel
                            </button>
                            <button
                                onClick={asyncHandler(handleBindUserGroups, 'click')}
                                disabled={selectedUserGroupsToAdd.size === 0}
                                className="px-3 py-2 bg-primary-600 hover:bg-primary-700 text-white rounded text-sm disabled:opacity-50"
                            >
                                Bind {selectedUserGroupsToAdd.size || ''}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default GroupMembersTab;
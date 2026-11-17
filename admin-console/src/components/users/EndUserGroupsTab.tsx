/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * EndUserGroup admin UI.
 *
 * Mounted as a sub-tab under "User Management" → "User Groups", this
 * surface lets operators:
 *
 *   * List end-user groups, filtered by source (all, or the manual ones)
 *     and free-text search.
 *   * Create manual groups. A group an identity provider created is
 *     listed by its stored source, and its roster is not changed here.
 *   * Inspect a group's roster (live, with add / remove / set-to-list
 *     mutations) and which Resource Groups it currently grants access
 *     to.
 *   * Bind / unbind to Resource Groups so every current member
 *     inherits the binding.
 *   * Rename / re-describe manual groups. A group an identity provider
 *     created can be re-described but not renamed.
 *
 * Heavy lifting (reconciliation, idempotency, cross-tenant guards)
 * lives in the server-side `EndUserGroupManager`. This component is
 * a thin shell over the REST API.
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Loader,
    Plus,
    RefreshCw,
    Search,
    Trash2,
    UserPlus,
    UserMinus,
    Users,
    X,
} from 'lucide-react';
import InfoBanner from '../common/InfoBanner';
import GenericDeleteConfirmationModal from '../common/GenericDeleteConfirmationModal';
import { useGateway } from '../../hooks/useGateway';
import { formatGroupLabel } from '../../lib/groupTypes';
import { TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import { errorMessage } from '../../lib/narrow';
import { asyncHandler, runBackground } from '../../lib/asyncSafe';

// -----------------------------------------------------------------------------
// Types — locally declared rather than imported, to avoid coupling the UI to
// generated client types that aren't published yet.
// -----------------------------------------------------------------------------

/** `manual`, or the source an identity provider stored on a group it created. */
type GroupSource = string;

interface GroupSummary {
    id: string;
    tenant_id: string;
    name: string;
    display_name?: string | null;
    description?: string | null;
    source: GroupSource;
    source_external_id?: string | null;
    idp_server_id?: string | null;
    member_count: number;
    created_at?: string | null;
    updated_at?: string | null;
}

interface GroupMember {
    id: number;
    username: string;
    email?: string | null;
    display_name?: string | null;
    is_active: boolean;
}

interface RGRef {
    id: string;
    name: string;
    display_name?: string | null;
}

interface GroupDetail extends GroupSummary {
    members: GroupMember[];
    resource_groups: RGRef[];
}

interface EndUserOption {
    id: number;
    username: string;
    email?: string;
}

interface ResourceGroupOption {
    id: string;
    name: string;
    display_name?: string | null;
    group_type?: string | null;
}

// -----------------------------------------------------------------------------
// Top-level component
// -----------------------------------------------------------------------------

const PAGE_SIZE = 50;

/** The source of a group as the list shows it: `Manual`, or a provider's source as stored. */
const sourceLabel = (source: GroupSource): string => (source === 'manual' ? 'MANUAL' : source);
const SOURCE_BADGE = 'bg-surface-raised text-secondary border border-default';

const EndUserGroupsTab: React.FC = () => {
    const { gatewayClient } = useGateway();

    const [groups, setGroups] = useState<GroupSummary[]>([]);
    const [total, setTotal] = useState(0);
    const [offset, setOffset] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [searchTerm, setSearchTerm] = useState('');
    const [sourceFilter, setSourceFilter] = useState<'all' | GroupSource>(
        'all'
    );

    const [showCreate, setShowCreate] = useState(false);
    const [pendingDelete, setPendingDelete] = useState<GroupSummary | null>(
        null
    );
    const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);

    // ---------------------------- data loading ---------------------------

    const loadGroups = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await gatewayClient.fetchEndUserGroups({
                search: searchTerm || undefined,
                source: sourceFilter === 'all' ? undefined : sourceFilter,
                limit: PAGE_SIZE,
                offset,
            });
            // Past the end of the list (the last row of the last page was just
            // deleted): step back. Rendering the empty page would claim "No user
            // groups yet" with the pager hidden, and Refresh re-asks the same
            // offset, so the remaining groups would be unreachable.
            const rows = res.groups || [];
            const count = res.pagination?.total ?? 0;
            if (rows.length === 0 && offset > 0 && count > 0) {
                setOffset(Math.max(0, offset - PAGE_SIZE));
                return;
            }
            setGroups(rows);
            setTotal(count);
        } catch (e) {
            setError(errorMessage(e, 'Failed to load user groups'));
            setGroups([]);
            setTotal(0);
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, searchTerm, sourceFilter, offset]);

    useEffect(() => {
        runBackground(loadGroups(), 'groups');
    }, [loadGroups]);

    // ---------------------------- handlers -------------------------------

    const handleDelete = async () => {
        if (!pendingDelete) return;
        try {
            await gatewayClient.deleteEndUserGroup(pendingDelete.id);
            setPendingDelete(null);
            if (selectedGroupId === pendingDelete.id) {
                setSelectedGroupId(null);
            }
            await loadGroups();
        } catch (e) {
            setError(errorMessage(e, 'Failed to delete group'));
        }
    };

    const handleCreated = async (newId?: string) => {
        setShowCreate(false);
        setOffset(0);
        await loadGroups();
        // Drop the operator straight into the new group's detail panel
        // so the natural next step ("now add members") is one click away.
        if (newId) {
            setSelectedGroupId(newId);
        }
    };

    // ---------------------------- render ---------------------------------

    return (
        <div className="p-6">
            <InfoBanner title="End-User Groups" className="mb-4">
                Named collections of end-users. Click <em>Manage members</em>{' '}
                on a row to add or remove users, and to bind the group to
                Resource Groups (so every current member inherits access).
                A group an identity provider created is listed with its
                stored source; its roster is not changed here.
            </InfoBanner>

            {/* Toolbar */}
            <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
                <div className="flex items-center space-x-2">
                    <div className="relative">
                        <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
                        <input
                            type="text"
                            value={searchTerm}
                            onChange={(e) => {
                                setOffset(0);
                                setSearchTerm(e.target.value);
                            }}
                            placeholder="Search by name…"
                            className="pl-9 pr-3 py-2 border rounded-md text-sm w-64 bg-surface"
                        />
                    </div>
                    <select
                        value={sourceFilter}
                        onChange={(e) => {
                            setOffset(0);
                            setSourceFilter(e.target.value as any);
                        }}
                        className="px-3 py-2 border rounded-md text-sm bg-surface"
                    >
                        <option value="all">All sources</option>
                        <option value="manual">Manual</option>
                    </select>
                    <button
                        onClick={asyncHandler(loadGroups, 'click')}
                        className="px-3 py-2 border rounded-md text-sm text-secondary hover:bg-surface-raised flex items-center space-x-1"
                    >
                        <RefreshCw className="w-4 h-4" />
                        <span>Refresh</span>
                    </button>
                </div>
                <button
                    onClick={() => setShowCreate(true)}
                    className="px-3 py-2 bg-primary-600 hover:bg-primary-700 text-white rounded-md text-sm flex items-center space-x-2"
                >
                    <Plus className="w-4 h-4" />
                    <span>New User Group</span>
                </button>
            </div>

            {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700">
                    {error}
                </div>
            )}

            {loading ? (
                <div className="flex items-center justify-center py-12">
                    <Loader className="w-6 h-6 animate-spin text-secondary" />
                </div>
            ) : groups.length === 0 ? (
                <div className="text-center py-12 text-secondary">
                    <Users className="w-10 h-10 mx-auto mb-2 text-muted" />
                    <p className="text-sm">
                        {searchTerm || sourceFilter !== 'all'
                            ? 'No user groups match the current filters.'
                            : 'No user groups yet. Create one to get started.'}
                    </p>
                </div>
            ) : (
                <>
                    <div className="bg-surface rounded-lg border overflow-hidden">
                        <table className="w-full text-sm">
                            <thead className={TABLE_THEAD}>
                                <tr>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        Name
                                    </th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        Source
                                    </th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        Members
                                    </th>
                                    <th className={`text-left px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        IdP / External ID
                                    </th>
                                    <th className={`text-right px-4 py-2 ${TABLE_TH_TYPE}`}>
                                        Actions
                                    </th>
                                </tr>
                            </thead>
                            <tbody>
                                {groups.map((g) => (
                                    <tr
                                        key={g.id}
                                        className="border-b last:border-b-0 hover:bg-surface-raised cursor-pointer"
                                        onClick={() => setSelectedGroupId(g.id)}
                                    >
                                        <td className="px-4 py-2">
                                            <div className="font-medium text-default">
                                                {g.display_name || g.name}
                                            </div>
                                            {g.display_name && g.display_name !== g.name && (
                                                <div className="text-xs text-muted">
                                                    {g.name}
                                                </div>
                                            )}
                                            {g.description && (
                                                <div className="text-xs text-secondary mt-1 line-clamp-1">
                                                    {g.description}
                                                </div>
                                            )}
                                        </td>
                                        <td className="px-4 py-2">
                                            <span
                                                className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${SOURCE_BADGE}`}
                                            >
                                                {sourceLabel(g.source)}
                                            </span>
                                        </td>
                                        <td className="px-4 py-2 text-secondary">
                                            {g.member_count}
                                        </td>
                                        <td className="px-4 py-2 text-xs text-muted">
                                            {g.source === 'manual'
                                                ? '—'
                                                : (
                                                    <span>
                                                        {g.idp_server_id || '(no IdP)'}
                                                        {g.source_external_id ? (
                                                            <span className="ml-1 text-secondary">
                                                                / {g.source_external_id}
                                                            </span>
                                                        ) : null}
                                                    </span>
                                                )}
                                        </td>
                                        <td className="px-4 py-2 text-right whitespace-nowrap">
                                            {/* Explicit "Manage" button — the
                                                whole row is clickable, but the
                                                affordance isn't discoverable
                                                without this. Operators landing
                                                here for the first time need to
                                                find "where do I add members?". */}
                                            <button
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    setSelectedGroupId(g.id);
                                                }}
                                                className="px-2 py-1 mr-1 border rounded text-xs text-secondary hover:bg-surface-raised"
                                                aria-label={`Manage members of ${g.name}`}
                                            >
                                                Manage members
                                            </button>
                                            <button
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    setPendingDelete(g);
                                                }}
                                                className="p-1 text-muted hover:text-red-600"
                                                aria-label={`Delete ${g.name}`}
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>

                    {/* Pagination */}
                    {total > PAGE_SIZE && (
                        <div className="flex items-center justify-between mt-4 text-sm text-secondary">
                            <span>
                                Showing {offset + 1}–
                                {Math.min(offset + groups.length, total)} of {total}
                            </span>
                            <div className="space-x-2">
                                <button
                                    disabled={offset === 0}
                                    onClick={() =>
                                        setOffset(Math.max(0, offset - PAGE_SIZE))
                                    }
                                    className="px-3 py-1 border rounded disabled:opacity-50"
                                >
                                    Prev
                                </button>
                                <button
                                    disabled={offset + PAGE_SIZE >= total}
                                    onClick={() => setOffset(offset + PAGE_SIZE)}
                                    className="px-3 py-1 border rounded disabled:opacity-50"
                                >
                                    Next
                                </button>
                            </div>
                        </div>
                    )}
                </>
            )}

            {showCreate && (
                <CreateGroupModal
                    onClose={() => setShowCreate(false)}
                    onCreated={asyncHandler(handleCreated, 'created')}
                />
            )}

            {pendingDelete && (
                <GenericDeleteConfirmationModal
                    isOpen={true}
                    onClose={() => setPendingDelete(null)}
                    onConfirm={asyncHandler(handleDelete, 'confirm')}
                    itemType="group"
                    itemName={pendingDelete.display_name || pendingDelete.name}
                    errorMessage={error}
                    customWarning={
                        pendingDelete.source !== 'manual'
                            ? 'This group was created by an identity provider. It is removed here, with the access it gave its members; the members themselves are not deleted.'
                            : 'This will remove the group and revoke RG access from every member that depended on it. The members themselves are not deleted.'
                    }
                />
            )}

            {selectedGroupId && (
                <GroupDetailPanel
                    groupId={selectedGroupId}
                    onClose={() => setSelectedGroupId(null)}
                    onChanged={asyncHandler(loadGroups, 'changed')}
                />
            )}
        </div>
    );
};

// -----------------------------------------------------------------------------
// CreateGroupModal — manual groups only.
// -----------------------------------------------------------------------------

const CreateGroupModal: React.FC<{
    onClose: () => void;
    onCreated: (newId: string) => void;
}> = ({ onClose, onCreated }) => {
    const { gatewayClient } = useGateway();
    const [name, setName] = useState('');
    const [displayName, setDisplayName] = useState('');
    const [description, setDescription] = useState('');
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!name.trim()) {
            setError('Name is required.');
            return;
        }
        setSaving(true);
        setError(null);
        try {
            const created = await gatewayClient.createEndUserGroup({
                name: name.trim(),
                display_name: displayName.trim() || undefined,
                description: description.trim() || undefined,
            });
            onCreated(created.id);
        } catch (e: any) {
            // Server returns {detail: {code, message}} for known errors;
            // GatewayAPIError carries that parsed body as `details`.
            const detail = e?.details?.detail;
            if (detail?.code === 'name_conflict') {
                setError(`A group named "${name.trim()}" already exists.`);
            } else if (detail?.message) {
                setError(detail.message);
            } else {
                setError(e?.message || 'Failed to create group');
            }
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
            <div className="bg-surface rounded-lg border max-w-md w-full">
                <div className="flex items-center justify-between px-4 py-3 border-b">
                    <h3 className="font-semibold text-default">
                        Create User Group
                    </h3>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>
                <form onSubmit={asyncHandler(handleSubmit, 'submit')}>
                    <div className="p-4 space-y-3">
                        <div>
                            <label className="block text-sm font-medium mb-1">
                                Name <span className="text-red-500">*</span>
                            </label>
                            <input
                                type="text"
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder="engineering-team"
                                className="w-full px-3 py-2 border rounded text-sm bg-surface"
                                autoFocus
                                maxLength={255}
                            />
                            <p className="text-xs text-muted mt-1">
                                Machine-friendly slug. Must be unique within
                                this tenant.
                            </p>
                        </div>
                        <div>
                            <label className="block text-sm font-medium mb-1">
                                Display name
                            </label>
                            <input
                                type="text"
                                value={displayName}
                                onChange={(e) => setDisplayName(e.target.value)}
                                placeholder="Engineering Team"
                                className="w-full px-3 py-2 border rounded text-sm bg-surface"
                                maxLength={255}
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium mb-1">
                                Description
                            </label>
                            <textarea
                                value={description}
                                onChange={(e) => setDescription(e.target.value)}
                                rows={3}
                                className="w-full px-3 py-2 border rounded text-sm bg-surface"
                            />
                        </div>
                        {error && (
                            <div className="p-2 bg-red-50 border border-red-200 rounded text-sm text-red-700">
                                {error}
                            </div>
                        )}
                    </div>
                    <div className="px-4 py-3 border-t flex justify-end space-x-2">
                        <button
                            type="button"
                            onClick={onClose}
                            className="px-3 py-2 border rounded text-sm"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={saving}
                            className="px-3 py-2 bg-primary-600 hover:bg-primary-700 text-white rounded text-sm disabled:opacity-50"
                        >
                            {saving ? 'Creating…' : 'Create'}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
};

// -----------------------------------------------------------------------------
// GroupDetailPanel — slide-over with members + RG bindings.
// -----------------------------------------------------------------------------

const GroupDetailPanel: React.FC<{
    groupId: string;
    onClose: () => void;
    onChanged: () => void;
}> = ({ groupId, onClose, onChanged }) => {
    const { gatewayClient } = useGateway();
    const [detail, setDetail] = useState<GroupDetail | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Add-member UI state.
    const [showAddMember, setShowAddMember] = useState(false);
    const [allUsers, setAllUsers] = useState<EndUserOption[]>([]);
    const [memberSearch, setMemberSearch] = useState('');
    const [pickedUserIds, setPickedUserIds] = useState<Set<number>>(new Set());

    // Add-RG UI state.
    const [showAddRg, setShowAddRg] = useState(false);
    const [allRgs, setAllRgs] = useState<ResourceGroupOption[]>([]);
    const [pickedRgIds, setPickedRgIds] = useState<Set<string>>(new Set());

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const d = await gatewayClient.fetchEndUserGroupDetail(groupId);
            setDetail(d);
            setError(null);
        } catch (e) {
            setError(errorMessage(e, 'Failed to load group'));
        } finally {
            setLoading(false);
        }
    }, [gatewayClient, groupId]);

    useEffect(() => {
        runBackground(load(), 'load');
    }, [load]);

    const isReadOnly = detail?.source !== 'manual';

    const availableUsers = useMemo(() => {
        if (!allUsers || !detail) return [];
        const memberIds = new Set(detail.members.map((m) => m.id));
        const needle = memberSearch.trim().toLowerCase();
        return allUsers
            .filter((u) => !memberIds.has(u.id))
            .filter(
                (u) =>
                    !needle ||
                    u.username.toLowerCase().includes(needle) ||
                    (u.email || '').toLowerCase().includes(needle)
            );
    }, [allUsers, detail, memberSearch]);

    const availableRgs = useMemo(() => {
        if (!allRgs || !detail) return [];
        const bound = new Set(detail.resource_groups.map((r) => r.id));
        return allRgs.filter((r) => !bound.has(r.id));
    }, [allRgs, detail]);

    const openAddMember = async () => {
        setError(null);
        setShowAddMember(true);
        setPickedUserIds(new Set());
        if (allUsers.length === 0) {
            try {
                const res = await gatewayClient.fetchEndUsers({
                    limit: 500,
                });
                setAllUsers(
                    (res.users || []).map((u: any) => ({
                        id: u.id,
                        username: u.username,
                        email: u.email,
                    }))
                );
            } catch (e) {
                setError(errorMessage(e, 'Failed to load end-users'));
            }
        }
    };

    const handleAddMembers = async () => {
        if (pickedUserIds.size === 0) return;
        setError(null);
        try {
            const updated = await gatewayClient.addEndUserGroupMembers(
                groupId,
                Array.from(pickedUserIds)
            );
            setDetail(updated);
            setShowAddMember(false);
            setPickedUserIds(new Set());
            onChanged();
        } catch (e) {
            setError(errorMessage(e, 'Failed to add members'));
        }
    };

    const handleRemoveMember = async (userId: number) => {
        setError(null);
        try {
            const updated = await gatewayClient.removeEndUserGroupMembers(
                groupId,
                [userId]
            );
            setDetail(updated);
            onChanged();
        } catch (e) {
            setError(errorMessage(e, 'Failed to remove member'));
        }
    };

    const openAddRg = async () => {
        setError(null);
        setShowAddRg(true);
        setPickedRgIds(new Set());
        if (allRgs.length === 0) {
            try {
                // Resource groups: simple flat list. Use the existing
                // hierarchy fetcher then flatten — the picker doesn't
                // need parent context.
                const tree = await gatewayClient.fetchGroupHierarchyTree();
                const flat: ResourceGroupOption[] = [];
                const walk = (node: any) => {
                    if (!node) return;
                    flat.push({
                        id: node.id,
                        name: node.name,
                        display_name: node.display_name,
                        group_type: node.group_type,
                    });
                    (node.children || []).forEach(walk);
                };
                (tree.tree || []).forEach(walk);
                setAllRgs(flat);
            } catch (e) {
                setError(errorMessage(e, 'Failed to load Resource Groups'));
            }
        }
    };

    const handleBindRgs = async () => {
        if (pickedRgIds.size === 0) return;
        setError(null);
        try {
            const updated = await gatewayClient.bindEndUserGroupToResourceGroups(
                groupId,
                Array.from(pickedRgIds)
            );
            setDetail(updated);
            setShowAddRg(false);
            setPickedRgIds(new Set());
            onChanged();
        } catch (e) {
            setError(errorMessage(e, 'Failed to bind Resource Groups'));
        }
    };

    const handleUnbindRg = async (rgId: string) => {
        setError(null);
        try {
            const updated = await gatewayClient.unbindEndUserGroupFromResourceGroups(
                groupId,
                [rgId]
            );
            setDetail(updated);
            onChanged();
        } catch (e) {
            setError(errorMessage(e, 'Failed to unbind Resource Group'));
        }
    };

    // The pickers cover the panel, so the panel's own error line would sit
    // behind them: the operator's click would appear to do nothing.
    const pickerError = error && (
        <div className="p-2 bg-red-50 border border-red-200 rounded text-sm text-red-700">
            {error}
        </div>
    );

    return (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-stretch justify-end">
            <div className="bg-surface w-full max-w-2xl h-full overflow-y-auto">
                <div className="flex items-center justify-between px-4 py-3 border-b">
                    <h3 className="font-semibold text-default truncate">
                        {detail?.display_name || detail?.name || 'Loading…'}
                    </h3>
                    <button
                        onClick={onClose}
                        className="text-muted hover:text-secondary"
                        aria-label="Close"
                    >
                        <X className="w-5 h-5" />
                    </button>
                </div>

                {loading ? (
                    <div className="flex items-center justify-center py-12">
                        <Loader className="w-6 h-6 animate-spin text-secondary" />
                    </div>
                ) : !detail ? (
                    <div className="p-4 text-red-700">
                        {error || 'Group not found'}
                    </div>
                ) : (
                    <div className="p-4 space-y-6">
                        {/* Provenance summary */}
                        <div className="bg-surface-raised border rounded p-3 text-sm">
                            <div className="flex items-center space-x-2 mb-1">
                                <span
                                    className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${SOURCE_BADGE}`}
                                >
                                    {sourceLabel(detail.source)}
                                </span>
                                <span className="text-xs text-muted">
                                    {detail.id}
                                </span>
                            </div>
                            {detail.source !== 'manual' && (
                                <p className="text-xs text-muted mt-1">
                                    Created by an identity provider{' '}
                                    <code>{detail.idp_server_id || '?'}</code>
                                    {detail.source_external_id ? (
                                        <>
                                            {' '}
                                            (external id{' '}
                                            <code>{detail.source_external_id}</code>).
                                        </>
                                    ) : (
                                        '.'
                                    )}
                                </p>
                            )}
                            {detail.description && (
                                <p className="text-sm text-secondary mt-2">
                                    {detail.description}
                                </p>
                            )}
                        </div>

                        {error && (
                            <div className="p-3 bg-red-50 border border-red-200 rounded text-sm text-red-700">
                                {error}
                            </div>
                        )}

                        {/* Members */}
                        <section>
                            <div className="flex items-center justify-between mb-2">
                                <h4 className="text-sm font-semibold text-default">
                                    Members ({detail.members.length})
                                </h4>
                                <button
                                    onClick={asyncHandler(openAddMember, 'click')}
                                    disabled={isReadOnly}
                                    title={
                                        isReadOnly
                                            ? 'The roster of a group an identity provider created is not changed here.'
                                            : undefined
                                    }
                                    className="px-2 py-1 border rounded text-sm flex items-center space-x-1 hover:bg-surface-raised disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                    <UserPlus className="w-4 h-4" />
                                    <span>Add</span>
                                </button>
                            </div>
                            {detail.members.length === 0 ? (
                                <p className="text-sm text-muted italic">
                                    No members yet.
                                </p>
                            ) : (
                                <ul className="border rounded divide-y">
                                    {detail.members.map((m) => (
                                        <li
                                            key={m.id}
                                            className="flex items-center justify-between px-3 py-2 text-sm"
                                        >
                                            <div className="min-w-0">
                                                <div className="font-medium text-default truncate">
                                                    {m.display_name || m.username}
                                                </div>
                                                {m.email && (
                                                    <div className="text-xs text-muted truncate">
                                                        {m.email}
                                                    </div>
                                                )}
                                            </div>
                                            <button
                                                onClick={asyncHandler(() =>
                                                    handleRemoveMember(m.id)
                                                , 'click')}
                                                disabled={isReadOnly}
                                                title={
                                                    isReadOnly
                                                        ? 'IdP-sourced membership — remove via the IdP.'
                                                        : undefined
                                                }
                                                className="p-1 text-muted hover:text-red-600 disabled:opacity-30 disabled:cursor-not-allowed"
                                                aria-label={`Remove ${m.username}`}
                                            >
                                                <UserMinus className="w-4 h-4" />
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </section>

                        {/* Bound Resource Groups */}
                        <section>
                            <div className="flex items-center justify-between mb-2">
                                <h4 className="text-sm font-semibold text-default">
                                    Resource Group bindings (
                                    {detail.resource_groups.length})
                                </h4>
                                <button
                                    onClick={asyncHandler(openAddRg, 'click')}
                                    className="px-2 py-1 border rounded text-sm flex items-center space-x-1 hover:bg-surface-raised"
                                >
                                    <Plus className="w-4 h-4" />
                                    <span>Bind</span>
                                </button>
                            </div>
                            {detail.resource_groups.length === 0 ? (
                                <p className="text-sm text-muted italic">
                                    This group is not bound to any Resource Group
                                    yet — members get no resource access via this
                                    group.
                                </p>
                            ) : (
                                <ul className="border rounded divide-y">
                                    {detail.resource_groups.map((rg) => (
                                        <li
                                            key={rg.id}
                                            className="flex items-center justify-between px-3 py-2 text-sm"
                                        >
                                            <div>
                                                <div className="font-medium text-default">
                                                    {rg.display_name || rg.name}
                                                </div>
                                                <div className="text-xs text-muted">
                                                    {rg.id}
                                                </div>
                                            </div>
                                            <button
                                                onClick={asyncHandler(() => handleUnbindRg(rg.id), 'click')}
                                                className="p-1 text-muted hover:text-red-600"
                                                aria-label={`Unbind ${rg.name}`}
                                            >
                                                <X className="w-4 h-4" />
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </section>
                    </div>
                )}

                {showAddMember && (
                    <div className="fixed inset-0 bg-black/40 z-[60] flex items-center justify-center p-4">
                        <div className="bg-surface rounded-lg border w-full max-w-lg">
                            <div className="flex items-center justify-between px-4 py-3 border-b">
                                <h4 className="font-semibold">Add members</h4>
                                <button
                                    onClick={() => setShowAddMember(false)}
                                    className="text-muted hover:text-secondary"
                                >
                                    <X className="w-5 h-5" />
                                </button>
                            </div>
                            <div className="p-4 space-y-3">
                                {pickerError}
                                <input
                                    type="text"
                                    value={memberSearch}
                                    onChange={(e) =>
                                        setMemberSearch(e.target.value)
                                    }
                                    placeholder="Search users…"
                                    className="w-full px-3 py-2 border rounded text-sm bg-surface"
                                    autoFocus
                                />
                                <div className="max-h-72 overflow-y-auto border rounded">
                                    {availableUsers.length === 0 ? (
                                        <p className="p-3 text-sm text-muted italic">
                                            No users available to add.
                                        </p>
                                    ) : (
                                        availableUsers.map((u) => (
                                            <label
                                                key={u.id}
                                                className="flex items-center px-3 py-2 hover:bg-surface-raised cursor-pointer text-sm border-b last:border-b-0"
                                            >
                                                <input
                                                    type="checkbox"
                                                    checked={pickedUserIds.has(
                                                        u.id
                                                    )}
                                                    onChange={(e) => {
                                                        const next = new Set(
                                                            pickedUserIds
                                                        );
                                                        if (e.target.checked) {
                                                            next.add(u.id);
                                                        } else {
                                                            next.delete(u.id);
                                                        }
                                                        setPickedUserIds(next);
                                                    }}
                                                    className="mr-2"
                                                />
                                                <div className="min-w-0">
                                                    <div className="font-medium truncate">
                                                        {u.username}
                                                    </div>
                                                    {u.email && (
                                                        <div className="text-xs text-muted truncate">
                                                            {u.email}
                                                        </div>
                                                    )}
                                                </div>
                                            </label>
                                        ))
                                    )}
                                </div>
                            </div>
                            <div className="px-4 py-3 border-t flex justify-end space-x-2">
                                <button
                                    onClick={() => setShowAddMember(false)}
                                    className="px-3 py-2 border rounded text-sm"
                                >
                                    Cancel
                                </button>
                                <button
                                    onClick={asyncHandler(handleAddMembers, 'click')}
                                    disabled={pickedUserIds.size === 0}
                                    className="px-3 py-2 bg-primary-600 hover:bg-primary-700 text-white rounded text-sm disabled:opacity-50"
                                >
                                    Add {pickedUserIds.size || ''}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {showAddRg && (
                    <div className="fixed inset-0 bg-black/40 z-[60] flex items-center justify-center p-4">
                        <div className="bg-surface rounded-lg border w-full max-w-lg">
                            <div className="flex items-center justify-between px-4 py-3 border-b">
                                <h4 className="font-semibold">
                                    Bind to Resource Groups
                                </h4>
                                <button
                                    onClick={() => setShowAddRg(false)}
                                    className="text-muted hover:text-secondary"
                                >
                                    <X className="w-5 h-5" />
                                </button>
                            </div>
                            <div className="p-4 space-y-3">
                                {pickerError}
                                <div className="max-h-72 overflow-y-auto border rounded">
                                    {availableRgs.length === 0 ? (
                                        <p className="p-3 text-sm text-muted italic">
                                            No Resource Groups available.
                                        </p>
                                    ) : (
                                        availableRgs.map((rg) => (
                                            <label
                                                key={rg.id}
                                                className="flex items-center px-3 py-2 hover:bg-surface-raised cursor-pointer text-sm border-b last:border-b-0"
                                            >
                                                <input
                                                    type="checkbox"
                                                    checked={pickedRgIds.has(
                                                        rg.id
                                                    )}
                                                    onChange={(e) => {
                                                        const next = new Set(
                                                            pickedRgIds
                                                        );
                                                        if (e.target.checked) {
                                                            next.add(rg.id);
                                                        } else {
                                                            next.delete(rg.id);
                                                        }
                                                        setPickedRgIds(next);
                                                    }}
                                                    className="mr-2"
                                                />
                                                <div className="min-w-0">
                                                    <div className="font-medium truncate">
                                                        {formatGroupLabel(rg)}
                                                    </div>
                                                    <div className="text-xs text-muted truncate">
                                                        {rg.id}
                                                    </div>
                                                </div>
                                            </label>
                                        ))
                                    )}
                                </div>
                            </div>
                            <div className="px-4 py-3 border-t flex justify-end space-x-2">
                                <button
                                    onClick={() => setShowAddRg(false)}
                                    className="px-3 py-2 border rounded text-sm"
                                >
                                    Cancel
                                </button>
                                <button
                                    onClick={asyncHandler(handleBindRgs, 'click')}
                                    disabled={pickedRgIds.size === 0}
                                    className="px-3 py-2 bg-primary-600 hover:bg-primary-700 text-white rounded text-sm disabled:opacity-50"
                                >
                                    Bind {pickedRgIds.size || ''}
                                </button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};

export default EndUserGroupsTab;

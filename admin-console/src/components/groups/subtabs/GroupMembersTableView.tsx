/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React from 'react';
import { TABLE_TH_TYPE } from '../../ui/tableStyles';
import {
    UserMinus,
    Mail,
    Calendar,
    CheckCircle,
    ArrowUpRight,
    Star,
    BarChart3,
} from 'lucide-react';
import { GroupMember } from '../../../lib/groupTypes';
import { UserAvatar } from '../../ui/UserAvatar';

interface GroupMembersTableViewProps {
    members: GroupMember[];
    selectedMembers: Set<string>;
    onSelectMember: (userId: string) => void;
    onSelectAll: () => void;
    onRemove: (userId: string, username: string) => void;
    removingId: string | null;
    /** Invoked to set this group as the user's primary. Receives the user id. */
    onSetPrimary?: (userId: string, username: string) => void;
    /** Id of the user whose primary-group change is currently in flight. */
    settingPrimaryId?: string | null;
    /**
     * Open the per-member usage modal. Receives the row's
     * `GroupMember` so the parent gets the full identity (id +
     * username + display name) without re-resolving from the
     * member list. Renders for both direct and inherited rows
     * — usage rollup is based on actual log activity, which is
     * meaningful regardless of where the membership comes from.
     */
    onShowUsage?: (member: GroupMember) => void;
}

const GroupMembersTableView: React.FC<GroupMembersTableViewProps> = ({
                                                                         members,
                                                                         selectedMembers,
                                                                         onSelectMember,
                                                                         onSelectAll,
                                                                         onRemove,
                                                                         removingId,
                                                                         onSetPrimary,
                                                                         settingPrimaryId,
                                                                         onShowUsage,
                                                                     }) => {
    return (
        <div className="bg-surface border border-default rounded-lg overflow-hidden">
            <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-default">
                    <thead className="bg-surface-alt">
                    <tr>
                        <th className="px-4 py-3 text-left">
                            <input
                                type="checkbox"
                                checked={members.length > 0 && selectedMembers.size === members.filter(m => !m.is_inherited).length}
                                onChange={onSelectAll}
                                className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500"
                                title="Select all direct members"
                            />
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            User
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Email
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Membership
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-left`}>
                            Joined
                        </th>
                        <th scope="col" className={`${TABLE_TH_TYPE} px-4 py-3 text-right`}>
                            Actions
                        </th>
                    </tr>
                    </thead>
                    <tbody className="bg-surface divide-y divide-default">
                    {members.map((member) => {
                        const isInherited = member.is_inherited || false;
                        const isDirect = !isInherited;
                        const isRemoving = removingId === member.user_id;

                        return (
                            <tr
                                key={member.user_id}
                                className={`transition-colors ${
                                    isInherited
                                        ? 'bg-surface-alt'
                                        : selectedMembers.has(member.user_id)
                                            ? 'bg-primary-pale'
                                            : 'hover:bg-surface-alt'
                                }`}
                            >
                                {/* Checkbox */}
                                <td className="px-4 py-4">
                                    {isDirect && (
                                        <input
                                            type="checkbox"
                                            checked={selectedMembers.has(member.user_id)}
                                            onChange={() => onSelectMember(member.user_id)}
                                            className="w-4 h-4 text-primary-600 border-strong rounded focus:ring-primary-500"
                                        />
                                    )}
                                </td>

                                {/* User Info */}
                                <td className="px-4 py-4">
                                    <div className="flex items-center space-x-3">
                                        <UserAvatar avatarUrl={member.avatar_url} name={member.display_name || member.username} size="md" />
                                        <div>
                                            <div className="flex items-center gap-1.5">
                                                <span className="text-sm font-medium text-default">
                                                    {member.display_name || member.username}
                                                </span>
                                                {member.is_primary && (
                                                    <span
                                                        className="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium bg-surface-alt text-amber-700 rounded"
                                                        title="This group is the user's primary (active runtime) group"
                                                    >
                                                        <Star className="w-2.5 h-2.5 fill-current" />
                                                        Primary
                                                    </span>
                                                )}
                                            </div>
                                            <div className="text-sm text-muted">
                                                @{member.username}
                                            </div>
                                        </div>
                                    </div>
                                </td>

                                {/* Email */}
                                <td className="px-4 py-4">
                                    <div className="flex items-center space-x-2 text-sm text-secondary">
                                        <Mail className="w-4 h-4 text-muted" />
                                        <span>{member.email || '-'}</span>
                                    </div>
                                </td>

                                {/* Membership Type */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    {isInherited ? (
                                        <div className="flex flex-col">
                                            <div className="inline-flex items-center space-x-1 px-2 py-1 bg-surface-alt rounded-full w-fit">
                                                <ArrowUpRight className="w-3 h-3 text-muted" />
                                                <span className="text-xs font-medium text-secondary">Inherited</span>
                                            </div>
                                            {member.source_group_name && (
                                                <span className="text-xs text-muted mt-1">
                                                        from {member.source_group_name}
                                                    </span>
                                            )}
                                        </div>
                                    ) : (
                                        <div className="inline-flex items-center space-x-1 px-2 py-1 bg-primary-pale rounded-full">
                                            <CheckCircle className="w-3 h-3 text-primary" />
                                            <span className="text-xs font-medium text-primary">Direct</span>
                                        </div>
                                    )}
                                </td>

                                {/* Joined Date */}
                                <td className="px-4 py-4 whitespace-nowrap">
                                    <div className="flex items-center space-x-2 text-sm text-secondary">
                                        <Calendar className="w-4 h-4 text-muted" />
                                        <span>{new Date(member.joined_at).toLocaleDateString()}</span>
                                    </div>
                                </td>

                                {/* Actions */}
                                <td className="px-4 py-4 whitespace-nowrap text-right">
                                    <div className="inline-flex items-center gap-1">
                                        {/* Usage drill-down — visible on every
                                            row (direct + inherited). The rollup
                                            uses log activity which doesn't
                                            care which group surfaced the
                                            membership. */}
                                        {onShowUsage && (
                                            <button
                                                onClick={() => onShowUsage(member)}
                                                className="p-1.5 text-muted hover:text-primary-600 hover:bg-primary-50 rounded transition-colors"
                                                title="View this member's usage in this group"
                                            >
                                                <BarChart3 className="w-4 h-4" />
                                            </button>
                                        )}
                                        {isDirect && onSetPrimary && !member.is_primary && (
                                            <button
                                                onClick={() =>
                                                    onSetPrimary(member.user_id, member.username)
                                                }
                                                disabled={settingPrimaryId === member.user_id}
                                                className="p-1.5 text-muted hover:text-amber-600 hover:bg-surface-alt rounded transition-colors disabled:opacity-50"
                                                title="Make this the user's primary (active) group"
                                            >
                                                {settingPrimaryId === member.user_id ? (
                                                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-amber-500" />
                                                ) : (
                                                    <Star className="w-4 h-4" />
                                                )}
                                            </button>
                                        )}
                                        {isDirect && (
                                            <button
                                                onClick={() => onRemove(member.user_id, member.username)}
                                                disabled={isRemoving}
                                                className="p-1.5 text-red-600 hover:text-red-700 hover:bg-surface-alt rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                                title="Remove member"
                                            >
                                                {isRemoving ? (
                                                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-red-600"></div>
                                                ) : (
                                                    <UserMinus className="w-4 h-4" />
                                                )}
                                            </button>
                                        )}
                                    </div>
                                </td>
                            </tr>
                        );
                    })}
                    </tbody>
                </table>
            </div>

            {/* Footer with count */}
            <div className="px-4 py-3 bg-surface-alt border-t border-default">
                <div className="flex items-center justify-between text-sm">
                    <div className="flex items-center space-x-6">
                        <div>
                            <span className="text-secondary">Showing:</span>
                            <span className="font-semibold text-default ml-2">
                                {members.length} member{members.length !== 1 ? 's' : ''}
                            </span>
                        </div>
                        <div>
                            <span className="text-secondary">Direct:</span>
                            <span className="font-semibold text-primary ml-2">
                                {members.filter(m => !m.is_inherited).length}
                            </span>
                        </div>
                        <div>
                            <span className="text-secondary">Inherited:</span>
                            <span className="font-semibold text-secondary ml-2">
                                {members.filter(m => m.is_inherited).length}
                            </span>
                        </div>
                    </div>
                    {selectedMembers.size > 0 && (
                        <div>
                            <span className="text-secondary">Selected:</span>
                            <span className="font-semibold text-primary-600 ml-2">
                                {selectedMembers.size}
                            </span>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};

export default GroupMembersTableView;
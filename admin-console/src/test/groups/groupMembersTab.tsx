/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the tests of the Members tab of a Resource Group share (the two halves
 * of `GroupMembersTab.test` and of `GroupMembersTab.sendMessage.test`: the
 * tab's own cases are Community tests, the cases that need the real Send
 * Message action are under `enterprise/src/host/`): the gateway doubles, the
 * stubs of the tab's children, the group and its two members.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes the modules from here. This
 * module does not import the tab, so that a mock factory can await it.
 */

import { expect, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';

import type { GroupMember, ResourceGroup } from '../../lib/groupTypes';

export const gatewayClient = {
    fetchResourceGroupBoundUserGroups: vi.fn(),
    fetchEndUserGroups: vi.fn(),
    bindResourceGroupToUserGroups: vi.fn(),
    unbindResourceGroupFromUserGroups: vi.fn(),
    setUserPrimaryGroup: vi.fn(),
};
export const gateway = {
    fetchGroupMembers: vi.fn(),
    removeGroupMember: vi.fn(),
    addGroupMembers: vi.fn(),
    fetchEndUsers: vi.fn(),
    gatewayClient,
};
export const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };
export const confirm = vi.fn();

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => gateway });
/** `components/common/Toast`. */
export const toastModule = () => ({ useToast: () => toast });
/** `components/common/ConfirmationDialog`. */
export const confirmModule = () => ({ useConfirm: () => confirm });

/** `GroupMembersTableView`: a row per member, with a button for each thing the table can ask of the tab. */
export const tableViewModule = () => ({
    default: (p: {
        members: GroupMember[];
        onSelectMember: (id: string) => void;
        onSelectAll: () => void;
        onRemove: (id: string, name: string) => void;
        onSetPrimary: (id: string, name: string) => void;
        onShowUsage: (m: GroupMember) => void;
    }) => (
        <div data-testid="table">
            <button onClick={p.onSelectAll}>select-all</button>
            {p.members.map((m) => (
                <div key={m.user_id} data-testid={`row-${m.user_id}`}>
                    <span>{m.username}</span>
                    <button onClick={() => p.onSelectMember(m.user_id)}>select</button>
                    <button onClick={() => p.onRemove(m.user_id, m.username)}>remove</button>
                    <button onClick={() => p.onSetPrimary(m.user_id, m.username)}>primary</button>
                    <button onClick={() => p.onShowUsage(m)}>usage</button>
                </div>
            ))}
        </div>
    ),
});

/** `GroupMembersTableView`, for a test of the action bar: the members' names and "select all". */
export const namesTableViewModule = () => ({
    default: (p: { members: GroupMember[]; onSelectAll: () => void }) => (
        <div data-testid="table">
            <button onClick={p.onSelectAll}>select-all</button>
            {p.members.map((m) => (
                <span key={m.user_id}>{m.username}</span>
            ))}
        </div>
    ),
});

/** `GroupMemberUsageModal`. */
export const usageModalModule = () => ({
    default: (p: { userId: string; groupId: string; onClose: () => void }) => (
        <div data-testid="usage-modal">
            {p.groupId}/{p.userId}
            <button onClick={p.onClose}>close-usage</button>
        </div>
    ),
});

export const GROUP = { id: 'rg-1', name: 'engineering', display_name: 'Engineering' } as ResourceGroup;

export const member = (id: string, extra: Partial<GroupMember> = {}) =>
    ({ user_id: id, username: id, email: `${id}@acme.test`, ...extra }) as GroupMember;

/** Resolves once the tab has asked for the group's members and shows them. */
export async function membersLoaded(): Promise<void> {
    await waitFor(() => expect(gateway.fetchGroupMembers).toHaveBeenCalledWith('rg-1'));
    await waitFor(() => expect(screen.queryByText('Loading members...')).toBeNull());
}

/** How every case starts: alice and bob in the group, three end users to add from. For a `beforeEach`. */
export function resetGroupMembersTab(): void {
    vi.clearAllMocks();
    gateway.removeGroupMember.mockReset();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    gateway.fetchGroupMembers.mockResolvedValue({ members: [member('alice'), member('bob')] });
    gatewayClient.fetchResourceGroupBoundUserGroups.mockResolvedValue({ end_user_groups: [] });
    gateway.fetchEndUsers.mockResolvedValue({
        users: [
            { id: 'alice', username: 'alice', primary_group_id: 'x' },
            { id: 'carol', username: 'carol', email: 'carol@acme.test', primary_group_id: 'rg-other' },
            { id: 'dave', username: 'dave', primary_group_id: null },
        ],
    });
    gateway.addGroupMembers.mockResolvedValue({ added: [{ id: 'carol' }], skipped: [] });
}

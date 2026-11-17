/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Group Members — who can use this Resource Group.
 *
 * Pinned: the membership mutations send the right ids, the primary-group
 * checkbox is forced on (and sent as true) when every selected user is joining
 * their first group, modal state does not leak between openings, bulk removal
 * reports partial failure honestly, and already-bound user groups are not
 * offered again. The table has its own suite, so it is stubbed to its callbacks.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const shared = vi.hoisted(() => () => import('../../../../test/groups/groupMembersTab'));
vi.mock('../../../../hooks/useGateway', async () => (await shared()).useGatewayModule());
vi.mock('../../../common/Toast', async () => (await shared()).toastModule());
vi.mock('../../../common/ConfirmationDialog', async () => (await shared()).confirmModule());
vi.mock('../GroupMembersTableView', async () => (await shared()).tableViewModule());
vi.mock('../GroupMemberUsageModal', async () => (await shared()).usageModalModule());

import GroupMembersTab from '../GroupMembersTab';
import { WithBuiltinSlots } from '../../../../test/builtinSlots';
import {
    confirm,
    gateway,
    gatewayClient,
    GROUP,
    member,
    membersLoaded,
    resetGroupMembersTab,
    toast,
} from '../../../../test/groups/groupMembersTab';
import { SlotProvider } from '../../../../app/SlotProvider';

const onUpdate = vi.fn();

async function renderTab() {
    // Send Message is contributed through a slot: rendered as the application does.
    render(<GroupMembersTab group={GROUP} onUpdate={onUpdate} />, { wrapper: WithBuiltinSlots });
    await membersLoaded();
}

async function openAddModal() {
    fireEvent.click(screen.getByRole('button', { name: /Add Members/ }));
    await waitFor(() => expect(gateway.fetchEndUsers).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText('Loading users...')).toBeNull());
}

const primaryCheckbox = () =>
    within(screen.getByText('Make this the primary group for the selected users').closest('label') as HTMLElement).getByRole<HTMLInputElement>('checkbox');

describe('GroupMembersTab', () => {
    beforeEach(resetGroupMembersTab);

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('shows members and search narrows them; a load failure is surfaced', async () => {
        await renderTab();
        expect(screen.getByTestId('row-alice')).toBeInTheDocument();
        expect(screen.getByText('members')).toBeInTheDocument();

        fireEvent.change(screen.getByPlaceholderText('Search by name, email, or username...'), {
            target: { value: 'ALI' },
        });
        expect(screen.getByTestId('row-alice')).toBeInTheDocument();
        expect(screen.queryByTestId('row-bob')).toBeNull();

        fireEvent.change(screen.getByPlaceholderText('Search by name, email, or username...'), {
            target: { value: 'nobody' },
        });
        expect(screen.getByText('No members found matching your search')).toBeInTheDocument();
    });

    it('does not print an activity figure it never measured', async () => {
        // The line read `floor(members * 0.75) active in last 30 days`: invented,
        // and on a governance console an operator would take it as measured.
        await renderTab();
        expect(screen.getByText('members')).toBeInTheDocument();
        expect(screen.queryByText(/active in last 30 days/)).toBeNull();
    });

    it.todo('BUG: the Export button (GroupMembersTab.tsx) has no onClick and does nothing');

    it('offers only users who are not already members, and adds the selection', async () => {
        await renderTab();
        await openAddModal();

        expect(gateway.fetchEndUsers).toHaveBeenCalledWith({ search: '', active_only: true, limit: 50 });
        // alice is a member already and must not be offered.
        expect(screen.getByText('carol')).toBeInTheDocument();
        expect(screen.getAllByText('alice')).toHaveLength(1);

        fireEvent.click(screen.getByText('carol'));
        expect(primaryCheckbox().checked).toBe(false);
        expect(primaryCheckbox().disabled).toBe(false);

        fireEvent.click(screen.getByRole('button', { name: 'Add (1)' }));
        await waitFor(() =>
            expect(gateway.addGroupMembers).toHaveBeenCalledWith('rg-1', ['carol'], 'member', false),
        );
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith('Members Added', '1 member added to Engineering.'),
        );
        expect(screen.queryByText('Search Users')).toBeNull();
        // Reloaded so the new member appears.
        expect(gateway.fetchGroupMembers).toHaveBeenCalledTimes(2);
    });

    it('forces make-primary on when every selected user is joining a first group', async () => {
        await renderTab();
        await openAddModal();

        fireEvent.click(screen.getByText('dave'));
        expect(primaryCheckbox().checked).toBe(true);
        expect(primaryCheckbox().disabled).toBe(true);
        expect(screen.getByText(/the checkbox is locked on/)).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Add (1)' }));
        await waitFor(() =>
            expect(gateway.addGroupMembers).toHaveBeenCalledWith('rg-1', ['dave'], 'member', true),
        );
    });

    it('warns about the no-primary subset of a mixed selection and leaves the box free', async () => {
        await renderTab();
        await openAddModal();

        fireEvent.click(screen.getByText('dave'));
        fireEvent.click(screen.getByText('carol'));
        expect(screen.getByText(/1 of the selected\s+users have no primary group yet/)).toBeInTheDocument();
        expect(primaryCheckbox().disabled).toBe(false);
        expect(primaryCheckbox().checked).toBe(false);

        fireEvent.click(primaryCheckbox());
        fireEvent.click(screen.getByRole('button', { name: 'Add (2)' }));
        await waitFor(() =>
            expect(gateway.addGroupMembers).toHaveBeenCalledWith('rg-1', ['dave', 'carol'], 'member', true),
        );
    });

    it('does not carry make-primary into the next opening of the modal', async () => {
        await renderTab();
        await openAddModal();
        fireEvent.click(screen.getByText('carol'));
        fireEvent.click(primaryCheckbox());
        expect(primaryCheckbox().checked).toBe(true);

        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(screen.queryByText('Search Users')).toBeNull();

        await openAddModal();
        expect(primaryCheckbox().checked).toBe(false);
        expect(screen.queryByText(/users? selected/)).toBeNull();
    });

    it('reports skipped users rather than claiming success', async () => {
        gateway.addGroupMembers.mockResolvedValueOnce({
            added: [{ id: 'carol' }],
            skipped: [{ username: 'dave', reason: 'disabled' }],
        });
        await renderTab();
        await openAddModal();
        fireEvent.click(screen.getByText('carol'));
        fireEvent.click(screen.getByRole('button', { name: 'Add (1)' }));
        await waitFor(() =>
            expect(toast.warning).toHaveBeenCalledWith('Partially Added', '1 added, 1 skipped:\ndave: disabled'),
        );
        expect(toast.success).not.toHaveBeenCalled();

        gateway.addGroupMembers.mockResolvedValueOnce({
            added: [],
            skipped: [{ username: 'carol', reason: 'quota' }],
        });
        await openAddModal();
        fireEvent.click(screen.getByText('carol'));
        fireEvent.click(screen.getByRole('button', { name: 'Add (1)' }));
        await waitFor(() =>
            expect(toast.warning).toHaveBeenCalledWith('No Members Added', 'All 1 user was skipped:\ncarol: quota'),
        );
    });

    it('a failed add keeps the modal open and reports it', async () => {
        gateway.addGroupMembers.mockRejectedValue(new Error('nope'));
        await renderTab();
        await openAddModal();
        fireEvent.click(screen.getByText('carol'));
        fireEvent.click(screen.getByRole('button', { name: 'Add (1)' }));
        await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to Add Members', 'nope'));
        expect(screen.getByText('Search Users')).toBeInTheDocument();
    });

    it('searching in the add modal re-queries and shows the empty message', async () => {
        await renderTab();
        await openAddModal();
        gateway.fetchEndUsers.mockResolvedValue({ users: [] });
        fireEvent.change(screen.getByPlaceholderText('Search by name or email...'), {
            target: { value: 'zed' },
        });
        await waitFor(() =>
            expect(gateway.fetchEndUsers).toHaveBeenLastCalledWith({ search: 'zed', active_only: true, limit: 50 }),
        );
        expect(await screen.findByText('No users found')).toBeInTheDocument();
    });

    it('removes one member after typed confirmation, and shows a server refusal inline', async () => {
        gateway.removeGroupMember.mockRejectedValueOnce({ detail: 'owner cannot be removed' });
        await renderTab();
        fireEvent.click(within(screen.getByTestId('row-bob')).getByText('remove'));

        const confirmInput = screen.getByPlaceholderText('Enter "bob"');
        fireEvent.change(confirmInput, { target: { value: 'bob' } });
        fireEvent.keyDown(confirmInput, { key: 'Enter' });

        await waitFor(() => expect(gateway.removeGroupMember).toHaveBeenCalledWith('rg-1', 'bob'));
        expect(await screen.findByText('owner cannot be removed')).toBeInTheDocument();
        expect(onUpdate).not.toHaveBeenCalled();

        gateway.removeGroupMember.mockResolvedValueOnce({});
        fireEvent.keyDown(confirmInput, { key: 'Enter' });
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith('Member Removed', 'bob has been removed from Engineering.'),
        );
        expect(onUpdate).toHaveBeenCalledTimes(1);
    });

    it('select-all skips inherited members, so bulk remove never targets them', async () => {
        gateway.fetchGroupMembers.mockResolvedValue({
            members: [member('alice'), member('bob'), member('heir', { is_inherited: true })],
        });
        gateway.removeGroupMember.mockResolvedValue({});
        confirm.mockResolvedValue(true);
        await renderTab();

        fireEvent.click(screen.getByText('select-all'));
        fireEvent.click(screen.getByRole('button', { name: 'Remove (2)' }));

        await waitFor(() => expect(gateway.removeGroupMember).toHaveBeenCalledTimes(2));
        expect(gateway.removeGroupMember.mock.calls).toEqual([['rg-1', 'alice'], ['rg-1', 'bob']]);
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith('Members removed', '2 member(s) removed from Engineering.'),
        );
        expect(onUpdate).toHaveBeenCalled();
        expect(screen.queryByRole('button', { name: /^Remove \(/ })).toBeNull();
    });

    it('select-all a second time clears; declining the confirm removes nothing', async () => {
        confirm.mockResolvedValue(false);
        await renderTab();
        fireEvent.click(screen.getByText('select-all'));
        fireEvent.click(screen.getByRole('button', { name: 'Remove (2)' }));
        await waitFor(() => expect(confirm).toHaveBeenCalled());
        expect(gateway.removeGroupMember).not.toHaveBeenCalled();

        fireEvent.click(screen.getByText('select-all'));
        expect(screen.queryByRole('button', { name: /^Remove \(/ })).toBeNull();

        // Row selection toggles the same set.
        fireEvent.click(within(screen.getByTestId('row-bob')).getByText('select'));
        expect(screen.getByRole('button', { name: 'Remove (1)' })).toBeInTheDocument();
        fireEvent.click(within(screen.getByTestId('row-bob')).getByText('select'));
        expect(screen.queryByRole('button', { name: /^Remove \(/ })).toBeNull();
    });

    it('reports partial and total bulk-removal failure', async () => {
        confirm.mockResolvedValue(true);
        await renderTab();
        gateway.removeGroupMember.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('stale'));
        fireEvent.click(screen.getByText('select-all'));
        fireEvent.click(screen.getByRole('button', { name: 'Remove (2)' }));
        await waitFor(() =>
            expect(toast.error).toHaveBeenCalledWith(
                'Partial bulk remove',
                'Removed 1 of 2. 1 failed (e.g. bob: stale).',
            ),
        );

        gateway.removeGroupMember.mockRejectedValue(new Error('forbidden'));
        fireEvent.click(screen.getByText('select-all'));
        fireEvent.click(await screen.findByRole('button', { name: 'Remove (2)' }));
        await waitFor(() =>
            expect(toast.error).toHaveBeenCalledWith(
                'Bulk remove failed',
                'Could not remove any of the 2 selected member(s). First error: forbidden',
            ),
        );
    });

    it('sets the primary group and reports a failure', async () => {
        gatewayClient.setUserPrimaryGroup.mockResolvedValueOnce({});
        await renderTab();
        fireEvent.click(within(screen.getByTestId('row-alice')).getByText('primary'));
        await waitFor(() => expect(gatewayClient.setUserPrimaryGroup).toHaveBeenCalledWith('alice', 'rg-1'));
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith(
                'Primary group updated',
                "alice's primary group is now Engineering.",
            ),
        );

        gatewayClient.setUserPrimaryGroup.mockRejectedValueOnce(new Error('not a member'));
        fireEvent.click(within(screen.getByTestId('row-alice')).getByText('primary'));
        await waitFor(() =>
            expect(toast.error).toHaveBeenCalledWith('Failed to set primary group', 'not a member'),
        );
    });

    describe('bound user groups', () => {
        const bound = { id: 'ug-1', name: 'sso', display_name: 'SSO Users', source: 'saml', member_count: 1 };
        const free = { id: 'ug-2', name: 'contractors', source: 'manual', member_count: 4 };

        it('never offers a group that is already bound, and binds the selection', async () => {
            gatewayClient.fetchResourceGroupBoundUserGroups.mockResolvedValue({ end_user_groups: [bound] });
            gatewayClient.fetchEndUserGroups.mockResolvedValue({ groups: [bound, free] });
            gatewayClient.bindResourceGroupToUserGroups.mockResolvedValue({});
            await renderTab();
            expect(await screen.findByText('SSO Users')).toBeInTheDocument();
            expect(screen.getByText('saml')).toBeInTheDocument();
            expect(screen.queryByText('SAML')).toBeNull();
            expect(screen.getByText('Bound User Groups (1)')).toBeInTheDocument();

            fireEvent.click(screen.getByRole('button', { name: 'Bind User Group' }));
            await screen.findByText('contractors');
            expect(gatewayClient.fetchEndUserGroups).toHaveBeenCalledWith({ limit: 500 });
            // Only the unbound one is on offer (the bound name appears once, in the list).
            expect(screen.getAllByText('SSO Users')).toHaveLength(1);

            const bindBtn = screen.getByRole('button', { name: /^Bind\s*$/ });
            expect(bindBtn).toBeDisabled();

            fireEvent.change(screen.getByPlaceholderText('Search user groups…'), { target: { value: 'zz' } });
            expect(screen.getByText('No user groups match the search.')).toBeInTheDocument();
            fireEvent.change(screen.getByPlaceholderText('Search user groups…'), { target: { value: 'contr' } });

            fireEvent.click(within(screen.getByText('contractors').closest('label') as HTMLElement).getByRole('checkbox'));
            fireEvent.click(screen.getByRole('button', { name: 'Bind 1' }));

            await waitFor(() =>
                expect(gatewayClient.bindResourceGroupToUserGroups).toHaveBeenCalledWith('rg-1', ['ug-2']),
            );
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith(
                    'User groups bound',
                    '1 user group(s) attached to Engineering.',
                ),
            );
            expect(gatewayClient.fetchResourceGroupBoundUserGroups).toHaveBeenCalledTimes(2);
            expect(screen.queryByPlaceholderText('Search user groups…')).toBeNull();
        });

        it('reports a bind failure and keeps the picker open', async () => {
            gatewayClient.fetchEndUserGroups.mockResolvedValue({ groups: [free] });
            gatewayClient.bindResourceGroupToUserGroups.mockRejectedValue(new Error('conflict'));
            await renderTab();
            fireEvent.click(screen.getByRole('button', { name: 'Bind User Group' }));
            fireEvent.click(await screen.findByRole('checkbox'));
            fireEvent.click(screen.getByRole('button', { name: 'Bind 1' }));
            await waitFor(() =>
                expect(toast.error).toHaveBeenCalledWith('Failed to bind user groups', 'conflict'),
            );
            expect(screen.getByPlaceholderText('Search user groups…')).toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', { name: 'Close' }));
            expect(screen.queryByPlaceholderText('Search user groups…')).toBeNull();
        });

        it('says so when no groups are available (including when the fetch fails)', async () => {
            gatewayClient.fetchEndUserGroups.mockRejectedValue(new Error('down'));
            await renderTab();
            fireEvent.click(screen.getByRole('button', { name: 'Bind User Group' }));
            expect(await screen.findByText(/No user groups available/)).toBeInTheDocument();
        });

        it('unbinds, and reports a failure', async () => {
            gatewayClient.fetchResourceGroupBoundUserGroups.mockResolvedValue({ end_user_groups: [bound] });
            gatewayClient.unbindResourceGroupFromUserGroups.mockResolvedValueOnce({});
            await renderTab();
            fireEvent.click(await screen.findByRole('button', { name: 'Unbind sso' }));
            await waitFor(() =>
                expect(gatewayClient.unbindResourceGroupFromUserGroups).toHaveBeenCalledWith('rg-1', ['ug-1']),
            );
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith(
                    'User group unbound',
                    'Removed "SSO Users" from Engineering.',
                ),
            );

            gatewayClient.unbindResourceGroupFromUserGroups.mockRejectedValueOnce(new Error('locked'));
            fireEvent.click(await screen.findByRole('button', { name: 'Unbind sso' }));
            await waitFor(() =>
                expect(toast.error).toHaveBeenCalledWith('Failed to unbind user group', 'locked'),
            );
        });
    });
});

// The tab's own part of the two cases that are in
// `enterprise/src/host/.../GroupMembersTab.test.tsx` because the real Send
// Message action is on the tab there; here with a stand-in action.
describe('GroupMembersTab — with a stand-in member action', () => {
    /** The member counts the stand-in button was told, in order. */
    const counts: number[] = [];
    const standIn = {
        id: 'stand-in',
        Button: ({ memberCount, onOpen }: { memberCount: number; onOpen: () => void }) => {
            counts.push(memberCount);
            return (
                <button onClick={onOpen} disabled={memberCount === 0}>
                    stand-in action
                </button>
            );
        },
        Dialog: ({ group, onClose }: { group: { id: string }; onClose: () => void }) => (
            <div data-testid="stand-in-dialog">
                {group.id}
                <button onClick={onClose}>close-stand-in</button>
            </div>
        ),
    };

    async function renderWithStandIn() {
        render(
            <SlotProvider slots={{ 'resource-group.member-actions': [standIn] }}>
                <GroupMembersTab group={GROUP} onUpdate={onUpdate} />
            </SlotProvider>,
        );
        await membersLoaded();
    }

    beforeEach(() => {
        resetGroupMembersTab();
        counts.length = 0;
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('a failed member load shows the error and the empty state, and the action is told there is nobody', async () => {
        gateway.fetchGroupMembers.mockRejectedValue(new Error('db down'));
        await renderWithStandIn();
        expect(screen.getByText('db down')).toBeInTheDocument();
        expect(screen.getByText('No members yet')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Add Members' })).toBeEnabled();
        expect(counts[counts.length - 1]).toBe(0);
        expect(screen.getByRole('button', { name: 'stand-in action' })).toBeDisabled();
    });

    it('opens the usage modal of a member and the dialog of the action, each scoped to this group, and closes them', async () => {
        await renderWithStandIn();
        fireEvent.click(within(screen.getByTestId('row-alice')).getByText('usage'));
        expect(screen.getByTestId('usage-modal')).toHaveTextContent('rg-1/alice');
        fireEvent.click(screen.getByText('close-usage'));
        expect(screen.queryByTestId('usage-modal')).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'stand-in action' }));
        expect(screen.getByTestId('stand-in-dialog')).toHaveTextContent('rg-1');
        fireEvent.click(screen.getByText('close-stand-in'));
        expect(screen.queryByTestId('stand-in-dialog')).toBeNull();
    });
});

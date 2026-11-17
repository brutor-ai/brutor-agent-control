/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Group Members — the Send Message action.
 *
 * Messages go to the end-user Portal inbox, an Enterprise feature, so the
 * button and the dialog it opens reach the tab through the
 * `resource-group.member-actions` slot. The first
 * block is the tab with the action contributed, including the recorded
 * markup. The last block is the tab with nothing contributed.
 */

import { useState } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

/** What the dialog was given, once per time it was mounted. */
const dialogMounts: Record<string, unknown>[] = [];

const shared = vi.hoisted(() => () => import('../../../../test/groups/groupMembersTab'));
vi.mock('../../../../hooks/useGateway', async () => (await shared()).useGatewayModule());
vi.mock('../../../common/Toast', async () => (await shared()).toastModule());
vi.mock('../../../common/ConfirmationDialog', async () => (await shared()).confirmModule());
vi.mock('../GroupMembersTableView', async () => (await shared()).namesTableViewModule());

import GroupMembersTab from '../GroupMembersTab';
import { WithBuiltinSlots } from '../../../../test/builtinSlots';
import { gateway, GROUP, membersLoaded, resetGroupMembersTab } from '../../../../test/groups/groupMembersTab';
import { SlotProvider } from '../../../../app/SlotProvider';

/** `slots: false` renders the tab with nothing contributed to its slots. */
async function renderTab({ slots = true } = {}) {
    render(<GroupMembersTab group={GROUP} onUpdate={vi.fn()} />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await membersLoaded();
}

/** The last of a list that has one. */
const last = <T,>(list: T[]): T => list[list.length - 1];
/** The action bar at the top right of the tab. */
const actionBar = () => screen.getByRole('button', { name: 'Add Members' }).parentElement!;
/** The labels of the buttons in the action bar, in order. */
const actions = () => [...actionBar().querySelectorAll('button')].map((b) => b.textContent?.trim());

beforeEach(() => {
    resetGroupMembersTab();
    dialogMounts.length = 0;
    gateway.fetchEndUsers.mockResolvedValue({ users: [] });
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe('GroupMembersTab — what the tab gives a contributed action', () => {
    const seen: Record<string, unknown>[] = [];
    const item = (id: string) => ({
        id,
        Button: (p: { onOpen: () => void } & Record<string, unknown>) => {
            seen.push({ button: id, ...p });
            return <button onClick={p.onOpen}>{`open-${id}`}</button>;
        },
        Dialog: (p: { onClose: () => void } & Record<string, unknown>) => {
            seen.push({ dialog: id, ...p });
            return <button onClick={p.onClose}>{`close-${id}`}</button>;
        },
    });

    async function renderWith(ids: string[]) {
        seen.length = 0;
        render(
            <SlotProvider slots={{ 'resource-group.member-actions': ids.map(item) }}>
                <GroupMembersTab group={GROUP} onUpdate={vi.fn()} />
            </SlotProvider>,
        );
        await waitFor(() => expect(screen.queryByText('Loading members...')).toBeNull());
    }

    it('the button is given the group and how many members it has', async () => {
        await renderWith(['a']);
        const given = last(seen.filter((s) => s.button === 'a'));
        expect(Object.keys(given).sort()).toEqual(['button', 'group', 'memberCount', 'onOpen']);
        expect(given.group).toBe(GROUP);
        expect(given.memberCount).toBe(2);
    });

    it('the count is of the group, not of what the search shows', async () => {
        await renderWith(['a']);
        fireEvent.change(screen.getByPlaceholderText('Search by name, email, or username...'), {
            target: { value: 'ali' },
        });
        expect(last(seen.filter((s) => s.button === 'a')).memberCount).toBe(2);
    });

    it('only the dialog of the action that was opened is mounted, and it is given the group', async () => {
        await renderWith(['a', 'b']);
        expect(actions()).toEqual(['open-a', 'open-b', 'Add Members']);
        expect(seen.some((s) => 'dialog' in s)).toBe(false);
        fireEvent.click(screen.getByText('open-b'));
        expect(screen.getByText('close-b')).toBeInTheDocument();
        expect(screen.queryByText('close-a')).toBeNull();
        const dialog = seen.find((s) => s.dialog === 'b')!;
        expect(Object.keys(dialog).sort()).toEqual(['dialog', 'group', 'onClose']);
        expect(dialog.group).toBe(GROUP);
        fireEvent.click(screen.getByText('close-b'));
        expect(screen.queryByText('close-b')).toBeNull();
    });

    it('the actions stand after Remove when members are selected, still before Add Members', async () => {
        await renderWith(['a', 'b']);
        fireEvent.click(screen.getByText('select-all'));
        expect(actions()).toEqual(['Remove (2)', 'open-a', 'open-b', 'Add Members']);
    });

    it('the button of a group with no members is told so', async () => {
        gateway.fetchGroupMembers.mockResolvedValue({ members: [] });
        await renderWith(['a']);
        expect(last(seen.filter((s) => s.button === 'a')).memberCount).toBe(0);
        // The action is offered all the same: what it does with no members is its own.
        expect(actions()).toEqual(['open-a', 'Add Members']);
    });

    it('closing unmounts the dialog; opening again mounts a new one', async () => {
        const mounts: string[] = [];
        function CountedDialog({ onClose }: { onClose: () => void }) {
            // The initialiser runs once per mount.
            useState(() => mounts.push('dialog'));
            return <button onClick={onClose}>close-counted</button>;
        }
        render(
            <SlotProvider slots={{ 'resource-group.member-actions': [{ ...item('a'), Dialog: CountedDialog }] }}>
                <GroupMembersTab group={GROUP} onUpdate={vi.fn()} />
            </SlotProvider>,
        );
        await membersLoaded();
        fireEvent.click(screen.getByText('open-a'));
        fireEvent.click(screen.getByText('close-counted'));
        expect(screen.queryByText('close-counted')).toBeNull();
        fireEvent.click(screen.getByText('open-a'));
        expect(screen.getByText('close-counted')).toBeInTheDocument();
        // A dialog that stayed mounted would keep what was typed into it.
        expect(mounts).toHaveLength(2);
    });
});

describe('GroupMembersTab — nothing contributed', () => {
    it('has no Send Message, with or without members', async () => {
        await renderTab({ slots: false });
        expect(actions()).toEqual(['Add Members']);
        fireEvent.click(screen.getByText('select-all'));
        expect(actions()).toEqual(['Remove (2)', 'Add Members']);
        expect(screen.queryByText('Send Message')).toBeNull();
        expect(screen.queryByTestId('send-modal')).toBeNull();
        expect(dialogMounts).toHaveLength(0);
    });

    it('still lists the members and opens Add Members', async () => {
        await renderTab({ slots: false });
        expect(screen.getByTestId('table')).toHaveTextContent('alice');
        fireEvent.click(screen.getByRole('button', { name: 'Add Members' }));
        await waitFor(() => expect(gateway.fetchEndUsers).toHaveBeenCalled());
        expect(screen.getByPlaceholderText('Search by name or email...')).toBeInTheDocument();
    });
});

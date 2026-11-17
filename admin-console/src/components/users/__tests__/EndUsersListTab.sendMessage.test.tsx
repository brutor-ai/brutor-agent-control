/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * End Users list — the Send Message action.
 *
 * Messages go to the end-user Portal inbox, an Enterprise feature, so the
 * button and the dialog it opens reach the page through the
 * `end-users.actions` slot. The first block is the
 * page with the action contributed, including the recorded markup. The last
 * block is the page with nothing contributed.
 */

import { useState } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

/** What the dialog was given, once per time it was mounted. */
const dialogMounts: Record<string, unknown>[] = [];

vi.mock('../../../hooks/useGateway', async () => (await import('../../../test/users/endUsersListTab')).useGatewayModule());

import EndUsersListTab from '../EndUsersListTab';
import { WithBuiltinSlots } from '../../../test/builtinSlots';
import { actions, ALICE_NEW as ALICE, resetEndUsersList } from '../../../test/users/endUsersListTab';
import { SlotProvider } from '../../../app/SlotProvider';

/** `slots: false` renders the page with nothing contributed to its slots. */
async function loaded({ slots = true } = {}) {
    render(<EndUsersListTab />, slots ? { wrapper: WithBuiltinSlots } : undefined);
    await screen.findByText('Alice A');
}

beforeEach(() => {
    resetEndUsersList([ALICE]);
    dialogMounts.length = 0;
});

afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
});

describe('EndUsersListTab — what the page gives a contributed action', () => {
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

    it('places the actions in order and mounts only the dialog that was opened', async () => {
        seen.length = 0;
        render(
            <SlotProvider slots={{ 'end-users.actions': [item('a'), item('b')] }}>
                <EndUsersListTab />
            </SlotProvider>,
        );
        await screen.findByText('Alice A');
        expect(actions()).toEqual(['Create User', 'open-a', 'open-b', 'Export', 'Refresh']);
        expect(Object.keys(seen.find((s) => s.button === 'a')!).sort()).toEqual(['button', 'onOpen']);
        expect(seen.some((s) => 'dialog' in s)).toBe(false);
        fireEvent.click(screen.getByText('open-b'));
        expect(screen.getByText('close-b')).toBeInTheDocument();
        expect(screen.queryByText('close-a')).toBeNull();
        expect(Object.keys(seen.find((s) => s.dialog === 'b')!).sort()).toEqual(['dialog', 'onClose']);
        fireEvent.click(screen.getByText('close-b'));
        expect(screen.queryByText('close-b')).toBeNull();
    });

    it('offers the actions when the list is empty too', async () => {
        resetEndUsersList([]);
        render(
            <SlotProvider slots={{ 'end-users.actions': [item('a')] }}>
                <EndUsersListTab />
            </SlotProvider>,
        );
        expect(await screen.findByRole('button', { name: 'open-a' })).toBeEnabled();
        expect(actions()).toEqual(['Create User', 'open-a', 'Export', 'Refresh']);
    });

    it('closing unmounts the dialog; opening again mounts a new one', async () => {
        const mounts: string[] = [];
        function CountedDialog({ onClose }: { onClose: () => void }) {
            // The initialiser runs once per mount.
            useState(() => mounts.push('dialog'));
            return <button onClick={onClose}>close-counted</button>;
        }
        render(
            <SlotProvider slots={{ 'end-users.actions': [{ ...item('a'), Dialog: CountedDialog }] }}>
                <EndUsersListTab />
            </SlotProvider>,
        );
        await screen.findByText('Alice A');
        fireEvent.click(screen.getByText('open-a'));
        fireEvent.click(screen.getByText('close-counted'));
        expect(screen.queryByText('close-counted')).toBeNull();
        fireEvent.click(screen.getByText('open-a'));
        expect(screen.getByText('close-counted')).toBeInTheDocument();
        // A dialog that stayed mounted would keep what was typed into it.
        expect(mounts).toHaveLength(2);
    });
});

describe('EndUsersListTab — nothing contributed', () => {
    it('has no Send Message', async () => {
        await loaded({ slots: false });
        expect(actions()).toEqual(['Create User', 'Export', 'Refresh']);
        expect(screen.queryByText('Send Message')).toBeNull();
        expect(screen.queryByTestId('send-modal')).toBeNull();
        expect(dialogMounts).toHaveLength(0);
    });

    it('still lists the users and opens Create User', async () => {
        await loaded({ slots: false });
        expect(screen.getByText('alice@acme.test')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Create User' }));
        expect(screen.getByText('Send welcome email')).toBeInTheDocument();
    });
});

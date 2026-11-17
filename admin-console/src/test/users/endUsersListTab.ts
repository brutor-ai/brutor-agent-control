/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the tests of the end users list share (the two halves of
 * `EndUsersListTab.actions.test` and of `EndUsersListTab.sendMessage.test`:
 * the list's own cases are Community tests, the cases that need the real Send
 * Message action are under `enterprise/src/host/`): the gateway double, two
 * users, and the list the gateway answers with.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file mocks `useGateway` itself and takes the module from here.
 * This module does not import the list, so that the mock factory can await it.
 */

import { vi } from 'vitest';
import { screen } from '@testing-library/react';

import { clientOver } from '../clientOver';

export const request = vi.fn();
export const mockGateway = {
    fetchEndUsers: vi.fn(),
    fetchEndUserDetails: vi.fn(),
    updateEndUserStatus: vi.fn(),
    deleteEndUser: vi.fn(),
    exportEndUsers: vi.fn(),
    // The client's own methods, issued through `request`.
    gatewayClient: clientOver(request),
};

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => mockGateway });

/** A verified, active user who has not signed in yet. */
export const ALICE_NEW = {
    id: 1,
    username: 'alice',
    email: 'alice@acme.test',
    display_name: 'Alice A',
    is_verified: true,
    is_active: true,
    created_at: '2026-07-01T00:00:00Z',
};
export const ALICE = { ...ALICE_NEW, last_login: '2026-08-01T00:00:00Z' };
export const BOB = {
    id: 2,
    username: 'bob',
    email: 'bob@acme.test',
    is_verified: false,
    is_active: false,
    created_at: '2026-07-02T00:00:00Z',
};

export function listOf(users: unknown[], total = users.length) {
    return { users, pagination: { total, offset: 0, limit: 100, returned: users.length }, filters: {} };
}

/** The action bar at the top right of the page. */
export const actionBar = () => screen.getByRole('button', { name: 'Create User' }).parentElement!;
/** The labels of the buttons in the action bar, in order. */
export const actions = () => [...actionBar().querySelectorAll('button')].map((b) => b.textContent?.trim());

/** How every case starts: the console quiet, and `users` as the list. For a `beforeEach`. */
export function resetEndUsersList(users: unknown[]): void {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockGateway.fetchEndUsers.mockResolvedValue(listOf(users));
}

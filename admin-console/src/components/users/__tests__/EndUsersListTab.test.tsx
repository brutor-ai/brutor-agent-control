/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * End Users list — reachability.
 *
 * What this guards against is silent. A tab that issues exactly one request,
 * `{ limit: 100, offset: 0 }`, discards the response's `total`, and filters
 * the resulting array in the browser leaves every end-user past the first
 * 100 not administrable from this screen — not disableable, not deletable,
 * not inspectable — with nothing to say so: no pager, no count, no "showing
 * X of Y". Searching for a real username that happens to sort past row 100
 * would render "No users found", which reads as "that user does not exist".
 *
 * So the assertions are about what reaches the server and what the operator
 * can see, not about the table's markup: the query must carry search and
 * offset, and the total must be on screen whenever it exceeds the page.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const mockFetchEndUsers = vi.fn();

// The mock's return value is hoisted and reused rather than rebuilt per call.
// `useGateway: () => ({ ... })` mints a fresh object — and a fresh
// `gatewayClient` — on every render, so any effect or `useCallback` keyed on
// it re-fires every render. Under parallel load that turns into a render loop
// that exhausts the test timeout; in isolation the machine is fast enough to
// finish anyway, which is why this only ever failed in a full run.
const mockGateway = {
    fetchEndUsers: mockFetchEndUsers,
    fetchEndUserDetails: vi.fn(),
    updateEndUserStatus: vi.fn(),
    deleteEndUser: vi.fn(),
    exportEndUsers: vi.fn(),
    gatewayClient: {},
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => mockGateway,
}));

import EndUsersListTab from '../EndUsersListTab';

// These tests wait out a real 300ms debounce. The default 5s budget is ample
// in isolation but not when the whole suite runs in parallel on a loaded
// machine, where the timeout — not the assertion — is what fails.
vi.setConfig({ testTimeout: 20_000 });

function user(n: number, active = true) {
    return {
        id: n,
        username: `user${n}`,
        email: `user${n}@acme.test`,
        is_verified: true,
        is_active: active,
        created_at: '2026-07-01T00:00:00Z',
    };
}

function page(count: number, total: number, offset = 0) {
    return {
        users: Array.from({ length: count }, (_, i) => user(offset + i + 1)),
        pagination: { total, offset, limit: 100, returned: count },
        filters: {},
    };
}

describe('EndUsersListTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        mockFetchEndUsers.mockResolvedValue(page(100, 240));
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('discloses the total when there are more users than one page', async () => {
        render(<EndUsersListTab />);
        await waitFor(() => expect(mockFetchEndUsers).toHaveBeenCalled());

        // The response's total. Without it a truncated list
        // is indistinguishable from a complete one.
        expect(await screen.findByText(/of 240/)).toBeInTheDocument();
        expect(screen.getByText(/Showing 1–100/)).toBeInTheDocument();
    });

    it('pages forward with an offset rather than re-asking for row 0', async () => {
        render(<EndUsersListTab />);
        await waitFor(() => expect(mockFetchEndUsers).toHaveBeenCalled());
        await screen.findByText(/of 240/);

        mockFetchEndUsers.mockResolvedValue(page(100, 240, 100));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));

        await waitFor(() =>
            expect(mockFetchEndUsers).toHaveBeenLastCalledWith(
                expect.objectContaining({ offset: 100, limit: 100 }),
            ),
        );
        expect(await screen.findByText(/Showing 101–200 of 240/)).toBeInTheDocument();
    });

    it('cannot page back from the first page', async () => {
        render(<EndUsersListTab />);
        await screen.findByText(/of 240/);
        expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    });

    it('sends the search to the server instead of filtering the page', async () => {
        render(<EndUsersListTab />);
        await waitFor(() => expect(mockFetchEndUsers).toHaveBeenCalled());

        fireEvent.change(
            screen.getByPlaceholderText(/Search by username/i),
            { target: { value: 'user137' } },
        );

        // Client-side filtering could never have found user 137 — it was never
        // in the array. The server owns the whole set.
        await waitFor(
            () =>
                expect(mockFetchEndUsers).toHaveBeenLastCalledWith(
                    expect.objectContaining({ search: 'user137' }),
                ),
            { timeout: 2000 },
        );
    });

    it('debounces the search rather than querying per keystroke', async () => {
        render(<EndUsersListTab />);
        await waitFor(() => expect(mockFetchEndUsers).toHaveBeenCalled());
        const before = mockFetchEndUsers.mock.calls.length;

        const box = screen.getByPlaceholderText(/Search by username/i);
        for (const v of ['u', 'us', 'use', 'user']) {
            fireEvent.change(box, { target: { value: v } });
        }

        await waitFor(
            () =>
                expect(mockFetchEndUsers).toHaveBeenLastCalledWith(
                    expect.objectContaining({ search: 'user' }),
                ),
            { timeout: 2000 },
        );
        // Four characters, one query — not four.
        expect(mockFetchEndUsers.mock.calls.length - before).toBe(1);
    });

    it('returns to the first page when the query narrows', async () => {
        render(<EndUsersListTab />);
        await screen.findByText(/of 240/);

        mockFetchEndUsers.mockResolvedValue(page(100, 240, 100));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        await waitFor(() =>
            expect(mockFetchEndUsers).toHaveBeenLastCalledWith(
                expect.objectContaining({ offset: 100 }),
            ),
        );

        // Searching from page 2 would otherwise ask for rows 100-199 of a
        // result set that may hold three, and render an empty table.
        mockFetchEndUsers.mockResolvedValue(page(3, 3));
        fireEvent.change(
            screen.getByPlaceholderText(/Search by username/i),
            { target: { value: 'zed' } },
        );

        await waitFor(
            () =>
                expect(mockFetchEndUsers).toHaveBeenLastCalledWith(
                    expect.objectContaining({ search: 'zed', offset: 0 }),
                ),
            { timeout: 2000 },
        );
    });

    it('asks the server for active users rather than filtering the page', async () => {
        render(<EndUsersListTab />);
        await waitFor(() => expect(mockFetchEndUsers).toHaveBeenCalled());

        fireEvent.change(screen.getByRole('combobox'), { target: { value: 'active' } });

        await waitFor(() =>
            expect(mockFetchEndUsers).toHaveBeenLastCalledWith(
                expect.objectContaining({ active_only: true, offset: 0 }),
            ),
        );
    });

    it('hides the pager when everything fits on one page', async () => {
        mockFetchEndUsers.mockResolvedValue(page(4, 4));
        render(<EndUsersListTab />);
        await waitFor(() => expect(mockFetchEndUsers).toHaveBeenCalled());

        await waitFor(() =>
            expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument(),
        );
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Audit log viewer — search cost and response ordering.
 *
 * Two hazards, one root cause: `searchTerm` directly in the dependency
 * chain that drives `loadAuditLogs`, and a response written to state
 * unconditionally.
 *
 * The first makes every keystroke a `POST /audit-logs/search`. The second is
 * worse and quieter: responses do not arrive in the order they were issued, so
 * a broad query's result could land after a narrower one and repaint the table
 * with rows the filter chips no longer describe. Nothing on screen would say
 * the table and the filter had come apart — and this is the audit log, the
 * surface an operator is looking at precisely when they need to trust it.
 *
 * The ordering test resolves the requests deliberately out of order, because
 * the bug is invisible in any test that lets them settle in sequence.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const mockSearchAdminAuditLogs = vi.fn();
const mockFetchAdminAuditFacets = vi.fn();
// Stable identity — the load callback closes over `gatewayClient`, so a fresh
// object per render would re-fire the effect and mask what is being measured.
const stubClient = {
    searchAdminAuditLogs: mockSearchAdminAuditLogs,
    fetchAdminAuditFacets: mockFetchAdminAuditFacets,
    fetchAdminAuditSuggestions: vi.fn().mockResolvedValue({ values: [] }),
};

const mockExportAuditLogs = vi.fn();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ exportAuditLogs: mockExportAuditLogs, gatewayClient: stubClient }),
}));
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ user: { username: 'admin' }, isAuthenticated: true }),
}));
vi.mock('../SavedViewMenu', () => ({ default: () => null }));
vi.mock('../FacetSidebar', () => ({ default: () => null }));

import AuditLogViewer from '../AuditLogViewer';

// These tests wait out a real 300ms debounce. The default 5s budget is ample
// in isolation but not when the whole suite runs in parallel on a loaded
// machine, where the timeout — not the assertion — is what fails.
vi.setConfig({ testTimeout: 20_000 });

function logs(ids: string[], success = true) {
    return {
        logs: ids.map((id) => ({
            id,
            timestamp: '2026-07-27T10:00:00Z',
            action: `action-${id}`,
            admin_username: 'admin',
            success,
        })),
    };
}

const searchBox = () => screen.getByPlaceholderText(/search/i);

describe('AuditLogViewer', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        mockFetchAdminAuditFacets.mockResolvedValue({ facets: {} });
        mockSearchAdminAuditLogs.mockResolvedValue(logs(['a']));
        mockExportAuditLogs.mockResolvedValue({ blob: new Blob(['x']), filename: 'audit.csv' });
        // The download is a click on a link to the exported blob; the test window must not follow it.
        vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
        window.history.replaceState(null, '', '/');
    });

    it('debounces the search rather than querying per keystroke', async () => {
        render(<AuditLogViewer isLive={false} />);
        await waitFor(() => expect(mockSearchAdminAuditLogs).toHaveBeenCalled());
        const before = mockSearchAdminAuditLogs.mock.calls.length;

        for (const v of ['t', 'te', 'ten', 'tena']) {
            fireEvent.change(searchBox(), { target: { value: v } });
        }

        await waitFor(
            () =>
                expect(mockSearchAdminAuditLogs).toHaveBeenLastCalledWith(
                    expect.objectContaining({ q: 'tena' }),
                ),
            { timeout: 2000 },
        );
        expect(mockSearchAdminAuditLogs.mock.calls.length - before).toBe(1);
    });

    it('ignores a superseded response that lands after a newer one', async () => {
        // The unfiltered requests are held open; the narrowed one answers
        // first. Releasing the held ones afterwards must not repaint the table.
        const held: Array<(v: unknown) => void> = [];
        mockSearchAdminAuditLogs.mockImplementation((body: { q?: string }) => {
            if (body.q === 'narrow') return Promise.resolve(logs(['newer']));
            return new Promise((resolve) => held.push(resolve));
        });

        render(<AuditLogViewer isLive={false} />);
        await waitFor(() => expect(held.length).toBeGreaterThan(0));

        fireEvent.change(searchBox(), { target: { value: 'narrow' } });
        await waitFor(
            () => expect(screen.getByText('action-newer')).toBeInTheDocument(),
            { timeout: 2000 },
        );

        // The stale responses arrive now. Without a sequence guard the last
        // one to land wins, and the table shows rows for a filter the chips
        // no longer describe.
        held.forEach((resolve) => resolve(logs(['stale'])));
        await new Promise((r) => setTimeout(r, 50));

        expect(screen.getByText('action-newer')).toBeInTheDocument();
        expect(screen.queryByText('action-stale')).not.toBeInTheDocument();
    });

    // Filtering happens server-side, so a "N of N" readout from a state
    // mirroring `logs` would be a client-side filter indicator with nothing
    // to indicate. The counts must describe the rows actually on screen.
    it('counts only the rows the server returned', async () => {
        mockSearchAdminAuditLogs.mockResolvedValue({
            logs: [...logs(['a', 'b']).logs, ...logs(['c'], false).logs],
        });
        render(<AuditLogViewer isLive={false} />);

        await waitFor(() => expect(screen.getByText('action-a')).toBeInTheDocument());
        expect(screen.getByText('3 logs')).toBeInTheDocument();
        expect(screen.getByText('Total: 3 logs')).toBeInTheDocument();
        expect(screen.getByText(/Success:\s*2\s*\|\s*Failed:\s*1/)).toBeInTheDocument();
        // There is no "N of N" readout.
        expect(screen.queryByText(/\bof 3 logs\b/)).not.toBeInTheDocument();
    });

    describe('export', () => {
        const csv = () => screen.getByRole('button', { name: 'Download as CSV' });
        const jsonl = () => screen.getByRole('button', { name: 'JSONL' });

        it('exports what the list shows: the window, one administrator, one action and the outcome', async () => {
            window.history.replaceState(null, '', '/?a_admins=adm-1&a_actions=create_user&a_status=false');
            render(<AuditLogViewer isLive={false} />);
            await waitFor(() =>
                expect(mockSearchAdminAuditLogs).toHaveBeenLastCalledWith(
                    expect.objectContaining({ admin_ids: ['adm-1'], actions: ['create_user'], success: false, hours_ago: 24 }),
                ),
            );

            fireEvent.click(csv());
            await waitFor(() => expect(mockExportAuditLogs).toHaveBeenCalled());
            expect(mockExportAuditLogs).toHaveBeenCalledWith(
                { hours_ago: 24, admin_ids: ['adm-1'], actions: ['create_user'], success: false },
                'csv',
            );
        });

        it('exports with several values of a filter: every one is sent', async () => {
            window.history.replaceState(null, '', '/?a_admins=adm-1,adm-2&a_actions=create_user,delete_user');
            render(<AuditLogViewer isLive={false} />);
            await waitFor(() =>
                expect(mockSearchAdminAuditLogs).toHaveBeenLastCalledWith(expect.objectContaining({ admin_ids: ['adm-1', 'adm-2'] })),
            );

            expect(csv()).toBeEnabled();
            expect(jsonl()).toBeEnabled();
            expect(screen.queryByTestId('audit-logs-export-blocked')).toBeNull();
            fireEvent.click(jsonl());
            await waitFor(() => expect(mockExportAuditLogs).toHaveBeenCalled());
            expect(mockExportAuditLogs).toHaveBeenCalledWith(
                { hours_ago: 24, admin_ids: ['adm-1', 'adm-2'], actions: ['create_user', 'delete_user'] },
                'jsonl',
            );
        });

        it('exports with a resource type selected: it is sent like the others', async () => {
            window.history.replaceState(null, '', '/?a_resources=tenant');
            render(<AuditLogViewer isLive={false} />);
            await waitFor(() =>
                expect(mockSearchAdminAuditLogs).toHaveBeenLastCalledWith(expect.objectContaining({ resource_types: ['tenant'] })),
            );
            expect(csv()).toBeEnabled();
            fireEvent.click(csv());
            await waitFor(() => expect(mockExportAuditLogs).toHaveBeenCalled());
            expect(mockExportAuditLogs).toHaveBeenCalledWith({ hours_ago: 24, resource_types: ['tenant'] }, 'csv');
        });
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Proxy log viewer — the wiring between the filter controls and the wire.
 *
 * The envelope itself is unit-tested in proxyLogFilterEnvelope.test.ts. What
 * is asserted here is that the component actually routes every consumer
 * through it: the table search, and — the one that matters most — the export.
 * The export takes the body of the search, so the file holds the rows the
 * table shows for the same filters.
 *
 * Also pinned: the search debounce, so typing a correlation ID does not fire
 * one full-text scan per character against the GIN index.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const mockFetchProxyLogs = vi.fn();
const mockExportProxyLogs = vi.fn();
const mockFetchFacets = vi.fn();

// Stable identity — the load callback closes over `gatewayClient`, so a fresh
// object per render would re-fire the effect and mask what is measured.
const stubClient = {
    fetchProxyLogFacets: mockFetchFacets,
    fetchProxyLogFacetSuggestions: vi.fn().mockResolvedValue({ values: [] }),
    listSavedLogViews: vi.fn().mockResolvedValue({ views: [] }),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchProxyLogs: mockFetchProxyLogs,
        exportProxyLogs: mockExportProxyLogs,
        gatewayClient: stubClient,
    }),
}));
vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ user: { id: 'adm_1', username: 'admin' }, isAuthenticated: true }),
}));
// The menu is replaced by a recorder of what the viewer hands it: applying a
// saved view is a call of `onApply` with the stored filter envelope.
const savedViewMenu = vi.hoisted(() => ({ onApply: null as null | ((filters: Record<string, unknown>) => void) }));
vi.mock('../SavedViewMenu', () => ({
    default: (props: { onApply: (filters: Record<string, unknown>) => void }) => {
        savedViewMenu.onApply = props.onApply;
        return null;
    },
}));
vi.mock('../FacetSidebar', () => ({ default: () => null }));

import ProxyLogsViewer from '../ProxyLogsViewer';

// These tests wait out a real debounce; the default 5s budget is not enough
// when the whole suite runs in parallel on a loaded machine.
vi.setConfig({ testTimeout: 20_000 });

const emptyPage = {
    logs: [],
    pagination: { total: 0, offset: 0, limit: 50, returned: 0, has_more: false },
};

const searchBox = () => screen.getByPlaceholderText(/search/i);

describe('ProxyLogsViewer', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        // The download is a click on a link to the exported blob; the test window must not follow it.
        vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
        mockFetchProxyLogs.mockResolvedValue(emptyPage);
        mockFetchFacets.mockResolvedValue({ facets: {} });
        mockExportProxyLogs.mockResolvedValue({ blob: new Blob(['x']), filename: 'logs.csv' });
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
        window.history.replaceState(null, '', '/');
    });

    it('debounces the free-text search rather than querying per keystroke', async () => {
        render(<ProxyLogsViewer />);
        await waitFor(() => expect(mockFetchProxyLogs).toHaveBeenCalled());
        const before = mockFetchProxyLogs.mock.calls.length;

        for (const v of ['c', 'co', 'cor', 'corr']) {
            fireEvent.change(searchBox(), { target: { value: v } });
        }

        await waitFor(
            () =>
                expect(mockFetchProxyLogs).toHaveBeenLastCalledWith(
                    expect.objectContaining({ q: 'corr' }),
                ),
            { timeout: 3000 },
        );
        expect(mockFetchProxyLogs.mock.calls.length - before).toBe(1);
    });

    it('exports what the table was searched with: the same filters, and the sort of the table', async () => {
        window.history.replaceState(null, '', '/?p_servers=srv-a,srv-b&p_success=false');
        render(<ProxyLogsViewer />);
        await waitFor(() =>
            expect(mockFetchProxyLogs).toHaveBeenLastCalledWith(
                expect.objectContaining({ server_ids: ['srv-a', 'srv-b'], success: false }),
            ),
        );

        fireEvent.click(screen.getByRole('button', { name: 'Download as CSV' }));
        await waitFor(() => expect(mockExportProxyLogs).toHaveBeenCalled());

        expect(mockExportProxyLogs).toHaveBeenLastCalledWith(
            expect.objectContaining({
                hours_ago: 24,
                server_ids: ['srv-a', 'srv-b'],
                success: false,
                sort_by: 'timestamp',
                sort_order: 'desc',
            }),
            'csv',
        );
    });

    it('exports with the free-text search set: the export applies every filter of the list', async () => {
        render(<ProxyLogsViewer />);
        await waitFor(() => expect(mockFetchProxyLogs).toHaveBeenCalled());

        fireEvent.change(searchBox(), { target: { value: 'timeout' } });
        await waitFor(
            () => expect(mockFetchProxyLogs).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'timeout' })),
            { timeout: 3000 },
        );
        expect(screen.getByRole('button', { name: 'JSONL' })).toBeEnabled();
        fireEvent.click(screen.getByRole('button', { name: 'JSONL' }));
        await waitFor(() =>
            expect(mockExportProxyLogs).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'timeout' }), 'jsonl'),
        );
        expect(screen.queryByTestId('proxy-logs-export-blocked')).toBeNull();
    });

    it('a saved view sets the compliance filter from its list of tags, the one name the envelope has for it', async () => {
        render(<ProxyLogsViewer />);
        await waitFor(() => expect(mockFetchProxyLogs).toHaveBeenCalled());

        act(() => savedViewMenu.onApply!({ compliance_tags_any: ['gdpr_art_30', 'soc2.cc7.2'] }));
        await waitFor(() =>
            expect(mockFetchProxyLogs).toHaveBeenLastCalledWith(
                expect.objectContaining({ compliance_tags_any: ['gdpr_art_30', 'soc2.cc7.2'] }),
            ),
        );

        // A single tag under another key is not a field of the envelope.
        act(() => savedViewMenu.onApply!({ compliance_tag: 'gdpr_art_30', server_ids: ['srv-a'] }));
        await waitFor(() =>
            expect(mockFetchProxyLogs).toHaveBeenLastCalledWith(expect.objectContaining({ server_ids: ['srv-a'] })),
        );
        const [lastSearch] = mockFetchProxyLogs.mock.lastCall as [Record<string, unknown>];
        expect(lastSearch.compliance_tags_any).toBeUndefined();
    });
});

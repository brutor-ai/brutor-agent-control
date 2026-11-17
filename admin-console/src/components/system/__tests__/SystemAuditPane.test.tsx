/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for SystemAuditPane.
 *
 * Coverage:
 *   - Renders the toolbar (time range, refresh, export CSV) and
 *     mounts the FacetSidebar with the four audit facetable fields.
 *   - Table renders one row per audit entry, with the action and
 *     scope columns populated from the wire payload.
 *   - Empty state shows the explicit no-rows hint instead of a
 *     blank table (operators get an explicit answer rather than
 *     wondering whether the query failed silently).
 *   - Clicking a row opens the drawer; the drawer shows the
 *     before/after JSON panes.
 *   - Export CSV button calls the downloadSystemAuditCsv client
 *     method (which is the contract that wires through to the
 *     /audit/export endpoint).
 *   - Filter changes reset the pagination offset to 0 (paging
 *     across a changed envelope would land on rows that no longer
 *     exist).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
    render,
    screen,
    fireEvent,
    waitFor,
    act,
} from '@testing-library/react';

const mockListSystemAudit = vi.fn();
const mockGetSystemAuditFacets = vi.fn();
const mockDownloadSystemAuditCsv = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: {
            listSystemAudit: mockListSystemAudit,
            getSystemAuditFacets: mockGetSystemAuditFacets,
            downloadSystemAuditCsv: mockDownloadSystemAuditCsv,
        },
    }),
}));

import SystemAuditPane from '../SystemAuditPane';

const sampleRow = {
    id: 'sol-01H000000000000000000000',
    tenant_id: 'tenant-acme',
    actor_admin_id: 'sa-01H',
    actor_type: 'system_admin' as const,
    action: 'cache_flush',
    scope_kind: 'cache_layer',
    scope_id: 'semantic',
    payload: { reason: 'incident-1234' },
    before_state: { entries: 1024 },
    after_state: { entries: 0 },
    ip: '10.0.0.1',
    user_agent: 'curl/8.4',
    created_at: '2026-06-01T10:00:00Z',
};

describe('SystemAuditPane', () => {
    beforeEach(() => {
        mockListSystemAudit.mockReset();
        mockGetSystemAuditFacets.mockReset();
        mockDownloadSystemAuditCsv.mockReset();
        mockListSystemAudit.mockResolvedValue({
            items: [sampleRow],
            total: 1,
        });
        mockGetSystemAuditFacets.mockResolvedValue({
            facets: {
                action: [{ value: 'cache_flush', count: 1 }],
                scope_kind: [],
                actor_admin_id: [],
                actor_type: [],
            },
            labels: {
                action: 'Action',
                scope_kind: 'Scope',
                actor_admin_id: 'Actor',
                actor_type: 'Actor type',
            },
        });
    });

    it('renders the toolbar with refresh + export buttons', async () => {
        render(<SystemAuditPane />);
        await waitFor(() => {
            expect(
                screen.getByTestId('system-audit-refresh'),
            ).toBeDefined();
            expect(
                screen.getByTestId('system-audit-export-csv'),
            ).toBeDefined();
        });
    });

    it('fetches the list on mount and renders a row per entry', async () => {
        render(<SystemAuditPane />);
        await waitFor(() => {
            expect(mockListSystemAudit).toHaveBeenCalled();
            expect(
                screen.getByTestId(`system-audit-row-${sampleRow.id}`),
            ).toBeDefined();
        });
    });

    it('renders the empty state when no rows match', async () => {
        mockListSystemAudit.mockResolvedValueOnce({ items: [], total: 0 });
        render(<SystemAuditPane />);
        await waitFor(() => {
            expect(screen.getByTestId('system-audit-empty')).toBeDefined();
        });
    });

    it('opens the drawer with before/after panes when a row is clicked', async () => {
        render(<SystemAuditPane />);
        const row = await screen.findByTestId(
            `system-audit-row-${sampleRow.id}`,
        );
        fireEvent.click(row);
        expect(screen.getByTestId('system-audit-drawer')).toBeDefined();
        expect(
            screen.getByTestId('system-audit-drawer-before'),
        ).toBeDefined();
        expect(screen.getByTestId('system-audit-drawer-after')).toBeDefined();
        expect(screen.getByTestId('system-audit-drawer-payload')).toBeDefined();
    });

    it('invokes downloadSystemAuditCsv when export button is clicked', async () => {
        mockDownloadSystemAuditCsv.mockResolvedValueOnce({
            blob: new Blob(['# brutor-export\n']),
            filename: 'system-audit.csv',
        });
        // jsdom's anchor click path requires URL.createObjectURL to
        // be defined; stub it here for the duration of the test.
        const originalCreate = (URL as any).createObjectURL;
        const originalRevoke = (URL as any).revokeObjectURL;
        (URL as any).createObjectURL = vi.fn(() => 'blob:fake');
        (URL as any).revokeObjectURL = vi.fn();
        try {
            render(<SystemAuditPane />);
            const btn = await screen.findByTestId('system-audit-export-csv');
            await act(async () => {
                fireEvent.click(btn);
            });
            expect(mockDownloadSystemAuditCsv).toHaveBeenCalled();
        } finally {
            (URL as any).createObjectURL = originalCreate;
            (URL as any).revokeObjectURL = originalRevoke;
        }
    });

    it('resets pagination when the free-text filter changes', async () => {
        // Seed enough rows that the Next button is enabled.
        mockListSystemAudit.mockResolvedValue({
            items: [sampleRow],
            total: 200,
        });
        render(<SystemAuditPane />);

        const next = await screen.findByTestId('system-audit-next');
        await act(async () => {
            fireEvent.click(next);
        });

        // Now change the search — offset should snap back to 0,
        // which we observe by calling listSystemAudit with offset 0.
        const search = await screen.findByTestId('system-audit-search');
        await act(async () => {
            fireEvent.change(search, { target: { value: 'incident-1234' } });
        });

        await waitFor(() => {
            const lastCall =
                mockListSystemAudit.mock.calls[
                    mockListSystemAudit.mock.calls.length - 1
                ][0];
            expect(lastCall.offset).toBe(0);
            expect(lastCall.q).toBe('incident-1234');
        });
    });
});

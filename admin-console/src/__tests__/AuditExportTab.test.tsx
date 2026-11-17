/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * AuditExportTab test
 *
 * Scope: smoke test that the list view renders destinations, the empty
 * state, and the action buttons in the right shape. The form's
 * submit-translation logic is unit-tested in AuditExportForm.test.tsx;
 * here we focus on the list / row / status-badge behaviour.
 *
 * We mock `useGateway` to skip the live API client (which expects a
 * JWT + a running backend). The component under test only knows about
 * the hook, so this stays a pure UI test.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import AuditExportTab from '../components/audit-export/AuditExportTab';
import { AuditExportDestination } from '../lib/types';

// Mock the toast notifier — the production Toaster is wired at
// index.tsx (see toasterMounted.test.ts) and isn't present in tests.
vi.mock('../components/common/Toast', () => ({
    useToast: () => ({
        success: vi.fn(),
        error: vi.fn(),
        loading: vi.fn(),
        dismiss: vi.fn(),
    }),
}));

// Stash mock implementations per-test via these refs so each test
// gets a fresh fetch behaviour without the cost of a per-test
// `vi.doMock`.
let mockFetchAllImpl: () => Promise<{ destinations: AuditExportDestination[]; total: number }>;
// `mockFetchAll` is ONE function for the whole file, as the real hook's
// `fetchAuditExportDestinations` is one function for the life of the page
// (`useCallback` in hooks/gateway/auditExport.ts). The tab's load effect is
// keyed on it; see 'loads the list once' below for what a new function per
// render does to it.
let fetchAllCalls = 0;
const mockFetchAll = (): Promise<{ destinations: AuditExportDestination[]; total: number }> => {
    fetchAllCalls += 1;
    return mockFetchAllImpl();
};
const mockFetchOne = vi.fn();
const mockDeleteDest = vi.fn();
const mockRunNow = vi.fn();

vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAuditExportDestinations: mockFetchAll,
        fetchAuditExportDestination: mockFetchOne,
        deleteAuditExportDestination: mockDeleteDest,
        runAuditExportDestinationNow: mockRunNow,
        createAuditExportDestination: vi.fn(),
        updateAuditExportDestination: vi.fn(),
    }),
}));


function buildDestination(overrides: Partial<AuditExportDestination> = {}): AuditExportDestination {
    return {
        id: 'axd-test-1',
        tenant_id: 'test-tenant',
        name: 'Test S3 Vault',
        destination_type: 's3',
        enabled: true,
        config: { bucket: 'test-bucket' },
        field_allowlist: null,
        interval_seconds: 3600,
        last_exported_at: null,
        last_run_at: null,
        last_run_status: null,
        last_run_rows: null,
        last_run_error: null,
        ...overrides,
    };
}


describe('AuditExportTab', () => {
    beforeEach(() => {
        mockFetchOne.mockReset();
        mockDeleteDest.mockReset();
        mockRunNow.mockReset();
        fetchAllCalls = 0;
    });

    it('renders the empty state when no destinations are configured', async () => {
        mockFetchAllImpl = async () => ({ destinations: [], total: 0 });
        render(<AuditExportTab />);
        await waitFor(() => {
            expect(screen.getByText(/No destinations configured/i)).toBeInTheDocument();
        });
    });

    it('loads the list once: rendering again does not fetch again', async () => {
        // The tab loads in an effect keyed on `fetchAuditExportDestinations`.
        // The real hook returns it from `useCallback`, so it is the same
        // function on every render. A mock that hands out a new function per
        // render makes the effect run after every render, and every load
        // renders: an endless fetch loop in which the loaded list is on
        // screen only between two loads.
        mockFetchAllImpl = () => Promise.resolve({ destinations: [], total: 0 });
        const { rerender } = render(<AuditExportTab />);
        await waitFor(() => {
            expect(screen.getByText(/No destinations configured/i)).toBeInTheDocument();
        });
        rerender(<AuditExportTab />);
        await act(async () => {});
        expect(fetchAllCalls).toBe(1);
        expect(screen.getByText(/No destinations configured/i)).toBeInTheDocument();
    });

    it('renders each destination as a table row', async () => {
        mockFetchAllImpl = async () => ({
            destinations: [
                buildDestination({ id: 'axd-1', name: 'Splunk HEC', destination_type: 'webhook' }),
                buildDestination({ id: 'axd-2', name: 'S3 Vault', destination_type: 's3' }),
            ],
            total: 2,
        });
        render(<AuditExportTab />);
        await waitFor(() => {
            expect(screen.getByText('Splunk HEC')).toBeInTheDocument();
            expect(screen.getByText('S3 Vault')).toBeInTheDocument();
        });
    });

    it('shows the success badge when last_run_status is success', async () => {
        mockFetchAllImpl = async () => ({
            destinations: [buildDestination({
                last_run_status: 'success',
                last_run_rows: 42,
                last_run_at: new Date().toISOString(),
            })],
            total: 1,
        });
        render(<AuditExportTab />);
        await waitFor(() => {
            expect(screen.getByText(/Success/i)).toBeInTheDocument();
            expect(screen.getByText(/42 rows/i)).toBeInTheDocument();
        });
    });

    it('shows the failed badge with the error tooltip when last_run_status is failure', async () => {
        mockFetchAllImpl = async () => ({
            destinations: [buildDestination({
                last_run_status: 'failure',
                last_run_error: 'S3 Access Denied: 403',
                last_run_at: new Date().toISOString(),
            })],
            total: 1,
        });
        render(<AuditExportTab />);
        await waitFor(() => {
            // The failure badge carries the error text in the title attribute
            // so operators get the cause on hover without expanding a row.
            const failed = screen.getByText(/Failed/i);
            expect(failed).toBeInTheDocument();
            expect(failed.closest('[title]')?.getAttribute('title')).toMatch(/Access Denied/);
        });
    });

    it('shows the "Disabled" tag for destinations with enabled=false', async () => {
        mockFetchAllImpl = async () => ({
            destinations: [buildDestination({ enabled: false })],
            total: 1,
        });
        render(<AuditExportTab />);
        await waitFor(() => {
            expect(screen.getByText('Disabled')).toBeInTheDocument();
        });
    });

    it('surfaces a fetch error in the error banner', async () => {
        mockFetchAllImpl = async () => { throw new Error('Backend unreachable'); };
        render(<AuditExportTab />);
        await waitFor(() => {
            expect(screen.getByText(/Failed to load destinations/i)).toBeInTheDocument();
            expect(screen.getByText(/Backend unreachable/i)).toBeInTheDocument();
        });
    });
});

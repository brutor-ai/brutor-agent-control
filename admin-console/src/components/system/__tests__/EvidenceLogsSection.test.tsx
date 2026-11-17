/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * System → Audit Chains → tenant evidence logs: logs with
 * the latest head grade, heads with receipts per witness, and a head left
 * standalone / self-witnessed for over an hour shown as a finding — never
 * phrased as a verdict on the records.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

const listEvidenceLogs = vi.fn();
const listEvidenceLogTreeHeads = vi.fn();
const gateway = { gatewayClient: { listEvidenceLogs, listEvidenceLogTreeHeads } };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import EvidenceLogsSection from '../EvidenceLogsSection';

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();

const LOGS = {
    items: [
        {
            log_id: 'log-a', tenant_id: 'acme', tree_size: 1200, root: 'aa'.repeat(32), kid: 'k1'.repeat(32),
            heads_count: 3, receipts_count: 4, latest_grade: 'standalone',
            last_tree_head: { id: 'h3', tree_size: 1200, grade: 'standalone', issued_at: hoursAgo(3) },
        },
        {
            log_id: 'log-b', tenant_id: 'globex', tree_size: 50, root: 'bb'.repeat(32), kid: null,
            heads_count: 1, receipts_count: 2, latest_grade: 'plural',
            last_tree_head: { id: 'hb', tree_size: 50, grade: 'plural', issued_at: hoursAgo(5) },
        },
    ],
    total: 2,
};

const HEADS = {
    items: [
        { id: 'h3', log_id: 'log-a', tenant_id: 'acme', tree_size: 1200, root: 'c3'.repeat(32), prev_size: 1100, grade: 'standalone', issued_at: hoursAgo(3), receipts: [] },
        { id: 'h2', log_id: 'log-a', tenant_id: 'acme', tree_size: 1100, root: 'c2'.repeat(32), prev_size: 1000, grade: 'self_witnessed', issued_at: hoursAgo(0.2),
          receipts: [{ id: 'r0', witness_id: 'wit-local', kind: 'scitt', verdict: 'witnessed', verified_at: hoursAgo(0.1) }] },
        { id: 'h1', log_id: 'log-a', tenant_id: 'acme', tree_size: 1000, root: 'c1'.repeat(32), prev_size: 0, grade: 'witnessed', issued_at: hoursAgo(30),
          receipts: [
              { id: 'r1', witness_id: 'wit-auditor', kind: 'scitt', verdict: 'witnessed', verified_at: hoursAgo(29) },
              { id: 'r2', witness_id: 'wit-tsa', kind: 'rfc3161', verdict: 'timestamped', verified_at: hoursAgo(29) },
              { id: 'r3', witness_id: 'wit-bad', kind: 'scitt', verdict: 'invalid', error: 'signature mismatch' },
          ] },
    ],
    total: 3, limit: 20, offset: 0,
};

beforeEach(() => {
    vi.clearAllMocks();
    listEvidenceLogs.mockResolvedValue(LOGS);
    listEvidenceLogTreeHeads.mockResolvedValue(HEADS);
});

describe('EvidenceLogsSection', () => {
    it('lists tenant logs with their latest grade and flags an unwitnessed latest head', async () => {
        render(<EvidenceLogsSection />);
        await screen.findByTestId('evidence-log-log-a');
        expect(screen.getByTestId('evidence-log-log-a')).toHaveTextContent('acme');
        expect(screen.getByTestId('evidence-log-grade-log-a')).toHaveTextContent('standalone');
        expect(screen.getByTestId('evidence-log-grade-log-b')).toHaveTextContent('plural');
        expect(screen.getByTestId('evidence-log-finding-log-a')).toBeInTheDocument();
        expect(screen.queryByTestId('evidence-log-finding-log-b')).toBeNull();
    });

    it('loads heads for the log’s own tenant and shows receipts per witness', async () => {
        render(<EvidenceLogsSection />);
        fireEvent.click(await screen.findByTestId('evidence-log-toggle-log-a'));
        await screen.findByTestId('evidence-heads-log-a');
        expect(listEvidenceLogTreeHeads).toHaveBeenCalledWith('log-a', { tenantId: 'acme', limit: 20 });
        const receipts = screen.getByTestId('evidence-head-receipts-h1');
        expect(receipts).toHaveTextContent('wit-auditor');
        expect(receipts).toHaveTextContent('timestamped');
        expect(receipts).toHaveTextContent('signature mismatch');
        expect(screen.getByTestId('evidence-head-grade-h2')).toHaveTextContent('self-witnessed');
    });

    it('shows a skipped receipt as unwitnessed because the witness is switched off', async () => {
        listEvidenceLogTreeHeads.mockResolvedValueOnce({
            items: [{ id: 'h9', log_id: 'log-a', tenant_id: 'acme', tree_size: 1300, root: 'd4'.repeat(32), prev_size: 1200, grade: 'standalone', issued_at: hoursAgo(0.2),
                receipts: [{ id: 'r9', witness_id: 'wit-auditor', kind: 'scitt', verdict: 'skipped', error: 'not submitted: witness registration is switched off' }] }],
            total: 1,
        });
        render(<EvidenceLogsSection />);
        fireEvent.click(await screen.findByTestId('evidence-log-toggle-log-a'));
        const receipts = await screen.findByTestId('evidence-head-receipts-h9');
        expect(receipts).toHaveTextContent('unwitnessed (witness switched off)');
        expect(receipts).not.toHaveTextContent('skipped');
    });

    it('marks only heads unwitnessed for over an hour, as a finding and not a verdict', async () => {
        render(<EvidenceLogsSection />);
        fireEvent.click(await screen.findByTestId('evidence-log-toggle-log-a'));
        const finding = await screen.findByTestId('evidence-head-finding-h3');
        expect(finding).toHaveTextContent(/Finding/);
        expect(finding).toHaveTextContent('records under this head are unaffected');
        expect(screen.queryByTestId('evidence-head-finding-h2')).toBeNull(); // self-witnessed, but only 12 min old
        expect(screen.queryByTestId('evidence-head-finding-h1')).toBeNull(); // witnessed
    });

    it('a head load failure says no check ran', async () => {
        listEvidenceLogTreeHeads.mockRejectedValue(new Error('403'));
        render(<EvidenceLogsSection />);
        fireEvent.click(await screen.findByTestId('evidence-log-toggle-log-a'));
        expect(await screen.findByTestId('evidence-heads-error-log-a')).toHaveTextContent('no check ran');
    });

    it('empty and error states', async () => {
        listEvidenceLogs.mockResolvedValueOnce({ items: [], total: 0 });
        const { unmount } = render(<EvidenceLogsSection />);
        expect(await screen.findByTestId('evidence-logs-empty')).toBeInTheDocument();
        unmount();
        listEvidenceLogs.mockRejectedValueOnce(new Error('down'));
        render(<EvidenceLogsSection />);
        expect(await screen.findByTestId('evidence-logs-error')).toHaveTextContent('down');
    });
});

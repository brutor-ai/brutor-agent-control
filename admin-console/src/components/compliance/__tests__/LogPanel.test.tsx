/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const listEvidenceLogs = vi.fn();
const listEvidenceTreeHeads = vi.fn();
const listEvidenceWitnesses = vi.fn();
const getEvidenceSummary = vi.fn();

const gateway = { gatewayClient: { listEvidenceLogs, listEvidenceTreeHeads, listEvidenceWitnesses, getEvidenceSummary } };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));

import LogPanel from '../LogPanel';

const LOG = { log_id: 'log-1', tenant_id: 't', tree_size: 1234, root: 'abcdef0123456789', kid: 'kid-1', heads_count: 12, receipts_count: 9, last_tree_head: { id: 'h2', tree_size: 1234, issued_at: '2026-09-19T10:00:00Z', grade: 'witnessed' } };
const HEADS = {
    items: [
        { id: 'h2', tree_size: 1234, root: 'r2', issued_at: '2026-09-19T10:00:00Z', grade: 'witnessed', receipts: [{ id: 'rc3', witness_id: 'w1', kind: 'scitt', verdict: 'witnessed' }] },
        { id: 'h1', tree_size: 1000, root: 'r1', issued_at: '2026-09-18T10:00:00Z', grade: 'self_witnessed', receipts: [{ id: 'rc1', witness_id: 'w2', kind: 'rfc3161', verdict: 'unverified', error: 'no pinned key' }] },
    ],
    total: 12,
};
const WITNESSES = {
    items: [
        { id: 'w1', kind: 'scitt', scope: 'platform', operator_label: 'Brutor witness', same_operator: true, enabled: true, pinned_key: 'a'.repeat(64), url: 'https://w1' },
        { id: 'w2', kind: 'rfc3161', scope: 'tenant', operator_label: 'DigiStamp', same_operator: false, enabled: true, pinned_key: null, url: 'https://w2' },
        { id: 'w3', kind: 'scitt', scope: 'tenant', operator_label: 'Idle', same_operator: false, enabled: false, pinned_key: 'b'.repeat(64), url: 'https://w3' },
    ],
    total: 3,
};

beforeEach(() => {
    vi.clearAllMocks();
    listEvidenceLogs.mockResolvedValue({ items: [LOG], total: 1 });
    listEvidenceTreeHeads.mockResolvedValue(HEADS);
    listEvidenceWitnesses.mockResolvedValue(WITNESSES);
    getEvidenceSummary.mockResolvedValue({ log: { unwitnessed_heads: 3 } });
});

describe('LogPanel', () => {
    it('shows the log, the unwitnessed backlog, and each witness with its latest receipt verdict', async () => {
        render(<LogPanel />);
        await screen.findByTestId('log-panel-status');
        expect(screen.getByTestId('log-panel-status')).toHaveTextContent('1,234');
        expect(screen.getByTestId('log-panel-status')).toHaveTextContent('witnessed');
        expect(screen.getByTestId('log-panel-backlog')).toHaveTextContent('3 heads without a receipt');
        expect(screen.getByTestId('witness-w1')).toHaveTextContent('Brutor witness');
        expect(screen.getByTestId('witness-w1')).toHaveTextContent('same operator');
        expect(screen.getByTestId('witness-w1')).toHaveTextContent('witnessed');
        expect(screen.getByTestId('witness-w2')).toHaveTextContent('no pinned key');
        expect(screen.getByTestId('witness-w2')).toHaveTextContent('unverified');
        expect(screen.getByTestId('witness-w3')).toHaveTextContent('disabled');
        expect(screen.getByTestId('witness-w3')).toHaveTextContent('no receipt among the latest heads');
        expect(screen.getByTestId('tree-head-h1')).toHaveTextContent('self-witnessed');
        expect(screen.getByText('Showing the latest 2 of 12 heads.')).toBeInTheDocument();
    });

    it('shows a skipped receipt as unwitnessed because the witness is switched off, not as an unknown verdict', async () => {
        const reason = 'not submitted: witness registration is switched off';
        listEvidenceTreeHeads.mockResolvedValueOnce({
            items: [{ id: 'h3', tree_size: 1300, root: 'r3', issued_at: '2026-09-20T10:00:00Z', grade: 'standalone', receipts: [{ id: 'rc9', witness_id: 'w1', kind: 'scitt', verdict: 'skipped', error: reason }] }],
            total: 1,
        });
        render(<LogPanel />);
        await screen.findByTestId('tree-head-h3');
        expect(screen.getByTestId('tree-head-h3')).toHaveTextContent('scitt · unwitnessed (witness switched off)');
        expect(screen.getByTestId('tree-head-h3')).not.toHaveTextContent('skipped');
        expect(screen.getByTestId('witness-w1')).toHaveTextContent('unwitnessed (witness switched off)');
    });

    it('degrades the side information without losing the log', async () => {
        listEvidenceWitnesses.mockRejectedValueOnce(new Error('no'));
        getEvidenceSummary.mockRejectedValueOnce(new Error('no'));
        render(<LogPanel />);
        await screen.findByTestId('log-panel-status');
        expect(screen.getByTestId('log-panel-backlog')).toHaveTextContent('Failed to load the unwitnessed backlog: no');
        expect(screen.getByTestId('log-panel-witnesses')).toHaveTextContent('Failed to load the witnesses: no');
        expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(2);
    });

    it('says when no witness is configured and when there is no log', async () => {
        listEvidenceWitnesses.mockResolvedValueOnce({ items: [], total: 0 });
        const { unmount } = render(<LogPanel />);
        expect(await screen.findByTestId('log-panel-no-witnesses')).toHaveTextContent('every head stays standalone');
        unmount();

        listEvidenceLogs.mockResolvedValueOnce({ items: [], total: 0 });
        render(<LogPanel />);
        expect(await screen.findByTestId('log-panel-empty')).toBeInTheDocument();
    });
});

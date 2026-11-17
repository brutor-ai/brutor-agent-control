/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Evidence Ledger: the record explorer with all 14 verdicts in its
 * filter, the chain shortcut per row, the chain view, the log, verify and
 * bundle panels, and the disclosure list with selector and digest. The
 * panels have their own tests; here they are stubs.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const listEvidenceRecords = vi.fn();
const getEvidenceSummary = vi.fn();
const listEvidenceDisclosures = vi.fn();
const fetchAISystems = vi.fn();

const gateway = {
    fetchAISystems,
    gatewayClient: { listEvidenceRecords, getEvidenceSummary, listEvidenceDisclosures },
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));
vi.mock('../LogPanel', () => ({ default: () => <div data-testid="log-stub" /> }));
vi.mock('../VerifyPanel', () => ({ default: () => <div data-testid="verify-stub" /> }));
vi.mock('../BundleExportPanel', () => ({
    default: (p: { framework: string; onDisclosed?: () => void; systems: { id: string; name: string }[] }) => (
        <div data-testid="bundle-stub">
            {p.framework}·{p.systems.map((s) => s.name).join(',')}
            <button onClick={() => p.onDisclosed?.()}>disclosed</button>
        </div>
    ),
}));
vi.mock('../ChainPanel', () => ({
    default: (p: { recordId: string; onClose: () => void }) => (
        <div data-testid="chain-stub">
            {p.recordId}
            <button onClick={p.onClose}>close chain</button>
        </div>
    ),
}));
vi.mock('../RecordDialog', () => ({
    default: (p: { recordId: string; onClose: () => void }) => (
        <div data-testid="record-dialog-stub">
            {p.recordId}
            <button onClick={p.onClose}>close</button>
        </div>
    ),
}));

import EvidenceLedgerPage from '../pages/EvidenceLedgerPage';
import { RECORD_VERDICTS } from '../../../lib/complianceEvidenceTypes';

const RECORDS = {
    items: Array.from({ length: 3 }, (_, i) => ({
        record_id: `rec-${i}`,
        log_id: 'log-1',
        seq: i + 1,
        ai_system_id: 'sys-1',
        verdict: i === 1 ? 'blocked' : 'executed',
        kind: 'decide',
        surface: 'llm',
        effect_type: 'llm.completion',
        timestamp: '2026-09-19T10:00:00Z',
        human_disposed: i === 2,
        chain: i === 2 ? { parent_record_id: 'rec-0', relation: 'confirms' } : null,
    })),
    total: 120,
    limit: 50,
    offset: 0,
};

const SUMMARY = {
    records_total: 120,
    by_verdict: { executed: 100, blocked: 20 },
    by_kind: {},
    by_surface: {},
    human_disposed_count: 5,
    systems_covered: 1,
    log: null,
};

const props = () => ({ framework: 'eu-ai-act', frameworks: [], registry: [], refreshFrameworks: vi.fn(), onTabChange: vi.fn() });

beforeEach(() => {
    vi.clearAllMocks();
    listEvidenceRecords.mockResolvedValue(RECORDS);
    getEvidenceSummary.mockResolvedValue(SUMMARY);
    listEvidenceDisclosures.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
    fetchAISystems.mockResolvedValue({ systems: [{ id: 'sys-1', name: 'copilot', display_name: 'Copilot', governance_gaps: [] }], total: 1 });
});

describe('EvidenceLedgerPage', () => {
    it('lists records with verdict chips, the summary strip, and every panel', async () => {
        render(<EvidenceLedgerPage {...props()} />);
        await screen.findByTestId('record-row-rec-0');
        expect(screen.getByTestId('record-row-rec-1')).toHaveTextContent('blocked');
        expect(screen.getByTestId('record-row-rec-2')).toHaveTextContent('yes');
        await waitFor(() => expect(screen.getByTestId('record-row-rec-0')).toHaveTextContent('Copilot'));
        expect(screen.getByTestId('ledger-summary')).toHaveTextContent('120 sealed');
        expect(screen.getByTestId('ledger-verdict-blocked')).toHaveTextContent('blocked 20');
        expect(screen.getByTestId('log-stub')).toBeInTheDocument();
        expect(screen.getByTestId('verify-stub')).toBeInTheDocument();
        await waitFor(() => expect(screen.getByTestId('bundle-stub')).toHaveTextContent('eu-ai-act·Copilot'));
        expect(await screen.findByTestId('disclosures-empty')).toBeInTheDocument();
    });

    it('offers all 14 verdicts in the filter', async () => {
        render(<EvidenceLedgerPage {...props()} />);
        await screen.findByTestId('record-row-rec-0');
        const options = Array.from(screen.getByTestId<HTMLSelectElement>('ledger-filter-verdict').options).map((o) => o.value);
        expect(options).toEqual(['', ...RECORD_VERDICTS]);
        expect(RECORD_VERDICTS).toHaveLength(14);
    });

    it('applies filters and pages through results', async () => {
        render(<EvidenceLedgerPage {...props()} />);
        await screen.findByTestId('record-row-rec-0');
        expect(listEvidenceRecords).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 50, offset: 0 }));

        fireEvent.change(screen.getByTestId('ledger-filter-verdict'), { target: { value: 'adjudication' } });
        fireEvent.change(screen.getByTestId('ledger-filter-since'), { target: { value: '2026-09-01' } });
        fireEvent.change(screen.getByTestId('ledger-filter-run'), { target: { value: 'run-9' } });
        fireEvent.click(screen.getByTestId('ledger-apply'));
        await waitFor(() =>
            expect(listEvidenceRecords).toHaveBeenLastCalledWith(
                expect.objectContaining({ verdict: 'adjudication', since: '2026-09-01T00:00:00Z', root_task_id: 'run-9', offset: 0 }),
            ),
        );
        expect(getEvidenceSummary).toHaveBeenLastCalledWith(expect.objectContaining({ since: '2026-09-01T00:00:00Z' }));

        fireEvent.click(screen.getByTestId('ledger-next'));
        await waitFor(() => expect(listEvidenceRecords).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 })));
        expect(screen.getByText('Page 2 of 3')).toBeInTheDocument();
    });

    it('a verdict chip in the summary strip becomes a filter', async () => {
        render(<EvidenceLedgerPage {...props()} />);
        await screen.findByTestId('ledger-verdict-blocked');
        fireEvent.click(screen.getByTestId('ledger-verdict-blocked'));
        await waitFor(() => expect(listEvidenceRecords).toHaveBeenLastCalledWith(expect.objectContaining({ verdict: 'blocked' })));
    });

    it('opens the shared record dialog on a row, and the chain view from the chain shortcut', async () => {
        render(<EvidenceLedgerPage {...props()} />);
        fireEvent.click(await screen.findByTestId('record-row-rec-1'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-1');
        fireEvent.click(screen.getByText('close'));
        expect(screen.queryByTestId('record-dialog-stub')).toBeNull();

        expect(screen.getByTestId('record-chain-rec-2')).toHaveTextContent('confirms');
        expect(screen.getByTestId('record-chain-rec-0')).toHaveTextContent('root');
        fireEvent.click(screen.getByTestId('record-chain-rec-2'));
        expect(screen.getByTestId('chain-stub')).toHaveTextContent('rec-2');
        expect(screen.queryByTestId('record-dialog-stub')).toBeNull(); // the shortcut does not open the dialog
        fireEvent.click(screen.getByText('close chain'));
        expect(screen.queryByTestId('chain-stub')).toBeNull();
    });

    it('renders empty and error states', async () => {
        listEvidenceRecords.mockResolvedValueOnce({ ...RECORDS, items: [], total: 0 });
        const { unmount } = render(<EvidenceLedgerPage {...props()} />);
        expect(await screen.findByTestId('ledger-empty')).toBeInTheDocument();
        unmount();

        listEvidenceRecords.mockRejectedValueOnce(new Error('ledger down'));
        render(<EvidenceLedgerPage {...props()} />);
        expect(await screen.findByTestId('ledger-error')).toHaveTextContent('ledger down');
    });

    it('lists disclosures with selector, digest and withheld fields, and reloads after a disclosure', async () => {
        listEvidenceDisclosures.mockResolvedValue({
            items: [{ id: 'd1', tenant_id: 't', log_id: 'log-1', audience: 'auditor', selector: { root_task_id: 'run-1' }, completeness: 'complete', disclosed_record_ids: ['a', 'b'], disclosed_count: 2, suppressed_fields: ['prompt'], bundle_digest: 'abcdef0123456789abcdef', seq: 7, created_at: '2026-09-19T00:00:00Z' }],
            total: 1, limit: 20, offset: 0,
        });
        render(<EvidenceLedgerPage {...props()} />);
        const row = await screen.findByTestId('disclosure-d1');
        expect(row).toHaveTextContent('to auditor');
        expect(row).toHaveTextContent('2 records · complete · root_task_id');
        expect(row).toHaveTextContent('digest abcdef0123456789…');
        expect(row).toHaveTextContent('1 field(s) withheld');
        expect(row).toHaveTextContent('seq 7');
        fireEvent.click(screen.getByText('disclosed'));
        await waitFor(() => expect(listEvidenceDisclosures).toHaveBeenCalledTimes(2));
    });
});

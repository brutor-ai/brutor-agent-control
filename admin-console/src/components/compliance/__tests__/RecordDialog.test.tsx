/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The shared record dialog: record JSON, statement, log position, proof,
 * and Verify with findings grouped by severity — an error gates the
 * result, warnings and infos do not.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const getEvidenceRecord = vi.fn();
const getEvidenceRecordProof = vi.fn();
const verifyEvidenceRecord = vi.fn();

const gateway = { gatewayClient: { getEvidenceRecord, getEvidenceRecordProof, verifyEvidenceRecord } };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));

import RecordDialog from '../RecordDialog';

const DETAIL = {
    record_id: 'rec-1',
    log_id: 'log-1',
    seq: 7,
    verdict: 'blocked',
    kind: 'decide',
    surface: 'llm',
    kid: 'kid-abc',
    statement_b64: 'AAAA',
    record: { id: 'rec-1', verdict: 'blocked', action: { effect_type: 'llm.completion' } },
    log: { log_id: 'log-1', seq: 7, tree_size_now: 42 },
};

const PROOF = {
    record_id: 'rec-1', log_id: 'log-1', leaf_index: 6, tree_size: 42,
    root: 'r00t', leaf_hash: 'l34f', path: ['aaaa', 'bbbb'],
};

beforeEach(() => {
    vi.clearAllMocks();
    getEvidenceRecord.mockResolvedValue(DETAIL);
    getEvidenceRecordProof.mockResolvedValue(PROOF);
});

describe('RecordDialog', () => {
    it('shows the record, its log position, proof and statement', async () => {
        render(<RecordDialog recordId="rec-1" onClose={vi.fn()} />);
        await screen.findByTestId('record-dialog-json');
        expect(screen.getByTestId('record-dialog-verdict')).toHaveTextContent('blocked');
        expect(screen.getByTestId('record-dialog-position')).toHaveTextContent('42');
        expect(screen.getByTestId('record-dialog-position')).toHaveTextContent('kid-abc');
        await waitFor(() => expect(screen.getByTestId('record-dialog-proof')).toHaveTextContent('r00t'));
        expect(screen.getByTestId('record-dialog-proof')).toHaveTextContent('aaaa → bbbb');
        expect(screen.getByTestId('record-dialog-statement')).toHaveTextContent('AAAA');
        expect(screen.getByTestId('record-dialog-json')).toHaveTextContent('llm.completion');
    });

    it('reports a missing proof as a state, not a failure', async () => {
        getEvidenceRecordProof.mockRejectedValueOnce(new Error('not in tree yet'));
        render(<RecordDialog recordId="rec-1" onClose={vi.fn()} />);
        expect(await screen.findByTestId('record-dialog-proof-missing')).toHaveTextContent('not in tree yet');
    });

    it('verifies and groups findings by severity; an error gates the result', async () => {
        verifyEvidenceRecord.mockResolvedValueOnce({
            ok: false,
            record_id: 'rec-1',
            effect_mode: 'decide',
            statement: { verified: false, error: 'signature mismatch', kid: 'kid-abc' },
            findings: [
                { check: 'signature', code: 'bad_sig', severity: 'error', detail: 'The signature did not recompute.' },
                { check: 'semantic', code: 'stale_epoch', severity: 'warning', detail: 'Epoch older than the head.' },
                { check: 'semantic', code: 'note', severity: 'info', detail: 'Informational.' },
            ],
        });
        render(<RecordDialog recordId="rec-1" onClose={vi.fn()} />);
        await screen.findByTestId('record-dialog-json');
        fireEvent.click(screen.getByTestId('record-dialog-verify'));
        const result = await screen.findByTestId('record-dialog-verify-result');
        expect(result).toHaveTextContent('Recomputed: not consistent');
        expect(result).toHaveTextContent('not recomputed — signature mismatch');
        expect(within(screen.getByTestId('record-dialog-findings-error')).getByText('error · 1 — gates the result')).toBeInTheDocument();
        expect(within(screen.getByTestId('record-dialog-findings-warning')).getByText('warning · 1 — does not gate')).toBeInTheDocument();
        expect(screen.getByTestId('record-dialog-findings-info')).toBeInTheDocument();
        expect(verifyEvidenceRecord).toHaveBeenCalledWith('rec-1');
    });

    it('a consistent result reads "recomputed", never a banned word', async () => {
        verifyEvidenceRecord.mockResolvedValueOnce({
            ok: true, record_id: 'rec-1', effect_mode: 'decide',
            statement: { verified: true, kid: 'kid-abc' },
            findings: [{ check: 'semantic', code: 'x', severity: 'info', detail: 'fine' }],
        });
        render(<RecordDialog recordId="rec-1" onClose={vi.fn()} />);
        await screen.findByTestId('record-dialog-json');
        fireEvent.click(screen.getByTestId('record-dialog-verify'));
        const result = await screen.findByTestId('record-dialog-verify-result');
        expect(result).toHaveTextContent('Recomputed: consistent');
        expect(result).toHaveTextContent('recomputed under the producer key');
        expect(result.textContent).not.toMatch(/\bverified\b/i);
    });

    it('an unreachable verifier is reported, never treated as ok', async () => {
        verifyEvidenceRecord.mockRejectedValueOnce(new Error('core offline'));
        render(<RecordDialog recordId="rec-1" onClose={vi.fn()} />);
        await screen.findByTestId('record-dialog-json');
        fireEvent.click(screen.getByTestId('record-dialog-verify'));
        const result = await screen.findByTestId('record-dialog-verify-result');
        expect(result).toHaveTextContent('Verifier unreachable');
        expect(result).toHaveTextContent('core offline');
    });

    it('closes on the button and on Escape', async () => {
        const onClose = vi.fn();
        render(<RecordDialog recordId="rec-1" onClose={onClose} />);
        await screen.findByTestId('record-dialog-json');
        fireEvent.click(screen.getByTestId('record-dialog-close'));
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(onClose).toHaveBeenCalledTimes(2);
    });

    it('shows a load error', async () => {
        getEvidenceRecord.mockRejectedValueOnce(new Error('gone'));
        render(<RecordDialog recordId="rec-1" onClose={vi.fn()} />);
        expect(await screen.findByTestId('record-dialog-error')).toHaveTextContent('gone');
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Verify panel's three modes and three honest outcomes: consistent,
 * not consistent, and "the verifier could not run" (`verifier_unavailable`
 * or an unreachable control plane) — which is never shown as a result.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

const verifyEvidenceRecord = vi.fn();
const verifyEvidenceInline = vi.fn();
const verifyEvidenceBundle = vi.fn();

const gateway = { gatewayClient: { verifyEvidenceRecord, verifyEvidenceInline, verifyEvidenceBundle } };

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => gateway,
}));

import VerifyPanel from '../VerifyPanel';

beforeEach(() => {
    vi.clearAllMocks();
});

describe('VerifyPanel', () => {
    it('verifies a stored record by id and groups findings; an error gates the result', async () => {
        verifyEvidenceRecord.mockResolvedValueOnce({
            ok: false, record_id: 'rec-1', effect_mode: 'confirmed',
            statement: { verified: false, error: 'signature mismatch', kid: 'kid-1' },
            findings: [
                { check: 'signature', code: 'bad_signature', severity: 'error', detail: 'The statement does not sign this record.' },
                { check: 'semantic', code: 'confirm_after_plan', severity: 'warning', detail: 'confirm precedes plan' },
            ],
        });
        render(<VerifyPanel initialRecordId="rec-1" />);
        expect(screen.getByTestId<HTMLInputElement>('verify-record-id').value).toBe('rec-1');
        fireEvent.click(screen.getByTestId('verify-run'));
        const result = await screen.findByTestId('verify-result');
        expect(verifyEvidenceRecord).toHaveBeenCalledWith('rec-1');
        expect(result).toHaveAttribute('data-outcome', 'inconsistent');
        expect(result).toHaveTextContent('Recomputed: not consistent');
        expect(result).toHaveTextContent('not recomputed — signature mismatch · kid kid-1 · effect mode confirmed');
        expect(within(screen.getByTestId('verify-findings-error')).getByText('error · 1 — gates the result')).toBeInTheDocument();
        expect(within(screen.getByTestId('verify-findings-warning')).getByText('warning · 1 — does not gate')).toBeInTheDocument();
    });

    it('shows a consistent record in the recomputed vocabulary', async () => {
        verifyEvidenceRecord.mockResolvedValueOnce({ ok: true, record_id: 'rec-2', statement: { verified: true, kid: 'k' }, findings: [] });
        render(<VerifyPanel />);
        fireEvent.change(screen.getByTestId('verify-record-id'), { target: { value: ' rec-2 ' } });
        fireEvent.click(screen.getByTestId('verify-run'));
        const result = await screen.findByTestId('verify-result');
        expect(verifyEvidenceRecord).toHaveBeenCalledWith('rec-2');
        expect(result).toHaveAttribute('data-outcome', 'consistent');
        expect(result).toHaveTextContent('Recomputed: consistent');
        expect(result).toHaveTextContent('recomputed under the producer key');
        expect(result).toHaveTextContent('No findings.');
    });

    it('shows verifier_unavailable as a state of the tool, apart from any result', async () => {
        verifyEvidenceRecord.mockResolvedValueOnce({
            ok: false, record_id: null, statement: null,
            findings: [{ check: 'verifier', code: 'verifier_unavailable', severity: 'error', detail: 'core unreachable' }],
        });
        render(<VerifyPanel initialRecordId="rec-3" />);
        fireEvent.click(screen.getByTestId('verify-run'));
        const result = await screen.findByTestId('verify-result');
        expect(result).toHaveAttribute('data-outcome', 'unavailable');
        expect(result).toHaveTextContent('Verifier unavailable — nothing was checked');
        expect(result).not.toHaveTextContent('Recomputed');
        expect(result).toHaveTextContent('core unreachable');
    });

    it('shows an unreachable control plane the same way', async () => {
        verifyEvidenceRecord.mockRejectedValueOnce(new Error('502 from the gateway'));
        render(<VerifyPanel initialRecordId="rec-4" />);
        fireEvent.click(screen.getByTestId('verify-run'));
        const result = await screen.findByTestId('verify-result');
        expect(result).toHaveTextContent('Verifier unreachable — nothing was checked');
        expect(result).toHaveTextContent('502 from the gateway');
    });

    it('verifies an inline record with its statement, and refuses non-JSON', async () => {
        verifyEvidenceInline.mockResolvedValueOnce({ ok: true, record_id: 'x', statement: { verified: true }, findings: [] });
        render(<VerifyPanel />);
        fireEvent.click(screen.getByTestId('verify-mode-inline'));
        fireEvent.change(screen.getByTestId('verify-record-json'), { target: { value: '{not json' } });
        fireEvent.click(screen.getByTestId('verify-run'));
        expect(await screen.findByTestId('verify-input-error')).toHaveTextContent('Not valid JSON');
        expect(verifyEvidenceInline).not.toHaveBeenCalled();

        fireEvent.change(screen.getByTestId('verify-record-json'), { target: { value: '{"type": "ai.brutor/action-record/v1"}' } });
        fireEvent.change(screen.getByTestId('verify-statement-b64'), { target: { value: 'AAAA' } });
        fireEvent.click(screen.getByTestId('verify-run'));
        await screen.findByTestId('verify-result');
        expect(verifyEvidenceInline).toHaveBeenCalledWith({ type: 'ai.brutor/action-record/v1' }, 'AAAA');
    });

    it('verifies a bundle with its expected digest and reports a mismatch', async () => {
        verifyEvidenceBundle.mockResolvedValueOnce({
            ok: false, findings: [], bundle_digest: 'abc', digest_matches: false, records_verified: 3, proofs_verified: 3, heads_verified: 1, missing: ['rec-z'],
        });
        render(<VerifyPanel />);
        fireEvent.click(screen.getByTestId('verify-mode-bundle'));
        fireEvent.change(screen.getByTestId('verify-bundle-json'), { target: { value: '{"type": "ai.brutor/evidence-bundle/v1", "records": []}' } });
        fireEvent.change(screen.getByTestId('verify-bundle-digest'), { target: { value: 'ABC' } });
        fireEvent.click(screen.getByTestId('verify-run'));
        const result = await screen.findByTestId('verify-result');
        expect(verifyEvidenceBundle).toHaveBeenCalledWith({ type: 'ai.brutor/evidence-bundle/v1', records: [] }, 'ABC');
        expect(result).toHaveAttribute('data-outcome', 'inconsistent');
        expect(screen.getByTestId('verify-bundle-summary')).toHaveTextContent(
            'digest abc · does NOT match the expected digest · 3 records recomputed · 3 proofs · 1 heads · 1 referenced record(s) missing',
        );
    });

    it('says what it does not offer and links to the offline verifier', () => {
        render(<VerifyPanel />);
        expect(screen.getByTestId('verify-panel')).toHaveTextContent('Standalone tree-head and receipt checks are not offered by the control plane');
        expect(screen.getByTestId('verify-offline-link')).toHaveAttribute('href', 'https://docs.brutor.ai/tools/verify/');
        fireEvent.click(screen.getByTestId('verify-run'));
        expect(screen.getByTestId('verify-input-error')).toHaveTextContent('Enter a record id.');
    });
});

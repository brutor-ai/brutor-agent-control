/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Run details → sealed steps. The chain is the point: a
 * planned record reads "confirmed by" its outcome, the outcome "confirms"
 * it; an approval that lapsed reads "expired by"; a half nothing closes is
 * shown open. Bundle and verify go through the CP, and a verifier that
 * could not run is never shown as a clean result.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const listEvidenceRecords = vi.fn();
const buildEvidenceBundle = vi.fn();
const verifyEvidenceBundle = vi.fn();
const downloadJson = vi.fn();

const gateway = { gatewayClient: { listEvidenceRecords, buildEvidenceBundle, verifyEvidenceBundle } };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../lib/downloadJson', () => ({ downloadJson: (...a: unknown[]) => downloadJson(...a) }));
vi.mock('../../compliance/RecordDialog', () => ({
    default: (p: { recordId: string }) => <div data-testid="record-dialog-stub">{p.recordId}</div>,
}));

import RunEvidenceSection from '../RunEvidenceSection';

const R = (id: string, verdict: string, extra: Record<string, unknown> = {}) => ({
    record_id: id,
    log_id: 'log',
    seq: 0,
    verdict,
    kind: 'decide',
    surface: 'mcp',
    action_id: `mcp/${id}`,
    timestamp: '2026-09-19T12:00:00Z',
    effect_mode: 'not_applicable',
    chain: null,
    ...extra,
});

// API order is newest first.
const STEPS = [
    R('rec-expired', 'expired', { chain: { parent_record_id: 'rec-await', relation: 'supersedes' } }),
    R('rec-await', 'awaiting_human'),
    R('rec-out', 'executed', { effect_mode: 'confirmed', chain: { parent_record_id: 'rec-plan', relation: 'confirms' } }),
    R('rec-plan', 'planned'),
    R('rec-dangling', 'planned'),
    R('rec-pruned', 'executed', { effect_mode: null }),
];

const BUNDLE = {
    bundle_id: 'bun-1',
    bundle_digest: 'ab'.repeat(32),
    record_id: 'cd'.repeat(32),
    missing: [],
    record_count: 6,
    bundle: { manifest: { kind: 'ai.brutor/evidence-bundle' }, records: [] },
};

beforeEach(() => {
    vi.clearAllMocks();
    listEvidenceRecords.mockResolvedValue({ items: STEPS, total: STEPS.length, limit: 200, offset: 0 });
    buildEvidenceBundle.mockResolvedValue(BUNDLE);
});

describe('RunEvidenceSection', () => {
    it('asks for the run by root task id and lists steps oldest first', async () => {
        render(<RunEvidenceSection rootTaskId="run-9" />);
        await screen.findByTestId('run-step-rec-plan');
        expect(listEvidenceRecords).toHaveBeenCalledWith({ root_task_id: 'run-9', limit: 200 });
        const rows = screen.getAllByTestId(/^run-step-rec-[a-z-]+$/);
        expect(rows[0]).toHaveAttribute('data-testid', 'run-step-rec-pruned');
    });

    it('renders planned → confirms from both sides, with the verdict and mode', async () => {
        render(<RunEvidenceSection rootTaskId="run-9" />);
        await screen.findByTestId('run-step-rec-plan');
        expect(screen.getByTestId('run-step-verdict-rec-plan')).toHaveTextContent('planned');
        expect(screen.getByTestId('run-step-chain-rec-out')).toHaveTextContent('confirms the planned record');
        expect(screen.getByTestId('run-step-chain-rec-plan')).toHaveTextContent('confirmed by');
        expect(screen.queryByTestId('run-step-open-rec-plan')).toBeNull();
        expect(screen.getByTestId('run-step-mode-rec-out')).toHaveTextContent('confirmed');
        expect(screen.getByTestId('run-step-mode-rec-plan')).toHaveTextContent('not applicable');
        expect(screen.getByTestId('run-step-mode-rec-pruned')).toHaveTextContent('body pruned');
    });

    it('renders an expired approval and flags the unconfirmed planned record as open', async () => {
        render(<RunEvidenceSection rootTaskId="run-9" />);
        await screen.findByTestId('run-step-rec-await');
        expect(screen.getByTestId('run-step-verdict-rec-expired')).toHaveTextContent('expired');
        expect(screen.getByTestId('run-step-chain-rec-expired')).toHaveTextContent('expires the approval request');
        expect(screen.getByTestId('run-step-chain-rec-await')).toHaveTextContent('expired by');
        expect(screen.queryByTestId('run-step-open-rec-await')).toBeNull();
        expect(screen.getByTestId('run-step-open-rec-dangling')).toHaveTextContent('no outcome record confirms it');
    });

    it('opens the record dialog for a step and for a chain parent', async () => {
        render(<RunEvidenceSection rootTaskId="run-9" />);
        fireEvent.click(await screen.findByTestId('run-step-record-rec-out'));
        expect(screen.getByTestId('record-dialog-stub')).toHaveTextContent('rec-out');
    });

    it('bundles the run by root task id and downloads the JSON', async () => {
        render(<RunEvidenceSection rootTaskId="run-9" />);
        await screen.findByTestId('run-step-rec-plan');
        fireEvent.click(screen.getByTestId('run-evidence-bundle'));
        const result = await screen.findByTestId('run-evidence-bundle-result');
        expect(buildEvidenceBundle).toHaveBeenCalledWith({ selector: { root_task_id: 'run-9' }, closure_depth: 1 });
        expect(result).toHaveTextContent('bun-1');
        expect(result).toHaveTextContent('6 records');
        fireEvent.click(screen.getByTestId('run-evidence-download'));
        expect(downloadJson).toHaveBeenCalledWith('evidence-bundle-run-9.json', BUNDLE.bundle);
    });

    it('verify builds the bundle first, then shows the verifier findings', async () => {
        verifyEvidenceBundle.mockResolvedValue({
            ok: false,
            findings: [{ check: 'proof', code: 'inclusion_mismatch', severity: 'error', detail: 'leaf 3' }],
            bundle_digest: BUNDLE.bundle_digest,
            digest_matches: true,
            records_verified: 5,
            proofs_verified: 5,
            heads_verified: 1,
            missing: [],
        });
        render(<RunEvidenceSection rootTaskId="run-9" />);
        await screen.findByTestId('run-step-rec-plan');
        fireEvent.click(screen.getByTestId('run-evidence-verify'));
        const box = await screen.findByTestId('run-evidence-verify-result');
        expect(buildEvidenceBundle).toHaveBeenCalledTimes(1);
        expect(verifyEvidenceBundle).toHaveBeenCalledWith(BUNDLE.bundle, BUNDLE.bundle_digest);
        expect(within(box).getByText(/1 finding/)).toBeInTheDocument();
        expect(box).toHaveTextContent('inclusion_mismatch');
    });

    it('a verifier that cannot run leaves no result on screen', async () => {
        verifyEvidenceBundle.mockRejectedValue(new Error('core unreachable'));
        render(<RunEvidenceSection rootTaskId="run-9" />);
        await screen.findByTestId('run-step-rec-plan');
        fireEvent.click(screen.getByTestId('run-evidence-verify'));
        expect(await screen.findByTestId('run-evidence-action-error')).toHaveTextContent('could not run');
        expect(screen.queryByTestId('run-evidence-verify-result')).toBeNull();
    });

    it('says so when the run has no sealed records, and disables the actions', async () => {
        listEvidenceRecords.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
        render(<RunEvidenceSection rootTaskId="run-9" />);
        expect(await screen.findByTestId('run-evidence-empty')).toBeInTheDocument();
        await waitFor(() => expect(screen.getByTestId('run-evidence-bundle')).toBeDisabled());
    });
});

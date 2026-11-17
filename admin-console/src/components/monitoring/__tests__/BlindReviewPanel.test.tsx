/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Blind review queue. The load-bearing property: while a
 * review is pending the judge's result is never on screen — not even when
 * the server (wrongly) sends it. It appears only after the human submits,
 * next to the sealed adjudication record id.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const listCheckReviews = vi.fn();
const gradeCheckReview = vi.fn();
const drawCheckReviewSample = vi.fn();

// One stable client object, as the real hook provides — a fresh object per
// render would change the loader identity and reload forever.
const gateway = { gatewayClient: { listCheckReviews, gradeCheckReview, drawCheckReviewSample } };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import BlindReviewPanel from '../BlindReviewPanel';

const AGREEMENT = {
    check_id: 'chk-1',
    evaluator_fingerprint: 'fp'.repeat(32),
    n: 34,
    agreements: 31,
    observed_agreement: 0.92,
    kappa: 0.81,
    ci95: [0.83, 0.97],
    mae: null,
    numeric_n: 0,
    blind: true,
    insufficient_sample: false,
    below_threshold: false,
    threshold: 0.8,
    window_days: 30,
    label: 'human agreement 0.92 [0.83–0.97], n = 34, blind',
};

// A leaky server: judged fields present on a pending row.
const LEAKY_PENDING = {
    id: 'rev-1',
    tenant_id: 't',
    check_id: 'chk-1',
    evaluator_fingerprint: 'fp'.repeat(32),
    week: '2026-09-07',
    sample_key: 'run-SAMPLE-A',
    assessment_record_id: 'ab'.repeat(32),
    judged_result: 'not_met',
    judged_score: 0.13,
    status: 'pending',
    blind: true,
};

beforeEach(() => {
    vi.clearAllMocks();
    listCheckReviews.mockResolvedValue({ items: [LEAKY_PENDING], total: 1, counts: { pending: 1, graded: 34, skipped: 0 }, agreement: AGREEMENT });
});

describe('BlindReviewPanel', () => {
    it('asks for pending reviews only', async () => {
        render(<BlindReviewPanel checkId="chk-1" />);
        await screen.findByTestId('review-row-rev-1');
        expect(listCheckReviews).toHaveBeenCalledWith('chk-1', { status: 'pending' });
    });

    it('never shows the judged result or score of a pending review', async () => {
        render(<BlindReviewPanel checkId="chk-1" />);
        const row = await screen.findByTestId('review-row-rev-1');
        expect(row).toHaveTextContent('withheld until you grade');
        const panel = screen.getByTestId('blind-review-panel');
        expect(panel.textContent).not.toMatch(/not_met|0\.13/);
        expect(panel.textContent).not.toMatch(/judge:/);
    });

    it('shows the agreement panel with κ, the Wilson interval and n', async () => {
        render(<BlindReviewPanel checkId="chk-1" />);
        await screen.findByTestId('review-agreement');
        expect(screen.getByTestId('agreement-observed')).toHaveTextContent('0.92');
        expect(screen.getByTestId('agreement-ci')).toHaveTextContent('0.83–0.97');
        expect(screen.getByTestId('agreement-kappa')).toHaveTextContent('0.81');
        expect(screen.getByTestId('agreement-n')).toHaveTextContent('n = 34, blind');
        expect(screen.queryByTestId('agreement-insufficient')).toBeNull();
    });

    it('says insufficient sample below n = 10, and threshold breaches separately', async () => {
        listCheckReviews.mockResolvedValue({
            items: [],
            total: 0,
            counts: { pending: 0, graded: 4 },
            agreement: { ...AGREEMENT, n: 4, observed_agreement: 0.5, insufficient_sample: true, below_threshold: true },
        });
        render(<BlindReviewPanel checkId="chk-1" />);
        expect(await screen.findByTestId('agreement-insufficient')).toBeInTheDocument();
        expect(screen.queryByTestId('agreement-below')).toBeNull();
        expect(screen.getByTestId('review-empty')).toBeInTheDocument();
    });

    it('submit is disabled until a grade is chosen, then posts it and shows the sealed adjudication id', async () => {
        gradeCheckReview.mockResolvedValue({
            review: { ...LEAKY_PENDING, status: 'graded', human_result: 'met', judged_result: 'not_met' },
            agrees_with_judge: false,
            seal: { sealed: true, record_id: 'cd'.repeat(32) },
            agreement: { ...AGREEMENT, n: 35, observed_agreement: 0.9 },
        });
        render(<BlindReviewPanel checkId="chk-1" />);
        await screen.findByTestId('review-row-rev-1');
        expect(screen.getByTestId('review-submit-rev-1')).toBeDisabled();
        fireEvent.click(screen.getByTestId('review-grade-rev-1-met'));
        fireEvent.change(screen.getByTestId('review-score-rev-1'), { target: { value: '0.9' } });
        fireEvent.click(screen.getByTestId('review-submit-rev-1'));

        await waitFor(() => expect(gradeCheckReview).toHaveBeenCalledWith('chk-1', 'rev-1', { human_result: 'met', human_score: 0.9 }));
        expect(await screen.findByTestId('review-sealed-rev-1')).toHaveTextContent('cd'.repeat(32));
        // Revealed only now, beside the human's grade.
        expect(screen.getByTestId('review-graded-rev-1')).toHaveTextContent('judge: not_met');
        expect(screen.getByTestId('review-graded-rev-1')).toHaveTextContent('disagrees');
        expect(screen.queryByTestId('review-row-rev-1')).toBeNull();
        expect(screen.getByTestId('agreement-observed')).toHaveTextContent('0.90');
        expect(screen.getByTestId('review-counts')).toHaveTextContent('0 pending · 35 graded');
    });

    it('an out-of-range score is refused locally', async () => {
        render(<BlindReviewPanel checkId="chk-1" />);
        await screen.findByTestId('review-row-rev-1');
        fireEvent.click(screen.getByTestId('review-grade-rev-1-n/a'));
        fireEvent.change(screen.getByTestId('review-score-rev-1'), { target: { value: '7' } });
        fireEvent.click(screen.getByTestId('review-submit-rev-1'));
        expect(await screen.findByTestId('review-row-error-rev-1')).toHaveTextContent('between 0 and 1');
        expect(gradeCheckReview).not.toHaveBeenCalled();
    });

    it('a grade saved but not sealed says so', async () => {
        gradeCheckReview.mockResolvedValue({
            review: { ...LEAKY_PENDING, status: 'graded', human_result: 'not_met' },
            agrees_with_judge: true,
            seal: { sealed: false, seal_error: 'evidence_disabled' },
            agreement: AGREEMENT,
        });
        render(<BlindReviewPanel checkId="chk-1" />);
        await screen.findByTestId('review-row-rev-1');
        fireEvent.click(screen.getByTestId('review-grade-rev-1-not_met'));
        fireEvent.click(screen.getByTestId('review-submit-rev-1'));
        expect(await screen.findByTestId('review-unsealed-rev-1')).toHaveTextContent('evidence_disabled');
    });

    it('a failed submit keeps the row and shows the error', async () => {
        gradeCheckReview.mockRejectedValue(new Error('already graded'));
        render(<BlindReviewPanel checkId="chk-1" />);
        await screen.findByTestId('review-row-rev-1');
        fireEvent.click(screen.getByTestId('review-grade-rev-1-met'));
        fireEvent.click(screen.getByTestId('review-submit-rev-1'));
        expect(await screen.findByTestId('review-row-error-rev-1')).toHaveTextContent('already graded');
        expect(screen.getByTestId('review-row-rev-1')).toBeInTheDocument();
    });

    it('draws a sample for the chosen week and reloads the queue', async () => {
        drawCheckReviewSample.mockResolvedValue({ check_id: 'chk-1', week: '2026-09-07', created: 6, items: [] });
        render(<BlindReviewPanel checkId="chk-1" />);
        await screen.findByTestId('review-row-rev-1');
        fireEvent.change(screen.getByTestId('review-sample-week'), { target: { value: '2026-09-09' } });
        fireEvent.click(screen.getByTestId('review-draw-sample'));
        const weekStart = '2026-09-07';
        expect(await screen.findByTestId('review-sample-note')).toHaveTextContent(`6 verdicts sampled for the week of ${weekStart}`);
        expect(drawCheckReviewSample).toHaveBeenCalledWith('chk-1', '2026-09-09');
        await waitFor(() => expect(listCheckReviews).toHaveBeenCalledTimes(2));
    });
});

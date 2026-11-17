/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Zero-fill honesty: quiet buckets become explicit zeros for counts
 * (silence must be visible on the chart), while rates and per-run costs
 * stay null (a gap, not an invented 0).
 */

import { describe, expect, it } from 'vitest';

import { chartGaps, plottedCost, zeroFillTimeseries } from '../runTimeseries';
import type { RunTimeseriesPoint, RunTimeseriesResponse } from '../aiSystemTypes';

function resp(overrides: Partial<RunTimeseriesResponse> = {}): RunTimeseriesResponse {
    return {
        ai_system_id: 'sys-1',
        period_hours: 24,
        bucket: 'hour',
        window_start: '2026-07-27T00:00:00+00:00',
        window_end: '2026-07-27T04:00:00+00:00',
        points: [
            {
                bucket_start: '2026-07-27T02:00:00Z',
                runs: 3, success: 3, no_work: 0, deviation: 0, failed: 0, not_decisive: 0,
                cost_usd: 0.3, processed_cost_usd: 0.3,
                cost_per_processed_outcome: 0.1,
                tool_calls: 5, tool_errors: 0, tool_error_rate: 0,
                avg_actions: 4,
            },
        ],
        ...overrides,
    };
}

describe('zeroFillTimeseries', () => {
    it('fills quiet hours with zero counts and null rates', () => {
        const filled = zeroFillTimeseries(resp());
        expect(filled).toHaveLength(4);
        expect(filled.map((p) => p.bucket_start)).toEqual([
            '2026-07-27T00:00:00Z',
            '2026-07-27T01:00:00Z',
            '2026-07-27T02:00:00Z',
            '2026-07-27T03:00:00Z',
        ]);
        const quiet = filled[0];
        expect(quiet.runs).toBe(0);
        // Every run class is an explicit zero in a quiet hour.
        expect([quiet.success, quiet.no_work, quiet.deviation, quiet.failed, quiet.not_decisive]).toEqual([0, 0, 0, 0, 0]);
        expect(quiet.processed_cost_usd).toBe(0);
        // Nothing was processed, so there IS no cost per processed outcome —
        // null renders as a gap, never as a zero-dollar data point.
        expect(quiet.cost_per_processed_outcome).toBeNull();
        expect(quiet.tool_error_rate).toBeNull();
        expect(quiet.avg_actions).toBeNull();
        // The real bucket passes through untouched.
        expect(filled[2].runs).toBe(3);
        expect(filled[2].cost_per_processed_outcome).toBe(0.1);
    });

    it('daily buckets fill by calendar day', () => {
        const filled = zeroFillTimeseries(resp({
            bucket: 'day',
            window_start: '2026-07-25T12:00:00+00:00',
            window_end: '2026-07-28T12:00:00+00:00',
            points: [],
        }));
        expect(filled.map((p) => p.bucket_start)).toEqual([
            '2026-07-25T00:00:00Z',
            '2026-07-26T00:00:00Z',
            '2026-07-27T00:00:00Z',
        ]);
    });

    it('all-time responses pass through unfilled — no known span to fill', () => {
        const r = resp({ window_start: null, window_end: null });
        expect(zeroFillTimeseries(r)).toBe(r.points);
    });
});

/** One hour of the Brutor Demo System (hand-built). `polls`
 *  are empty polls (no-work, $0); `processed` are screenings that ended in a
 *  declared outcome counting toward the baseline; the rest failed. */
function hour(
    h: number,
    runs: number,
    processed: number,
    processedCost: number,
    modelRate: number | null,
    polls = 0,
): RunTimeseriesPoint {
    return {
        bucket_start: `2026-10-25T${String(h).padStart(2, '0')}:00:00Z`,
        runs, success: processed, no_work: polls, deviation: 0, failed: runs - processed - polls, not_decisive: 0,
        cost_usd: processedCost, processed_cost_usd: processedCost,
        cost_per_processed_outcome: processed ? processedCost / processed : null,
        tool_calls: runs ? 4 : 0, tool_errors: 0, tool_error_rate: runs ? 0 : null,
        model_calls: runs, model_errors: 0, model_error_rate: modelRate,
        // Run length is over the work only: an hour of polls has none.
        avg_actions: processed ? 10 : null,
    };
}

// 16:00 normal; 17:00 an empty $0 poll (no work) plus an errored screening;
// 18:00 no runs (the agent backing off); 19:00 like 17:00; 20:00 normal again.
const OUTAGE = [
    hour(16, 17, 15, 0.18, 0.1),
    hour(17, 2, 0, 0, 1, 1),
    hour(18, 0, 0, 0, null),
    hour(19, 2, 0, 0, 1, 1),
    hour(20, 29, 27, 0.53, 0.03),
];

describe('plottedCost', () => {
    it('follows cost per processed outcome', () => {
        expect(plottedCost(OUTAGE[0])).toBeCloseTo(0.012);
    });

    it('is a gap, not $0, when the hour processed nothing (only an empty poll and a failure)', () => {
        expect(plottedCost(OUTAGE[1])).toBeNull();
    });

    it('is a gap, not $0, when every processed outcome cost nothing', () => {
        // Eight approved decisions applied at $0.
        const free = { ...hour(8, 8, 8, 0, null), cost_per_processed_outcome: 0 };
        expect(plottedCost(free)).toBeNull();
    });

    it('ignores what no-work runs cost — they are overhead, never in the line', () => {
        const paidPoll = { ...hour(9, 3, 1, 0.02, null, 2), cost_usd: 0.05 };
        expect(plottedCost(paidPoll)).toBeCloseTo(0.02);
    });
});

describe('chartGaps', () => {
    it('on run length, says the outage hours processed nothing, and the quiet hour had no runs', () => {
        expect(chartGaps(OUTAGE, 'actions')).toEqual([
            { x1: '2026-10-25T16:00:00Z', x2: '2026-10-25T20:00:00Z', label: 'No processed outcomes · No runs' },
        ]);
    });

    it('on the cost chart, folds the hours without processed outcomes into the gap and says so', () => {
        expect(chartGaps(OUTAGE, 'cost')).toEqual([
            { x1: '2026-10-25T16:00:00Z', x2: '2026-10-25T20:00:00Z', label: 'No processed outcomes · No runs' },
        ]);
    });

    it('says "No paid outcomes" for an hour whose processed outcomes all cost $0', () => {
        const free = hour(17, 8, 8, 0, 0);
        expect(chartGaps([OUTAGE[0], free, OUTAGE[4]], 'cost')).toEqual([
            { x1: '2026-10-25T16:00:00Z', x2: '2026-10-25T20:00:00Z', label: 'No paid outcomes' },
        ]);
        // ...while the run-length line is drawn there: the work happened.
        expect(chartGaps([OUTAGE[0], free, OUTAGE[4]], 'actions')).toEqual([]);
    });

    it('names each reason once across a mixed gap', () => {
        const free = hour(17, 8, 8, 0, 0);
        const label = chartGaps([OUTAGE[0], free, OUTAGE[1], OUTAGE[2], OUTAGE[4]], 'cost')[0].label;
        expect(label).toBe('No paid outcomes · No processed outcomes · No runs');
    });

    it('marks a gap at the edge of the window from its own edge', () => {
        const pts = [hour(0, 0, 0, 0, null), hour(1, 3, 3, 0.3, 0)];
        expect(chartGaps(pts, 'actions')).toEqual([
            { x1: '2026-10-25T00:00:00Z', x2: '2026-10-25T01:00:00Z', label: 'No runs' },
        ]);
    });

    it('draws no gap over a window with nothing in it — that is the empty chart', () => {
        expect(chartGaps([hour(0, 0, 0, 0, null), hour(1, 0, 0, 0, null)], 'actions')).toEqual([]);
    });

    it('says "No calls" for hours with runs but no model or tool call to fail', () => {
        const quiet = { ...hour(17, 2, 2, 0, null), tool_error_rate: null };
        expect(chartGaps([OUTAGE[0], quiet, OUTAGE[4]], 'errors')[0].label).toBe('No calls');
    });

    it('keeps the error line drawn through the outage — the model calls failed there', () => {
        expect(chartGaps(OUTAGE, 'errors')).toEqual([
            { x1: '2026-10-25T17:00:00Z', x2: '2026-10-25T19:00:00Z', label: 'No runs' },
        ]);
    });
});

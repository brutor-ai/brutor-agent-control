/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import type {
    RunTimeseriesPoint,
    RunTimeseriesResponse,
} from './aiSystemTypes';

/** Zero-fill the sparse server response across its window. Counts become
 *  0 in quiet buckets; rate/per-run fields stay null (a gap, not a 0). */
export function zeroFillTimeseries(resp: RunTimeseriesResponse): RunTimeseriesPoint[] {
    if (!resp.window_start) return resp.points; // all-time: no known span to fill
    const byKey = new Map(resp.points.map((p) => [p.bucket_start, p]));
    const stepMs = resp.bucket === 'hour' ? 3_600_000 : 86_400_000;
    const start = new Date(resp.window_start).getTime();
    const end = resp.window_end ? new Date(resp.window_end).getTime() : Date.now();

    const filled: RunTimeseriesPoint[] = [];
    for (let t = start; t < end; t += stepMs) {
        const d = new Date(t);
        const key =
            resp.bucket === 'hour'
                ? `${d.toISOString().slice(0, 10)}T${String(d.getUTCHours()).padStart(2, '0')}:00:00Z`
                : `${d.toISOString().slice(0, 10)}T00:00:00Z`;
        filled.push(
            byKey.get(key) ?? {
                bucket_start: key,
                runs: 0,
                success: 0,
                no_work: 0,
                deviation: 0,
                failed: 0,
                not_decisive: 0,
                cost_usd: 0,
                processed_cost_usd: 0,
                cost_per_processed_outcome: null,
                tool_calls: 0,
                tool_errors: 0,
                tool_error_rate: null,
                model_calls: 0,
                model_errors: 0,
                model_error_rate: null,
                avg_actions: null,
            },
        );
    }
    // Daily buckets can duplicate keys across the loop boundary; dedupe.
    return filled.filter((p, i) => i === 0 || p.bucket_start !== filled[i - 1].bucket_start);
}


/** The trend charts drawn as lines (the outcome bars need none of this). */
export type TrendLineKind = 'cost' | 'actions' | 'errors';

/** Why a stretch of a trend line is missing — shown on the shading. */
export const GAP_LABELS = {
    noRuns: 'No runs',
    // Runs, but none that processed an outcome counting toward the baseline
    // (only empty polls, failures or deviations): there is no work to
    // average the cost or length of. Empty polls are never averaged in.
    noProcessed: 'No processed outcomes',
    // Processed outcomes that all cost $0 (an approved decision applied):
    // plotting $0 read as a free hour.
    noPaid: 'No paid outcomes',
    noCalls: 'No calls',
} as const;

/** Cost per processed outcome as plotted: `null` (a gap) when no processed
 *  outcome in the bucket cost anything, not $0. */
export function plottedCost(p: RunTimeseriesPoint): number | null {
    return p.processed_cost_usd > 0 ? (p.cost_per_processed_outcome ?? null) : null;
}

function plotted(p: RunTimeseriesPoint, kind: TrendLineKind): boolean {
    if (kind === 'cost') return plottedCost(p) !== null;
    if (kind === 'actions') return p.avg_actions !== null && p.avg_actions !== undefined;
    return p.model_error_rate !== null || p.tool_error_rate !== null;
}

/** Why one bucket with runs has nothing on the line. */
function runsGapReason(p: RunTimeseriesPoint, kind: TrendLineKind): string {
    if (kind === 'errors') return GAP_LABELS.noCalls;
    if (!p.success) return GAP_LABELS.noProcessed;
    return GAP_LABELS.noPaid;
}

export interface ChartGap {
    /** The drawn points either side of the gap (or the gap's own edge at
     *  the ends of the window), so the shading covers the blank stretch. */
    x1: string;
    x2: string;
    label: string;
}

/**
 * Every stretch where the line is not drawn, labelled with why: a blank
 * hour otherwise reads as a broken chart (an agent that backs off during a
 * provider outage makes no runs for an hour).
 */
export function chartGaps(points: RunTimeseriesPoint[], kind: TrendLineKind): ChartGap[] {
    const gaps: ChartGap[] = [];
    let i = 0;
    while (i < points.length) {
        if (plotted(points[i], kind)) { i += 1; continue; }
        let j = i;
        while (j + 1 < points.length && !plotted(points[j + 1], kind)) j += 1;
        // A gap that covers the whole window is the empty chart, not a gap.
        if (i === 0 && j === points.length - 1) break;
        const span = points.slice(i, j + 1);
        const reasons: string[] = [];
        for (const p of span) {
            if (p.runs === 0) continue;
            const reason = runsGapReason(p, kind);
            if (!reasons.includes(reason)) reasons.push(reason);
        }
        if (span.some((p) => p.runs === 0)) reasons.push(GAP_LABELS.noRuns);
        gaps.push({
            x1: points[i > 0 ? i - 1 : i].bucket_start,
            x2: points[j < points.length - 1 ? j + 1 : j].bucket_start,
            label: reasons.join(' · '),
        });
        i = j + 1;
    }
    return gaps;
}

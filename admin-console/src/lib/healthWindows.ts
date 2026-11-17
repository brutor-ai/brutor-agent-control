/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the health verdict is computed over, in words — the one place the
 * strip, the Signals overview and their tooltips read it from.
 *
 * Health has two windows and the page has a third, and the page once
 * showed "Healthy 90%" three hours into an outage because nothing said so:
 *
 *  - the **health window** (30 days, fixed) — every signal's score;
 *  - the **latest finished runs** (3) — the "failing now" check, judged by
 *    the control plane's one down rule;
 *  - the **range selector** — only the Signals sub-tabs' tiles and charts.
 *
 * Pure, so the sentences can be pinned without rendering.
 */

import type { RecentRunsVerdict, SystemHealth } from './aiSystemTypes';

/** The control plane's defaults, used where an answer carries no
 *  `windows` (the contract makes it optional). */
const DEFAULT_PERIOD_HOURS = 720;
const DEFAULT_RECENT_RUNS = 3;
const DEFAULT_FRESHNESS_SECONDS = 300;

/** "30-day" for 720 hours, "36-hour" for 36. */
export function windowSpan(hours?: number | null): string {
    const h = hours ?? DEFAULT_PERIOD_HOURS;
    return h % 24 === 0 ? `${h / 24}-day` : `${h}-hour`;
}

function hhmm(iso?: string | null): string | null {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** One sentence on the latest finished runs, or null when there is nothing
 *  worth saying (no runs judged). */
export function recentRunsSentence(recent?: RecentRunsVerdict | null): string | null {
    if (!recent || !recent.considered) return null;
    const at = hhmm(recent.latest[0]?.ended_at);
    const latest = at ? ` (latest ${at})` : '';
    const n = recent.considered;
    if (recent.failed === n) {
        return n === 1 ? `the latest finished run failed${latest}.` : `the last ${n} finished runs all failed${latest}.`;
    }
    if (recent.failed > 0) return `${recent.failed} of the last ${n} finished runs failed${latest}.`;
    return n === 1 ? `the latest finished run succeeded${latest}.` : `the last ${n} finished runs all succeeded${latest}.`;
}

/** The short qualifier next to the score on the strip: what the number is
 *  over. A failing system is judged on its latest runs, not the window. */
export function healthBasisTag(health?: SystemHealth | null): string {
    if (health?.health_state === 'failing') {
        const n = health.recent_runs?.considered || health.windows?.recent_runs || DEFAULT_RECENT_RUNS;
        return `last ${n} runs failed`;
    }
    return windowSpan(health?.windows?.health_period_hours);
}

/** How the verdict is calculated, as short lines — the strip tooltip and
 *  the Overview's "How health is calculated" section. */
export function healthExplainer(health?: SystemHealth | null): string[] {
    const span = windowSpan(health?.windows?.health_period_hours);
    const n = health?.windows?.recent_runs ?? DEFAULT_RECENT_RUNS;
    const fresh = Math.round((health?.windows?.freshness_seconds ?? DEFAULT_FRESHNESS_SECONDS) / 60);
    const lines = [
        'The score is the worst enforced signal, not an average: one failing signal is enough.',
        `Each signal is graded over the last ${span.replace('-', ' ')}s. This window is fixed — the range buttons on this page do not change it; they only change the Signals tiles and charts.`,
        `The latest ${n} finished runs are also checked on their own: all ${n} failed means Failing; 1–${n - 1} failed caps Reliability at 50% (Degraded) until ${n} in a row succeed. ` +
            'Only runs that are evidence of the work count: a run that ended in a declared outcome not counting toward the baseline (an empty poll) is skipped, a run with an undeclared or no outcome counts as failed.',
        `The verdict is stored and recomputed when it is older than ${fresh} minutes; Refresh recomputes it now.`,
    ];
    if (health?.signal_basis === 'segments') {
        lines[2] = 'This is a shared service: it is judged on the calls it served for others, and its liveness applies the same 3-in-a-row rule to them.';
    }
    return lines;
}

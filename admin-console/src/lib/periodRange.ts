/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A selected period resolves to EITHER a rolling window (period_hours;
 * 0 = all time) OR an explicit calendar range (start/end ISO — overrides
 * period_hours server-side, same precedence as the backend's
 * resolve_window). FinOps lives in calendar months: "30 days" is not
 * "March", so MTD / Last month / custom ranges are first-class.
 *
 * Encodings for `selectedPeriod` (persisted): the rolling presets
 * ('24h'…'all'), 'mtd', 'lastm', and 'custom:YYYY-MM-DD:YYYY-MM-DD'
 * (end date inclusive in the UI, converted to an exclusive bound here).
 */
export interface PeriodRange {
    period_hours: number;
    start?: string;
    end?: string;
}

export const periodToRange = (period: string, now: Date = new Date()): PeriodRange => {
    if (period === 'mtd') {
        const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
        return { period_hours: 0, start: start.toISOString() };
    }
    if (period === 'lastm') {
        const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
        const end = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1));
        return { period_hours: 0, start: start.toISOString(), end: end.toISOString() };
    }
    if (period.startsWith('custom:')) {
        const [, s, e] = period.split(':');
        if (s && e) {
            const start = new Date(`${s}T00:00:00Z`);
            // UI end date is inclusive; the API bound is exclusive.
            const end = new Date(`${e}T00:00:00Z`);
            end.setUTCDate(end.getUTCDate() + 1);
            if (!Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime())) {
                return { period_hours: 0, start: start.toISOString(), end: end.toISOString() };
            }
        }
        return { period_hours: 24 };
    }
    return { period_hours: periodToHours(period) };
};

/** Approximate hour-length of a period — feeds heuristics (chart interval,
 *  heatmap days, unattributed-runs window) that only need the magnitude. */
export const periodToHours = (period: string): number => {
    switch (period) {
        case '24h': return 24;
        case '7d': return 168;
        case '30d': return 720;
        case '90d': return 2160;
        case 'all': return 0; // 0 = all time (backend omits the lower bound)
        default: break;
    }
    const r = periodToRange(period);
    if (r.start) {
        const end = r.end ? new Date(r.end).getTime() : Date.now();
        return Math.max(1, Math.round((end - new Date(r.start).getTime()) / 3_600_000));
    }
    return 24;
};

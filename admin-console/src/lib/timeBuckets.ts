/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The ONE time-grain rule for daily series rendered as charts:
 *
 *   ≤ 30 days   → daily bars
 *   ≤ ~3 months → weekly (Monday-keyed)
 *   beyond      → monthly
 *
 * Ninety daily slivers are unreadable; the rollup happens client-side so
 * every backend series can stay honestly daily (and the rule lives in one
 * language, once). Shared by RG → Usage and Mission Control's cost
 * composition — if the thresholds ever change, they change everywhere.
 */

export type Grain = 'Daily' | 'Weekly' | 'Monthly';

export const WEEKLY_THRESHOLD_DAYS = 30;
export const MONTHLY_THRESHOLD_DAYS = 92;

export function grainFor(dayCount: number): Grain {
    if (dayCount <= WEEKLY_THRESHOLD_DAYS) return 'Daily';
    if (dayCount <= MONTHLY_THRESHOLD_DAYS) return 'Weekly';
    return 'Monthly';
}

/** The bucket key a YYYY-MM-DD day rolls into for a grain. */
export function bucketKey(dateStr: string, grain: Grain): string {
    if (grain === 'Daily') return dateStr;
    if (grain === 'Monthly') return dateStr.slice(0, 7); // YYYY-MM
    const d = new Date(dateStr + 'T00:00:00Z');
    if (Number.isNaN(d.getTime())) return dateStr;
    const dow = (d.getUTCDay() + 6) % 7; // Monday = 0
    d.setUTCDate(d.getUTCDate() - dow);
    return d.toISOString().slice(0, 10); // the week's Monday
}

/**
 * Roll daily rows up to the adaptive grain, summing every numeric field.
 * Non-numeric fields keep the first-seen value. Rows must carry a `date`
 * (YYYY-MM-DD); output is sorted by bucket key.
 */
export function bucketDaily<T extends { date: string }>(
    rows: T[],
): { grain: Grain; rows: T[] } {
    const grain = grainFor(rows.length);
    if (grain === 'Daily') return { grain, rows };

    const buckets = new Map<string, Record<string, unknown>>();
    for (const r of rows) {
        const k = bucketKey(r.date, grain);
        const b = buckets.get(k);
        if (!b) {
            buckets.set(k, { ...r, date: k });
            continue;
        }
        for (const [field, value] of Object.entries(r)) {
            if (field === 'date') continue;
            if (typeof value === 'number') {
                b[field] = ((b[field] as number) ?? 0) + value;
            }
        }
    }
    return {
        grain,
        rows: [...buckets.values()].sort((a, b) =>
            String(a.date).localeCompare(String(b.date))
        ) as T[],
    };
}

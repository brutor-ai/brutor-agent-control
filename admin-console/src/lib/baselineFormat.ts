/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * How a learned baseline is written: one formatter for the Assurance tab and
 * for the printable report, so the PDF a person hands over says what the
 * screen said. (The printable once printed the raw floats, "0.022198625 ±
 * 0.002228000000000001", where the tab read "$0.0222 ± $0.0022".)
 */

/** The fields of a baseline row the formatter reads. */
export interface BaselineValueFields {
    metric_key: string;
    median?: number | null;
    mad?: number | null;
    p90?: number | null;
}

/** Median ± MAD, p90 for a numeric baseline; cost metrics are dollars. */
export function baselineValue(row: BaselineValueFields): string {
    if (row.median == null) return '—';
    const isCost = row.metric_key.startsWith('cost');
    const fmt = (n: number) => (isCost ? `$${n.toFixed(4)}` : `${Number(n.toFixed(2))}`);
    const spread = row.mad != null ? ` ± ${fmt(row.mad)}` : '';
    const p90 = row.p90 != null ? `, p90 ${fmt(row.p90)}` : '';
    return `${fmt(row.median)}${spread}${p90}`;
}

/** Top entries of a categorical distribution, e.g. "search 62% · fetch 31%". */
export function distributionSummary(dist: Record<string, number>): string {
    return Object.entries(dist)
        .sort(([, a], [, b]) => b - a)
        .slice(0, 3)
        .map(([k, v]) => `${k} ${Math.round(v * 100)}%`)
        .join(' · ');
}

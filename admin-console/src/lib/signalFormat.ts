/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** How the Signals sub-tabs print a figure: "—" for a value that does not
 *  exist (never 0), dollars to 4 places, rates as percentages. */

export function money(v?: number | null, digits = 4): string {
    if (v === null || v === undefined) return '—';
    return `$${v.toFixed(digits)}`;
}

export function pct(v?: number | null): string {
    if (v === null || v === undefined) return '—';
    return `${(v * 100).toFixed(1)}%`;
}

export function num(v?: number | null): string {
    if (v === null || v === undefined) return '—';
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

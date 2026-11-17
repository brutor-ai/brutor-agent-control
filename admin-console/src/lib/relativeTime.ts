/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** "just now" / "4m ago" / "3h ago" / "2d ago" — the "as of" wording every
 *  verdict carries, because a health state of unknown age is not a health
 *  state. Shared by the status strip and the Signals overview so the two
 *  never phrase the same timestamp differently. */
export function ago(iso?: string | null): string {
    if (!iso) return '—';
    const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (secs < 90) return 'just now';
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
    return `${Math.round(secs / 86400)}d ago`;
}

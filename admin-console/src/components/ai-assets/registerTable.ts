/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The date format the Asset Register's tables share. Their header cell style
 * is not shared from here: a file that draws a table takes it from
 * `ui/tableStyles` itself (see `ui/__tests__/tableStyleAdoption.test.ts`).
 */

export function formatRegisterDate(s?: string | null): string {
    if (!s) return '—';
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

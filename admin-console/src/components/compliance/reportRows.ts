/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Evidence Report rows arrive as free-form dictionaries —
 * one per system × obligation × statement, or a `not_covered` row for an
 * obligation with no statement. This reads them at the boundary and groups
 * them the way the report must be shown: recomputed, judged, indicator and
 * declared rows apart, never merged; `not_covered` on its own.
 */

import { getArray, getNumber, getRecord, getString, isRecord } from '../../lib/narrow';
import type { StatementLike } from './StatementList';

export interface ReportRow extends StatementLike {
    ai_system_id: string;
    ai_system_name: string;
    obligation_key: string;
    obligation_title: string;
    reference: string;
    applies_from: string | null;
    applicability: string | null;
    obligation_limit: string | null;
}

export function readReportRow(raw: unknown): ReportRow | null {
    if (!isRecord(raw)) return null;
    const systemId = getString(raw, 'ai_system_id');
    const obligationKey = getString(raw, 'obligation_key');
    const status = getString(raw, 'status');
    if (!systemId || !obligationKey || !status) return null;
    return {
        key: getString(raw, 'statement_key') ?? `${obligationKey}:not_covered`,
        statement: getString(raw, 'statement') ?? null,
        basis: getString(raw, 'basis') ?? null,
        resolver: getString(raw, 'resolver') ?? null,
        status,
        n: getNumber(raw, 'n') ?? null,
        m: getNumber(raw, 'm') ?? null,
        detail: getString(raw, 'detail') ?? null,
        limit: getString(raw, 'limit') ?? null,
        citations: (getArray(raw, 'citations') ?? []).filter((c): c is string => typeof c === 'string'),
        catalog_version: getString(raw, 'catalog_version') ?? null,
        human_agreement: getRecord(raw, 'human_agreement') ?? null,
        ai_system_id: systemId,
        ai_system_name: getString(raw, 'ai_system_name') ?? systemId,
        obligation_key: obligationKey,
        obligation_title: getString(raw, 'obligation_title') ?? obligationKey,
        reference: getString(raw, 'reference') ?? '',
        applies_from: getString(raw, 'applies_from') ?? null,
        applicability: getString(raw, 'applicability') ?? null,
        obligation_limit: getString(raw, 'obligation_limit') ?? null,
    };
}

export const ROW_GROUPS = ['recomputed', 'judged', 'indicator', 'declared', 'not_covered'] as const;
export type RowGroup = (typeof ROW_GROUPS)[number];

export const ROW_GROUP_TITLE: Record<RowGroup, string> = {
    recomputed: 'Recomputed rows',
    judged: 'Judged rows — human agreement beside each',
    indicator: 'Indicator rows',
    declared: 'Declared rows',
    not_covered: 'Not covered — obligations with no statement in the catalog',
};

/** Rows by basis, in the order the report prints them. A row whose basis
 *  is unknown lands with the declared rows rather than being dropped. */
export function groupReportRows(rows: ReportRow[]): Record<RowGroup, ReportRow[]> {
    const out: Record<RowGroup, ReportRow[]> = { recomputed: [], judged: [], indicator: [], declared: [], not_covered: [] };
    for (const r of rows) {
        if (r.status === 'not_covered' || r.basis === null) out.not_covered.push(r);
        else if (r.basis === 'recomputed' || r.basis === 'judged' || r.basis === 'indicator') out[r.basis].push(r);
        else out.declared.push(r);
    }
    return out;
}

/** `met 3 · not met 1 · not evaluable 2` from a summary's per-basis counts. */
export function basisCountsText(counts: unknown): string {
    if (!isRecord(counts)) return '';
    return Object.entries(counts)
        .filter((e): e is [string, number] => typeof e[1] === 'number' && e[1] > 0)
        .map(([status, n]) => `${status.replace(/_/g, ' ')} ${n}`)
        .join(' · ');
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { basisCountsText, groupReportRows, readReportRow } from '../reportRows';

describe('report rows', () => {
    const rows = [
        { ai_system_id: 's1', ai_system_name: 'Copilot', obligation_key: 'o1', obligation_title: 'Logging', reference: 'Art 12', statement_key: 'k1', basis: 'recomputed', status: 'met', n: 3, m: 4, citations: ['r1'], catalog_version: '2026-09' },
        { ai_system_id: 's1', obligation_key: 'o2', obligation_title: 'Oversight', reference: 'Art 14', statement_key: 'k2', basis: 'judged', status: 'met', n: 57, m: 60, human_agreement: { n: 34, observed_agreement: 0.92 } },
        { ai_system_id: 's1', obligation_key: 'o3', obligation_title: 'Drift', reference: 'Art 15', statement_key: 'k3', basis: 'indicator', status: 'indicator' },
        { ai_system_id: 's1', obligation_key: 'o4', obligation_title: 'QMS', reference: 'Art 17', statement_key: 'k4', basis: 'declared', status: 'declared' },
        { ai_system_id: 's1', obligation_key: 'o5', obligation_title: 'Registration', reference: 'Art 49', statement_key: null, basis: null, status: 'not_covered', detail: 'No evidence statement in the catalog for this obligation.' },
        { nonsense: true },
        'not a row',
    ];

    it('reads rows at the boundary and drops malformed ones', () => {
        const parsed = rows.map(readReportRow);
        expect(parsed.filter((r) => r === null)).toHaveLength(2);
        expect(parsed[0]).toMatchObject({ key: 'k1', basis: 'recomputed', n: 3, m: 4, citations: ['r1'], ai_system_name: 'Copilot' });
        expect(parsed[1]?.human_agreement).toEqual({ n: 34, observed_agreement: 0.92 });
        expect(parsed[4]).toMatchObject({ key: 'o5:not_covered', basis: null, status: 'not_covered', ai_system_name: 's1' });
    });

    it('groups by basis without merging, not_covered apart', () => {
        const g = groupReportRows(rows.map(readReportRow).filter((r): r is NonNullable<typeof r> => r !== null));
        expect(g.recomputed.map((r) => r.key)).toEqual(['k1']);
        expect(g.judged.map((r) => r.key)).toEqual(['k2']);
        expect(g.indicator.map((r) => r.key)).toEqual(['k3']);
        expect(g.declared.map((r) => r.key)).toEqual(['k4']);
        expect(g.not_covered.map((r) => r.obligation_key)).toEqual(['o5']);
    });

    it('phrases per-basis counts', () => {
        expect(basisCountsText({ met: 3, not_met: 1, not_evaluable: 0 })).toBe('met 3 · not met 1');
        expect(basisCountsText(undefined)).toBe('');
    });
});

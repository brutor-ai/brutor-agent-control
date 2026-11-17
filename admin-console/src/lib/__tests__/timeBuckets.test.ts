/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The shared day→week→month grain rule. RG Usage and Mission Control's
 * cost composition both consume this — a threshold change here changes
 * both, which is the point.
 */

import { describe, it, expect } from 'vitest';
import { bucketDaily, bucketKey, grainFor } from '../timeBuckets';

const days = (n: number, from = '2026-01-01') => {
    const start = new Date(from + 'T00:00:00Z');
    return Array.from({ length: n }, (_, i) => {
        const d = new Date(start);
        d.setUTCDate(d.getUTCDate() + i);
        return { date: d.toISOString().slice(0, 10), cost: 1, requests: 2 };
    });
};

describe('grainFor', () => {
    it('follows the ≤30 daily / ≤92 weekly / else monthly rule', () => {
        expect(grainFor(30)).toBe('Daily');
        expect(grainFor(31)).toBe('Weekly');
        expect(grainFor(92)).toBe('Weekly');
        expect(grainFor(93)).toBe('Monthly');
    });
});

describe('bucketKey', () => {
    it('weekly keys are the Monday of the week', () => {
        // 1 January 2026 is a Thursday → its week's Monday is 29 December 2025.
        expect(bucketKey('2026-01-01', 'Weekly')).toBe('2025-12-29');
        expect(bucketKey('2025-12-29', 'Weekly')).toBe('2025-12-29');
    });
    it('monthly keys are YYYY-MM', () => {
        expect(bucketKey('2026-03-15', 'Monthly')).toBe('2026-03');
    });
});

describe('bucketDaily', () => {
    it('keeps ≤30 rows daily and untouched', () => {
        const rows = days(30);
        const r = bucketDaily(rows);
        expect(r.grain).toBe('Daily');
        expect(r.rows).toBe(rows);
    });

    it('sums numeric fields into weekly buckets', () => {
        const r = bucketDaily(days(35));
        expect(r.grain).toBe('Weekly');
        const total = r.rows.reduce((n, row) => n + row.cost, 0);
        expect(total).toBe(35); // nothing lost in the rollup
        // A full Monday-to-Sunday week sums 7 days.
        const full = r.rows.find(row => row.date === '2026-01-05');
        expect(full?.cost).toBe(7);
        expect(full?.requests).toBe(14);
    });

    it('rolls a year into monthly buckets, sorted', () => {
        const r = bucketDaily(days(365));
        expect(r.grain).toBe('Monthly');
        expect(r.rows[0].date).toBe('2026-01');
        expect(r.rows.length).toBe(12); // Jan…Dec 2026
        const jan = r.rows.find(row => row.date === '2026-01');
        expect(jan?.cost).toBe(31);
        const sorted = [...r.rows].sort((a, b) => a.date.localeCompare(b.date));
        expect(r.rows).toEqual(sorted);
    });

    it('preserves non-numeric fields from the first row of a bucket', () => {
        const rows = days(40).map(r => ({ ...r, label: `day-${r.date}` }));
        const r = bucketDaily(rows);
        expect(r.grain).toBe('Weekly');
        expect(r.rows[0]).toHaveProperty('label');
    });
});

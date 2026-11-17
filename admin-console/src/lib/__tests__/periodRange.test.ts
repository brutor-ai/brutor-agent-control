/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Period encodings → API window params. Calendar periods must produce
 * explicit start/end (FinOps reconciles against invoices, which are
 * calendar months); rolling presets stay period_hours.
 */

import { describe, it, expect } from 'vitest';
import { periodToHours, periodToRange } from '../periodRange';

describe('periodToRange', () => {
    it('rolling presets keep period_hours and no explicit range', () => {
        expect(periodToRange('24h')).toEqual({ period_hours: 24 });
        expect(periodToRange('90d')).toEqual({ period_hours: 2160 });
        expect(periodToRange('all')).toEqual({ period_hours: 0 });
    });

    it('mtd starts at the first of the current UTC month, open-ended', () => {
        const r = periodToRange('mtd', new Date('2026-08-04T12:00:00Z'));
        expect(r.period_hours).toBe(0);
        expect(r.start).toBe('2026-08-01T00:00:00.000Z');
        expect(r.end).toBeUndefined();
    });

    it('lastm is the whole previous calendar month, exclusive end', () => {
        const r = periodToRange('lastm', new Date('2026-08-04T12:00:00Z'));
        expect(r.start).toBe('2026-07-01T00:00:00.000Z');
        expect(r.end).toBe('2026-08-01T00:00:00.000Z');
    });

    it('lastm crosses the year boundary in January', () => {
        const r = periodToRange('lastm', new Date('2026-01-15T00:00:00Z'));
        expect(r.start).toBe('2025-12-01T00:00:00.000Z');
        expect(r.end).toBe('2026-01-01T00:00:00.000Z');
    });

    it('custom range: inclusive UI end date becomes an exclusive bound', () => {
        const r = periodToRange('custom:2026-04-01:2026-04-15');
        expect(r.start).toBe('2026-04-01T00:00:00.000Z');
        expect(r.end).toBe('2026-04-16T00:00:00.000Z'); // the 15th included
    });

    it('malformed custom input degrades to a day, not a crash', () => {
        expect(periodToRange('custom:garbage')).toEqual({ period_hours: 24 });
        expect(periodToRange('custom:2026-13-99:2026-14-99').period_hours).toBe(24);
    });
});

describe('periodToHours', () => {
    it('returns the preset hours', () => {
        expect(periodToHours('7d')).toBe(168);
        expect(periodToHours('all')).toBe(0);
    });

    it('approximates calendar ranges by their real length', () => {
        // 15 inclusive days = 360 hours.
        expect(periodToHours('custom:2026-04-01:2026-04-15')).toBe(360);
    });
});

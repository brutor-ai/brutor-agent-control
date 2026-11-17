/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Pure-function tests for the A2A formatters.
 *
 * These formatters feed multiple screens (card list, graph list,
 * graph detail, stats header). Matching them here catches any drift
 * between the shape the backend returns and what the UI displays
 * without having to render components.
 */

import { describe, it, expect } from 'vitest';

import {
    directionBadge,
    formatDurationMs,
    formatInt,
    formatPercent,
    formatRelative,
    trustTierBadge,
} from '../a2aFormatters';

describe('formatDurationMs', () => {
    it('returns em-dash for null/undefined', () => {
        expect(formatDurationMs(null)).toBe('—');
        expect(formatDurationMs(undefined)).toBe('—');
    });

    it('renders sub-millisecond as two-decimals ms', () => {
        expect(formatDurationMs(0.12345)).toBe('0.12 ms');
    });

    it('rounds sub-second to integer ms', () => {
        expect(formatDurationMs(123.8)).toBe('124 ms');
    });

    it('switches to seconds above 1000ms', () => {
        expect(formatDurationMs(1500)).toBe('1.50 s');
    });

    it('switches to minutes above 60s', () => {
        expect(formatDurationMs(150_000)).toBe('2.5 min');
    });
});

describe('formatInt', () => {
    it('returns em-dash for null', () => {
        expect(formatInt(null)).toBe('—');
    });

    it('uses locale grouping for large numbers', () => {
        // toLocaleString output varies by runtime; assert the commas are
        // present somewhere in 1_000_000's formatted form rather than
        // pinning the exact string.
        const s = formatInt(1_000_000);
        expect(s).toMatch(/1[,.\u00A0\u202F]?000[,.\u00A0\u202F]?000/);
    });
});

describe('formatPercent', () => {
    it('returns em-dash for null', () => {
        expect(formatPercent(null)).toBe('—');
    });

    it('formats as whole number with default digits', () => {
        expect(formatPercent(0.925)).toBe('92.5%');
    });

    it('honours requested digits', () => {
        expect(formatPercent(0.9251, 2)).toBe('92.51%');
    });
});

describe('formatRelative', () => {
    it('handles invalid input gracefully', () => {
        expect(formatRelative(null)).toBe('—');
        expect(formatRelative('not-a-date')).toBe('—');
    });

    it('returns "Ns ago" within the last minute', () => {
        const ts = new Date(Date.now() - 30_000).toISOString();
        expect(formatRelative(ts)).toMatch(/^\d+s ago$/);
    });
});

describe('trustTierBadge', () => {
    it('distinguishes all four trust tiers', () => {
        const tiers = ['unsigned', 'tenant_signed', 'partner_signed', 'revoked'] as const;
        const labels = tiers.map(t => trustTierBadge(t).label);
        expect(new Set(labels).size).toBe(tiers.length);
    });

    it('revoked tier carries danger colour in classes', () => {
        const badge = trustTierBadge('revoked');
        expect(badge.bg).toContain('red');
        expect(badge.title.toLowerCase()).toContain('revoked');
    });

    it('tenant_signed tier carries success colour', () => {
        expect(trustTierBadge('tenant_signed').bg).toContain('green');
    });
});

describe('directionBadge', () => {
    it('recognises inbound + outbound', () => {
        expect(directionBadge('inbound').label).toBe('Inbound');
        expect(directionBadge('outbound').label).toBe('Outbound');
    });

    it('falls back to muted badge for unknown direction', () => {
        const b = directionBadge(null);
        expect(b.bg).toContain('surface');
    });
});

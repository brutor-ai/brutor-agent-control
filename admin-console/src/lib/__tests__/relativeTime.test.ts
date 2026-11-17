/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** The "as of" wording every verdict carries. */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { ago } from '../relativeTime';

const NOW = new Date('2026-08-04T12:00:00Z');
const at = (secondsAgo: number) =>
    new Date(NOW.getTime() - secondsAgo * 1000).toISOString();

const freeze = () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
};

afterEach(() => {
    vi.useRealTimers();
});

describe('ago', () => {
    it('renders an em dash when there is no timestamp', () => {
        expect(ago()).toBe('—');
        expect(ago(null)).toBe('—');
        expect(ago('')).toBe('—');
    });

    it('collapses anything under 90 seconds to "just now"', () => {
        freeze();
        expect(ago(at(0))).toBe('just now');
        expect(ago(at(89))).toBe('just now');
    });

    it('switches to minutes at 90 seconds and to hours at an hour', () => {
        freeze();
        expect(ago(at(90))).toBe('2m ago');
        expect(ago(at(3599))).toBe('60m ago');
        expect(ago(at(3600))).toBe('1h ago');
    });

    it('switches to days at 24 hours', () => {
        freeze();
        expect(ago(at(86399))).toBe('24h ago');
        expect(ago(at(86400))).toBe('1d ago');
        expect(ago(at(86400 * 30))).toBe('30d ago');
    });

    it('clamps a future timestamp to "just now" rather than a negative age', () => {
        freeze();
        expect(ago(new Date(NOW.getTime() + 60_000).toISOString())).toBe('just now');
    });

    it('does not pretend an unparseable timestamp is recent', () => {
        freeze();
        // NaN comparisons are all false, so it falls through to the day branch.
        expect(ago('not-a-date')).toContain('ago');
    });
});

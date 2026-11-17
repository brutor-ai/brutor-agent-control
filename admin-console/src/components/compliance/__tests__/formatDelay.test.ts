/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, it, expect } from 'vitest';
import { formatDelay } from '../complianceFormat';

// The bug class: a value rounded into a unit that hides it. A record lag
// with a median of 8 ms rendered as "0.0 s" reads as "not measured".
describe('formatDelay', () => {
    it('shows a sub-second delay in milliseconds', () => {
        expect(formatDelay(0.008)).toBe('8 ms');
        expect(formatDelay(0.095)).toBe('95 ms');
        expect(formatDelay(0.9994)).toBe('999 ms');
    });

    it('shows seconds with one decimal, minutes beyond two minutes', () => {
        expect(formatDelay(1.5)).toBe('1.5 s');
        expect(formatDelay(3.329)).toBe('3.3 s');
        expect(formatDelay(600)).toBe('10 min');
    });

    it('never renders a measured non-zero delay as zero', () => {
        for (const s of [0.0006, 0.001, 0.004, 0.049, 0.5]) {
            expect(formatDelay(s)).not.toMatch(/^0(\.0)? /);
        }
        expect(formatDelay(0)).toBe('0 ms');
    });

    it('does not invent a value', () => {
        expect(formatDelay(Number.NaN)).toBe('—');
        expect(formatDelay(-1)).toBe('—');
    });
});

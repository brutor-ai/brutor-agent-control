/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The arithmetic behind an inherited limit field. Two verdicts that must not
 * be conflated: over the inherited floor is a REFUSAL (the API rejects the
 * save), over the unclaimed capacity is a WARNING (the save succeeds, the
 * budget is oversubscribed).
 */

import { describe, it, expect } from 'vitest';
import { fieldCapacity, getNestedLimit, limitFieldVerdict } from '../limitCapacity';
import type { LimitCapacityInfo } from '../groupTypes';

const capacity = (over: Partial<LimitCapacityInfo> = {}): LimitCapacityInfo => ({
    parent_limit: { llm: { requests_per_minute: 100 } },
    parent_name: 'Engineering',
    parent_is_immediate: true,
    siblings: [
        { group_id: 'g2', group_name: 'Platform', limits: { llm: { requests_per_minute: 40 } } },
        { group_id: 'g3', group_name: 'Data', limits: { llm: { requests_per_minute: 20 } } },
    ],
    sibling_total: { llm: { requests_per_minute: 60 } },
    available: { llm: { requests_per_minute: 40 } },
    ...over,
});

describe('getNestedLimit', () => {
    it('reads section.field when it holds a number', () => {
        expect(getNestedLimit({ llm: { rpm: 10 } }, 'llm', 'rpm')).toBe(10);
        expect(getNestedLimit({ llm: { rpm: 0 } }, 'llm', 'rpm')).toBe(0);
    });

    it('is undefined for a missing blob, section or field', () => {
        expect(getNestedLimit(null, 'llm', 'rpm')).toBeUndefined();
        expect(getNestedLimit(undefined, 'llm', 'rpm')).toBeUndefined();
        expect(getNestedLimit({}, 'llm', 'rpm')).toBeUndefined();
        expect(getNestedLimit({ llm: {} }, 'llm', 'rpm')).toBeUndefined();
    });

    it('refuses a non-numeric value rather than coercing it', () => {
        expect(getNestedLimit({ llm: { rpm: '10' } }, 'llm', 'rpm')).toBeUndefined();
        expect(getNestedLimit({ llm: { rpm: null } }, 'llm', 'rpm')).toBeUndefined();
        expect(getNestedLimit({ llm: 5 }, 'llm', 'rpm')).toBeUndefined();
    });
});

describe('fieldCapacity', () => {
    it('slices the holder, the claim and what is left out of the blob', () => {
        const fc = fieldCapacity(capacity(), 'llm', 'requests_per_minute');
        expect(fc).toEqual({
            holderLimit: 100,
            holderName: 'Engineering',
            holderIsParent: true,
            claimed: 60,
            available: 40,
            others: [
                { id: 'g2', name: 'Platform', value: 40 },
                { id: 'g3', name: 'Data', value: 20 },
            ],
        });
    });

    it('is null when no ancestor sets this field — nothing constrains it', () => {
        expect(fieldCapacity(capacity(), 'llm', 'tokens_per_minute')).toBeNull();
        expect(fieldCapacity(capacity({ parent_limit: null }), 'llm', 'requests_per_minute')).toBeNull();
    });

    it('is null when there is no capacity payload at all', () => {
        expect(fieldCapacity(null, 'llm', 'requests_per_minute')).toBeNull();
        expect(fieldCapacity(undefined, 'llm', 'requests_per_minute')).toBeNull();
    });

    it('says whether the holder is the immediate parent as the control plane does', () => {
        expect(
            fieldCapacity(capacity({ parent_is_immediate: true }), 'llm', 'requests_per_minute')
                ?.holderIsParent,
        ).toBe(true);
        expect(
            fieldCapacity(capacity({ parent_is_immediate: false }), 'llm', 'requests_per_minute')
                ?.holderIsParent,
        ).toBe(false);
    });

    it('keeps siblings that do not set the field, with an undefined value', () => {
        const fc = fieldCapacity(
            capacity({ siblings: [{ group_id: 'g9', group_name: 'Quiet', limits: null }] }),
            'llm',
            'requests_per_minute',
        );
        expect(fc?.others).toEqual([{ id: 'g9', name: 'Quiet', value: undefined }]);
    });

    it('reports an empty others list when nothing else draws on the budget', () => {
        const fc = fieldCapacity(capacity({ siblings: [] }), 'llm', 'requests_per_minute');
        expect(fc?.others).toEqual([]);
    });

    it('leaves claimed and available undefined when the server omits them', () => {
        const fc = fieldCapacity(
            capacity({ sibling_total: null, available: null }),
            'llm',
            'requests_per_minute',
        );
        expect(fc?.claimed).toBeUndefined();
        expect(fc?.available).toBeUndefined();
    });
});

describe('limitFieldVerdict', () => {
    const fc = fieldCapacity(capacity(), 'llm', 'requests_per_minute');

    it('separates the refusal from the warning', () => {
        // 50 is under the inherited 100 but over the 40 still unclaimed.
        expect(limitFieldVerdict(50, 100, fc)).toEqual({
            overInherited: false,
            overCapacity: true,
            ceiling: 40,
        });
        // 120 breaks both.
        expect(limitFieldVerdict(120, 100, fc)).toMatchObject({
            overInherited: true,
            overCapacity: true,
        });
    });

    it('is clean at exactly the inherited limit and exactly the capacity', () => {
        expect(limitFieldVerdict(100, 100, null).overInherited).toBe(false);
        expect(limitFieldVerdict(40, 100, fc).overCapacity).toBe(false);
    });

    it('shows the tightest of the two ceilings', () => {
        expect(limitFieldVerdict(1, 100, fc).ceiling).toBe(40);
        expect(limitFieldVerdict(1, 10, fc).ceiling).toBe(10);
    });

    it('has no ceiling when nothing is inherited and nothing is claimed', () => {
        expect(limitFieldVerdict(5, undefined, null).ceiling).toBeUndefined();
    });

    it('an empty field breaks nothing', () => {
        expect(limitFieldVerdict(undefined, 10, fc)).toEqual({
            overInherited: false,
            overCapacity: false,
            ceiling: 10,
        });
    });

    it('treats zero as a real value, not as absent', () => {
        expect(limitFieldVerdict(1, 0, null).overInherited).toBe(true);
        expect(limitFieldVerdict(0, 0, null).overInherited).toBe(false);
        expect(limitFieldVerdict(1, undefined, null).overInherited).toBe(false);
    });
});

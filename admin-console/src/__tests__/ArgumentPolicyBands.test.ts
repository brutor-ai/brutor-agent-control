/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Band logic for argument policies.
 *
 * Two silent failure modes, both of which produce a policy that reads as
 * enforced and is not:
 *
 * * a looser rule placed above a tighter one makes the tighter one
 *   unreachable, and nothing errors;
 * * a threshold on a field the caller can simply omit never fires.
 *
 * The API refuses the second. The form has to catch both before an operator
 * meets a 400, which is what this module is for.
 */

import { describe, expect, it } from 'vitest';

import {
    PRESENCE_RULE,
    comparedPaths,
    describeBand,
    effectiveSeverity,
    isComparisonRule,
    shadowedRules,
    unguardedPaths,
} from '../lib/argumentPolicyBands';
import {
    PREDICATES_BY_ANALYZER,
    PREDICATES_REQUIRING_FIELD,
    SEVERITY_LABEL,
    type ArgumentPolicyRule,
} from '../lib/argumentPolicyTypes';

const REQUIRED_AMOUNT = { schema: { required: ['amount'] } };

const BAND: ArgumentPolicyRule[] = [
    { deny_if: 'schema_invalid', severity: 'deny' },
    { deny_if: 'field_gt', field: 'amount', value: 10000, severity: 'deny' },
    { deny_if: 'field_gt', field: 'amount', value: 1000, severity: 'approval_required' },
];

describe('the predicate catalogue', () => {
    it('offers the eight comparisons on the JSON analyzer', () => {
        for (const p of PREDICATES_REQUIRING_FIELD) {
            expect(PREDICATES_BY_ANALYZER.json).toContain(p);
        }
        expect(PREDICATES_REQUIRING_FIELD.size).toBe(8);
    });

    it('names all three severities', () => {
        expect(Object.keys(SEVERITY_LABEL).sort()).toEqual([
            'approval_required',
            'deny',
            'warn',
        ]);
    });
});

describe('isComparisonRule / comparedPaths', () => {
    it('recognises a comparison and ignores the rest', () => {
        expect(isComparisonRule({ deny_if: 'field_gt', field: 'a', value: 1 })).toBe(true);
        expect(isComparisonRule({ deny_if: 'schema_invalid' })).toBe(false);
        expect(isComparisonRule({})).toBe(false);
    });

    it('collects each compared path once', () => {
        expect(comparedPaths(BAND)).toEqual(['amount']);
        expect(
            comparedPaths([
                { deny_if: 'field_gt', field: 'a', value: 1 },
                { deny_if: 'field_lt', field: 'b', value: 1 },
            ]),
        ).toEqual(['a', 'b']);
    });
});

describe('unguardedPaths — the omit-the-field bypass', () => {
    it('flags a bare threshold', () => {
        expect(
            unguardedPaths([{ deny_if: 'field_gt', field: 'amount', value: 1000 }], null),
        ).toEqual(['amount']);
    });

    it('accepts a schema requirement paired with a rule that denies on it', () => {
        expect(unguardedPaths(BAND, REQUIRED_AMOUNT)).toEqual([]);
    });

    it('rejects a schema requirement with nothing denying on it', () => {
        // `schema.required` only populates an error; something must deny on
        // that error for the requirement to bite.
        expect(
            unguardedPaths(
                [{ deny_if: 'field_gt', field: 'amount', value: 1000 }],
                REQUIRED_AMOUNT,
            ),
        ).toEqual(['amount']);
    });

    it('accepts an explicit presence rule in either phrasing', () => {
        expect(
            unguardedPaths(
                [
                    { allow_only_if: 'field_present', values: ['amount'] },
                    { deny_if: 'field_gt', field: 'amount', value: 1000 },
                ],
                null,
            ),
        ).toEqual([]);
        expect(
            unguardedPaths(
                [
                    { deny_if: 'field_not_present', values: ['amount'] },
                    { deny_if: 'field_gt', field: 'amount', value: 1000 },
                ],
                null,
            ),
        ).toEqual([]);
    });

    it('guarding one path does not guard another', () => {
        expect(
            unguardedPaths(
                [
                    { deny_if: 'schema_invalid' },
                    { deny_if: 'field_gt', field: 'amount', value: 1000 },
                    { deny_if: 'field_eq', field: 'currency', value: 'XXX' },
                ],
                REQUIRED_AMOUNT,
            ),
        ).toEqual(['currency']);
    });

    it('says nothing about a policy with no comparison rules', () => {
        expect(unguardedPaths([{ deny_if: 'writes' }], null)).toEqual([]);
    });

    it('seeds a presence rule that actually guards', () => {
        const seeded = [PRESENCE_RULE, { deny_if: 'field_gt', field: 'amount', value: 1000 }];
        expect(unguardedPaths(seeded, REQUIRED_AMOUNT)).toEqual([]);
    });
});

describe('shadowedRules — ordering is the band', () => {
    it('is quiet on a correctly ordered band', () => {
        expect(shadowedRules(BAND)).toEqual([]);
    });

    it('flags a looser upper threshold placed first', () => {
        const wrong: ArgumentPolicyRule[] = [
            { deny_if: 'field_gt', field: 'amount', value: 1000, severity: 'approval_required' },
            { deny_if: 'field_gt', field: 'amount', value: 10000, severity: 'deny' },
        ];
        const found = shadowedRules(wrong);
        expect(found).toHaveLength(1);
        expect(found[0].index).toBe(1);
        expect(found[0].shadowedBy).toBe(0);
        expect(found[0].message).toContain('can never run');
    });

    it('flags the mirror case for lower thresholds', () => {
        const wrong: ArgumentPolicyRule[] = [
            { deny_if: 'field_lt', field: 'amount', value: 100 },
            { deny_if: 'field_lt', field: 'amount', value: 10 },
        ];
        expect(shadowedRules(wrong)).toHaveLength(1);
    });

    it('does not confuse different fields', () => {
        expect(
            shadowedRules([
                { deny_if: 'field_gt', field: 'a', value: 1 },
                { deny_if: 'field_gt', field: 'b', value: 100 },
            ]),
        ).toEqual([]);
    });

    it('does not confuse opposite directions', () => {
        expect(
            shadowedRules([
                { deny_if: 'field_gt', field: 'amount', value: 1000 },
                { deny_if: 'field_lt', field: 'amount', value: 10 },
            ]),
        ).toEqual([]);
    });

    it('ignores non-numeric operands', () => {
        expect(
            shadowedRules([
                { deny_if: 'field_gt', field: 'amount', value: 'x' },
                { deny_if: 'field_gt', field: 'amount', value: 10 },
            ]),
        ).toEqual([]);
    });
});

describe('effectiveSeverity', () => {
    it("takes the rule's own severity when set", () => {
        expect(effectiveSeverity({ severity: 'approval_required' }, 'deny')).toBe(
            'approval_required',
        );
    });

    it("falls back to the policy's", () => {
        expect(effectiveSeverity({}, 'warn')).toBe('warn');
    });
});

describe('describeBand', () => {
    it('reads the band back in evaluation order', () => {
        expect(describeBand(BAND, 'warn')).toEqual([
            'amount above 10000 → refuse',
            'amount above 1000 → hold for a human',
        ]);
    });

    it('uses the policy severity for a rule that sets none', () => {
        expect(
            describeBand([{ deny_if: 'field_gt', field: 'amount', value: 5 }], 'deny'),
        ).toEqual(['amount above 5 → refuse']);
    });

    it('renders membership rules from their values list', () => {
        expect(
            describeBand(
                [{ deny_if: 'field_not_in', field: 'currency', values: ['GBP', 'EUR'] }],
                'deny',
            ),
        ).toEqual(['currency not one of GBP, EUR → refuse']);
    });
});

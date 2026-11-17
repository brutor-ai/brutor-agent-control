/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two ways a band silently stops governing anything: a looser rule
 * placed above a tighter one, and a threshold on a field the caller can
 * simply omit. Both are invisible at runtime, so they are pinned here.
 */

import { thrownByClient } from '../../test/serverAnswers';
import { describe, it, expect } from 'vitest';
import {
    PRESENCE_RULE,
    bySeverityStrictestFirst,
    comparedPaths,
    describeBand,
    effectiveSeverity,
    isComparisonRule,
    shadowedRules,
    unguardedPaths,
    presenceGuardProblems,
    presenceGuardMessage,
    argumentPolicyRefusalText,
} from '../argumentPolicyBands';
import type { ArgumentPolicyRule, Severity } from '../argumentPolicyTypes';

const gt = (field: string, value: number, severity?: Severity): ArgumentPolicyRule => ({
    deny_if: 'field_gt',
    field,
    value,
    ...(severity ? { severity } : {}),
});

describe('isComparisonRule', () => {
    it('is true only for the predicates that read a dotted field', () => {
        expect(isComparisonRule(gt('amount', 10))).toBe(true);
        expect(isComparisonRule({ allow_only_if: 'field_in', field: 'currency' })).toBe(true);
        expect(isComparisonRule({ deny_if: 'schema_invalid' })).toBe(false);
        expect(isComparisonRule({ allow_only_if: 'field_present', values: ['amount'] })).toBe(false);
    });

    it('is false for a rule carrying no predicate at all', () => {
        expect(isComparisonRule({})).toBe(false);
        expect(isComparisonRule({ deny_if: '' })).toBe(false);
    });
});

describe('comparedPaths', () => {
    it('de-duplicates the paths the comparison rules read', () => {
        expect(
            comparedPaths([gt('amount', 1000), gt('amount', 10000), gt('fee', 5)]),
        ).toEqual(['amount', 'fee']);
    });

    it('ignores comparison rules with no field and non-comparison rules', () => {
        expect(
            comparedPaths([
                { deny_if: 'field_gt', value: 5 },
                { deny_if: 'schema_invalid' },
                { allow_only_if: 'field_present', values: ['amount'] },
            ]),
        ).toEqual([]);
    });

    it('returns an empty list for no rules', () => {
        expect(comparedPaths([])).toEqual([]);
    });
});

describe('unguardedPaths', () => {
    it('reports a threshold on a field nothing forces the caller to send', () => {
        expect(unguardedPaths([gt('amount', 1000)], null)).toEqual(['amount']);
    });

    it('accepts a schema-required field only when a schema_invalid rule exists', () => {
        const rules = [gt('amount', 1000)];
        const config = { schema: { required: ['amount'] } };
        // Required in the schema, but nothing denies on a schema failure —
        // so the schema is decorative and the path is still unguarded.
        expect(unguardedPaths(rules, config)).toEqual(['amount']);
        expect(unguardedPaths([PRESENCE_RULE, ...rules], config)).toEqual([]);
    });

    it('accepts an explicit presence rule naming the path', () => {
        const rules: ArgumentPolicyRule[] = [
            { allow_only_if: 'field_present', values: ['amount'] },
            gt('amount', 1000),
        ];
        expect(unguardedPaths(rules, null)).toEqual([]);
        expect(
            unguardedPaths([{ deny_if: 'field_not_present', values: ['amount'] }, ...rules.slice(1)], null),
        ).toEqual([]);
    });

    it('only credits a presence rule for the exact path it names', () => {
        const rules: ArgumentPolicyRule[] = [
            { allow_only_if: 'field_present', values: ['currency'] },
            gt('amount', 1000),
        ];
        expect(unguardedPaths(rules, null)).toEqual(['amount']);
    });

    it('ignores non-string entries in a presence rule values list', () => {
        const rules: ArgumentPolicyRule[] = [
            { allow_only_if: 'field_present', values: [42, null] },
            gt('amount', 1000),
        ];
        expect(unguardedPaths(rules, null)).toEqual(['amount']);
    });

    it('has nothing to guard when no rule compares a field', () => {
        expect(unguardedPaths([{ deny_if: 'multi_statement' }], undefined)).toEqual([]);
    });

    it('survives a malformed analyzer_config', () => {
        const rules = [PRESENCE_RULE, gt('amount', 1000)];
        expect(unguardedPaths(rules, { schema: { required: 'amount' } })).toEqual(['amount']);
        expect(unguardedPaths(rules, {})).toEqual(['amount']);
    });
});

describe('a presence guard has to deny, and has to name one path', () => {
    const band = [gt('amount', 1000)];
    const required = { schema: { required: ['amount'] } };

    it('does not credit a guard whose effective severity is warn or approval: the gateway lets the call through or holds it', () => {
        for (const severity of ['warn', 'approval_required'] as const) {
            const own: ArgumentPolicyRule[] = [{ deny_if: 'field_not_present', values: ['amount'], severity }, ...band];
            expect(presenceGuardProblems(own, null, 'deny')).toEqual([{ path: 'amount', why: 'does_not_deny' }]);
            expect(unguardedPaths(own, null, 'deny')).toEqual(['amount']);
            // A guard without a severity of its own takes the policy's.
            const inherited: ArgumentPolicyRule[] = [{ deny_if: 'field_not_present', values: ['amount'] }, ...band];
            expect(presenceGuardProblems(inherited, null, severity)).toEqual([{ path: 'amount', why: 'does_not_deny' }]);
            expect(presenceGuardProblems([{ deny_if: 'schema_invalid', severity }, ...band], required, 'deny')).toEqual([
                { path: 'amount', why: 'does_not_deny' },
            ]);
        }
    });

    it('credits a guard that denies by its own severity under a policy that only warns', () => {
        const rules: ArgumentPolicyRule[] = [{ deny_if: 'field_not_present', values: ['amount'], severity: 'deny' }, ...band];
        expect(presenceGuardProblems(rules, null, 'warn')).toEqual([]);
        expect(presenceGuardProblems([PRESENCE_RULE, ...band], required, 'warn')).toEqual([]);
    });

    it('does not credit a presence rule that names several paths: one of them present satisfies it', () => {
        const rules: ArgumentPolicyRule[] = [
            { deny_if: 'field_not_present', values: ['amount', 'currency'] },
            gt('amount', 1000),
            gt('currency', 3),
        ];
        expect(presenceGuardProblems(rules, null, 'deny')).toEqual([
            { path: 'amount', why: 'several_paths' },
            { path: 'currency', why: 'several_paths' },
        ]);
    });

    it('says a path has no guard at all', () => {
        expect(presenceGuardProblems(band, null, 'deny')).toEqual([{ path: 'amount', why: 'missing' }]);
    });

    it('says in plain words what is wrong with each path and what to do', () => {
        expect(presenceGuardMessage([{ path: 'amount', why: 'missing' }])).toBe(
            'A caller can bypass this policy by leaving out amount. Add a rule that denies the call when amount is not present, or require it in the analyzer config.',
        );
        expect(presenceGuardMessage([{ path: 'amount', why: 'does_not_deny' }])).toBe(
            'A caller can bypass this policy by leaving out amount. The rule that guards amount does not deny: set its severity to Deny. A warning lets the call through, and an approval holds it instead of refusing it.',
        );
        expect(presenceGuardMessage([{ path: 'amount', why: 'several_paths' }, { path: 'currency', why: 'several_paths' }])).toBe(
            'A caller can bypass this policy by leaving out amount, currency. A presence rule that names several paths is satisfied by any one of them: give amount, currency a presence rule each.',
        );
        expect(presenceGuardMessage([])).toBe('');
    });
});

describe('the Control Plane\'s refusal of a policy without a presence guard', () => {
    const BASE =
        'A comparison rule can be bypassed by omitting the field it compares. Add the path to analyzer_config.schema.required (with a schema_invalid rule) or an explicit field_present rule for: amount, currency';
    const NOT_DENY =
        ". The rule guarding amount does not deny: its effective severity (its own, else the policy's) must be 'deny', because the gateway lets a 'warn' through and holds an 'approval_required' instead of refusing it";
    const SHARED =
        '. A presence rule naming several paths is satisfied by any one of them, so it guards none: give currency a presence rule of its own';

    it('names the paths and says what to add, when the policy is created (422) and when it is changed (400)', async () => {
        const said =
            'Nothing was saved. A caller can bypass this policy by leaving out amount, currency. Add a rule that denies the call when the field is not present, one rule for each field, or require the field in the analyzer config.';
        expect(argumentPolicyRefusalText(await thrownByClient(400, { detail: BASE }))).toBe(said);
        const created = await thrownByClient(422, { detail: [{ type: 'value_error', loc: ['body'], msg: `Value error, ${BASE}` }] });
        expect(argumentPolicyRefusalText(created)).toBe(said);
    });

    it('says a guard does not deny, and that a guard names one path', async () => {
        const text = argumentPolicyRefusalText(await thrownByClient(400, { detail: BASE + NOT_DENY + SHARED }));
        expect(text).toBe(
            'Nothing was saved. A caller can bypass this policy by leaving out amount, currency. The rule that guards amount does not deny: set its severity to Deny. A warning lets the call through, and an approval holds it instead of refusing it. A presence rule that names several paths is satisfied by any one of them: give currency a presence rule of its own.',
        );
        expect(text).not.toMatch(/analyzer_config|schema_invalid|field_present|approval_required/);
    });

    it('is undefined for any other refusal', async () => {
        expect(argumentPolicyRefusalText(await thrownByClient(400, { detail: 'Name already exists' }))).toBeUndefined();
        expect(argumentPolicyRefusalText(new Error('x'))).toBeUndefined();
    });
});

describe('shadowedRules', () => {
    it('flags a low upper threshold placed above a higher one', () => {
        const found = shadowedRules([gt('amount', 1000), gt('amount', 10000)]);
        expect(found).toHaveLength(1);
        expect(found[0]).toMatchObject({ index: 1, shadowedBy: 0, field: 'amount' });
        expect(found[0].message).toContain('Rule 2 can never run');
        expect(found[0].message).toContain('rule 1');
    });

    it('accepts the tighter threshold placed first', () => {
        expect(shadowedRules([gt('amount', 10000), gt('amount', 1000)])).toEqual([]);
    });

    it('treats an equal threshold as swallowing the later rule', () => {
        expect(shadowedRules([gt('amount', 1000), gt('amount', 1000)])).toHaveLength(1);
    });

    it('inverts the direction for the lower-bound predicates', () => {
        const lt = (value: number): ArgumentPolicyRule => ({
            deny_if: 'field_lt',
            field: 'amount',
            value,
        });
        // A HIGHER `field_lt` bound matches more often, so it shadows.
        expect(shadowedRules([lt(100), lt(10)])).toHaveLength(1);
        expect(shadowedRules([lt(10), lt(100)])).toEqual([]);
    });

    it('never crosses fields or directions', () => {
        expect(shadowedRules([gt('amount', 1), gt('fee', 100)])).toEqual([]);
        expect(
            shadowedRules([
                { deny_if: 'field_lt', field: 'amount', value: 1 },
                gt('amount', 100),
            ]),
        ).toEqual([]);
    });

    it('ignores rules whose operand is not a number', () => {
        expect(
            shadowedRules([gt('amount', 1000), { deny_if: 'field_gt', field: 'amount', value: 'big' }]),
        ).toEqual([]);
    });

    it('reports each shadowed rule once, against its first shadower', () => {
        const found = shadowedRules([gt('amount', 100), gt('amount', 1000), gt('amount', 10000)]);
        expect(found.map((f) => [f.index, f.shadowedBy])).toEqual([
            [1, 0],
            [2, 0],
        ]);
    });

    it('reads allow_only_if as well as deny_if', () => {
        expect(
            shadowedRules([
                { allow_only_if: 'field_gt', field: 'amount', value: 10 },
                { allow_only_if: 'field_gt', field: 'amount', value: 100 },
            ]),
        ).toHaveLength(1);
    });
});

describe('effectiveSeverity', () => {
    it('prefers the rule severity and falls back to the policy severity', () => {
        expect(effectiveSeverity(gt('amount', 1, 'warn'), 'deny')).toBe('warn');
        expect(effectiveSeverity(gt('amount', 1), 'approval_required')).toBe('approval_required');
    });
});

describe('describeBand', () => {
    it('reads the band back in evaluation order with each rule outcome', () => {
        const lines = describeBand(
            [
                PRESENCE_RULE,
                gt('amount', 1000, 'approval_required'),
                gt('amount', 10000, 'deny'),
            ],
            'warn',
        );
        expect(lines).toEqual([
            'amount above 1000 → hold for a human',
            'amount above 10000 → refuse',
        ]);
    });

    it('falls back to the policy severity and says "log only" for warn', () => {
        expect(describeBand([gt('amount', 5)], 'warn')).toEqual(['amount above 5 → log only']);
    });

    it('uses the values list for the set predicates', () => {
        expect(
            describeBand(
                [{ deny_if: 'field_not_in', field: 'currency', values: ['GBP', 'EUR'] }],
                'deny',
            ),
        ).toEqual(['currency not one of GBP, EUR → refuse']);
    });

    it('renders an object operand as JSON rather than [object Object]', () => {
        const line = describeBand(
            [{ deny_if: 'field_eq', field: 'target', value: { id: 7 } }],
            'deny',
        )[0];
        expect(line).toContain('{"id":7}');
        expect(line).not.toContain('[object Object]');
    });

    it('drops rules that are not field comparisons', () => {
        expect(describeBand([PRESENCE_RULE, { deny_if: 'multi_statement' }], 'deny')).toEqual([]);
    });
});

describe('bySeverityStrictestFirst', () => {
    it('sorts deny before approval_required before warn', () => {
        const sorted: Severity[] = ['warn', 'deny', 'approval_required'].sort(
            bySeverityStrictestFirst as (a: string, b: string) => number,
        ) as Severity[];
        expect(sorted).toEqual(['deny', 'approval_required', 'warn']);
    });

    it('returns 0 for equal severities', () => {
        expect(bySeverityStrictestFirst('deny', 'deny')).toBe(0);
    });
});

describe('PRESENCE_RULE', () => {
    it('denies rather than warns — a bypassed threshold is not an advisory', () => {
        expect(PRESENCE_RULE.severity).toBe('deny');
        expect(PRESENCE_RULE.deny_if).toBe('schema_invalid');
    });
});

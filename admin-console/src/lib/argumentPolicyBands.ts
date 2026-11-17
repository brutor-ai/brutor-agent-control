/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Band logic for argument policies.
 *
 * A band is "allow below the threshold, hold for a human above it, refuse
 * far above it", expressed as ordered rules with per-rule severity. Two
 * things make that safe rather than decorative, and both are easy to get
 * wrong in a form:
 *
 * 1. **Ordering is the band.** Rules are evaluated in order and the first
 *    match wins, so a looser rule placed above a tighter one makes the
 *    tighter one unreachable. `> 1000 → approval` above `> 10000 → deny`
 *    means a 50,000 transfer only ever asks for approval. Nothing errors;
 *    the deny simply never runs.
 *
 * 2. **A threshold on a field the caller can omit is not a threshold.** A
 *    comparison predicate reading a missing field does not hold, so an agent
 *    that omits `amount` walks straight past `field_gt: amount`. The API
 *    refuses to save such a policy; the form seeds the presence rule so an
 *    operator never meets that refusal by accident.
 *
 * Kept free of React so both rules can be pinned without rendering.
 */

import { getArray, getRecord, getString } from './narrow';
import {
    PREDICATES_REQUIRING_FIELD,
    SEVERITY_RANK,
    type ArgumentPolicyRule,
    type Severity,
} from './argumentPolicyTypes';

/** The rule that makes a threshold un-bypassable. */
export const PRESENCE_RULE: ArgumentPolicyRule = {
    deny_if: 'schema_invalid',
    severity: 'deny',
    message: 'Called without the field this policy checks',
};

/** Is this rule one of the comparison predicates? */
export function isComparisonRule(rule: ArgumentPolicyRule): boolean {
    const predicate = rule.deny_if || rule.allow_only_if;
    return !!predicate && PREDICATES_REQUIRING_FIELD.has(predicate);
}

/** Every dotted path the rules compare. */
export function comparedPaths(rules: ArgumentPolicyRule[]): string[] {
    const seen = new Set<string>();
    for (const rule of rules) {
        if (isComparisonRule(rule) && rule.field) seen.add(rule.field);
    }
    return [...seen];
}

/** Why a compared path is not guarded against being left out. */
export type GuardWeakness =
    /** Nothing refuses a call without the field. */
    | 'missing'
    /** A rule guards it, with a severity that does not refuse the call. */
    | 'does_not_deny'
    /** Only a presence rule naming several paths names it, and one of them present satisfies that rule. */
    | 'several_paths';

export interface UnguardedPath {
    path: string;
    why: GuardWeakness;
}

/**
 * The compared paths a caller could leave out, each with why.
 *
 * The rule is the Control Plane's, which applies it to the policy as it
 * will be stored, when one is created and when one is changed. A compared
 * path is guarded when a call without it is refused: the path is in
 * `analyzer_config.schema.required` and a `schema_invalid` rule denies, or
 * a presence rule (`field_present`, `field_not_present`) that names this
 * one path denies. "Denies" is the rule's effective severity: its own,
 * else the policy's. A warning lets the call through and an approval holds
 * it for a person, so a guard of either leaves the field open. A presence
 * rule naming several paths is satisfied by any one of them and guards
 * none.
 */
export function presenceGuardProblems(
    rules: ArgumentPolicyRule[],
    analyzerConfig: Record<string, unknown> | null | undefined,
    policySeverity: Severity = 'deny',
): UnguardedPath[] {
    const compared = comparedPaths(rules);
    if (compared.length === 0) return [];
    const denies = (rule: ArgumentPolicyRule) => effectiveSeverity(rule, policySeverity) === 'deny';

    const schema = (analyzerConfig?.schema ?? {}) as { required?: unknown };
    const required = new Set(
        Array.isArray(schema.required)
            ? (schema.required as unknown[]).filter((r): r is string => typeof r === 'string')
            : [],
    );
    const guarded = new Set<string>();
    const weak = new Set<string>();
    const shared = new Set<string>();

    const schemaRules = rules.filter((r) => r.deny_if === 'schema_invalid');
    if (schemaRules.some(denies)) {
        for (const path of required) guarded.add(path);
    } else if (schemaRules.length > 0) {
        for (const path of required) weak.add(path);
    }

    for (const rule of rules) {
        if (rule.allow_only_if !== 'field_present' && rule.deny_if !== 'field_not_present') continue;
        const paths = new Set((rule.values ?? []).filter((v): v is string => typeof v === 'string'));
        const into = paths.size !== 1 ? shared : denies(rule) ? guarded : weak;
        for (const path of paths) into.add(path);
    }

    return compared
        .filter((path) => !guarded.has(path))
        .map((path) => ({ path, why: weak.has(path) ? 'does_not_deny' : shared.has(path) ? 'several_paths' : 'missing' }));
}

/** The compared paths a caller could leave out (`presenceGuardProblems`, without the reasons). */
export function unguardedPaths(
    rules: ArgumentPolicyRule[],
    analyzerConfig: Record<string, unknown> | null | undefined,
    policySeverity: Severity = 'deny',
): string[] {
    return presenceGuardProblems(rules, analyzerConfig, policySeverity).map(({ path }) => path);
}

const NOT_DENY = 'does not deny: set its severity to Deny. A warning lets the call through, and an approval holds it instead of refusing it.';
const SEVERAL = 'A presence rule that names several paths is satisfied by any one of them:';

/** What is wrong with the unguarded paths and what to do, in one message; '' for none. */
export function presenceGuardMessage(problems: readonly UnguardedPath[]): string {
    if (problems.length === 0) return '';
    const of = (why: GuardWeakness) => problems.filter((p) => p.why === why).map((p) => p.path);
    const parts = [`A caller can bypass this policy by leaving out ${problems.map((p) => p.path).join(', ')}.`];
    const missing = of('missing');
    const weak = of('does_not_deny');
    const several = of('several_paths');
    if (missing.length > 0) {
        const it = missing.join(', ');
        parts.push(`Add a rule that denies the call when ${it} is not present, or require it in the analyzer config.`);
    }
    if (weak.length > 0) parts.push(`The rule that guards ${weak.join(', ')} ${NOT_DENY}`);
    if (several.length > 0) parts.push(`${SEVERAL} give ${several.join(', ')} a presence rule each.`);
    return parts.join(' ');
}

/** The rule, as the form states it beside the rules. */
export const PRESENCE_GUARD_HELP =
    'A rule that compares a field does nothing when the caller leaves the field out. So every compared field needs a guard that refuses a call without it: ' +
    'a rule that denies when the field is not present, naming that one field, or the field listed as required in the analyzer config together with a rule that denies an invalid schema. ' +
    'The guard has to deny: a warning lets the call through and an approval holds it. The Control Plane checks this when a policy is created and each time it is changed, and refuses a policy without the guard.';

/**
 * The Control Plane's refusal of a policy whose compared field can be left
 * out, in plain words; `undefined` for any other failure. It answers 422
 * with a field error when the policy is created and 400 with the sentence
 * when it is changed. `error` is what the API client threw.
 */
export function argumentPolicyRefusalText(error: unknown): string | undefined {
    const body = getRecord(error, 'details');
    const said =
        getString(body, 'detail') ??
        (getArray(body, 'detail') ?? []).map((item) => getString(item, 'msg') ?? '').find((msg) => msg.includes('can be bypassed by omitting'));
    const paths = said === undefined ? undefined : /can be bypassed by omitting the field it compares\..* rule for: ([^.]+?)(?:\. |$)/s.exec(said)?.[1];
    if (said === undefined || paths === undefined) return undefined;
    const parts = [`Nothing was saved. A caller can bypass this policy by leaving out ${paths.trim()}.`];
    const weak = /The rule guarding (.+?) does not deny/.exec(said)?.[1];
    const several = /guards none: give (.+?) a presence rule of its own/.exec(said)?.[1];
    if (weak) parts.push(`The rule that guards ${weak} ${NOT_DENY}`);
    if (several) parts.push(`${SEVERAL} give ${several} a presence rule of its own.`);
    if (!weak && !several) {
        parts.push('Add a rule that denies the call when the field is not present, one rule for each field, or require the field in the analyzer config.');
    }
    return parts.join(' ');
}

export interface ShadowedRule {
    /** Index of the rule that can never run. */
    index: number;
    /** Index of the earlier, looser rule that swallows it. */
    shadowedBy: number;
    field: string;
    message: string;
}

/**
 * Rules that can never run because an earlier, looser rule on the same field
 * always matches first.
 *
 * Only meaningful for the ordering predicates, where "looser" has a
 * direction: for `field_gt` / `field_gte` a LOWER threshold matches more
 * often, and for `field_lt` / `field_lte` a HIGHER one does.
 */
export function shadowedRules(rules: ArgumentPolicyRule[]): ShadowedRule[] {
    // The form holds a threshold as the text of its input until it is saved.
    const threshold = (value: unknown): number | undefined => {
        if (typeof value === 'number') return value;
        if (typeof value !== 'string' || value.trim() === '') return undefined;
        const n = Number(value);
        return Number.isNaN(n) ? undefined : n;
    };
    const out: ShadowedRule[] = [];
    const direction: Record<string, 'upper' | 'lower'> = {
        field_gt: 'upper',
        field_gte: 'upper',
        field_lt: 'lower',
        field_lte: 'lower',
    };

    for (let i = 0; i < rules.length; i++) {
        const later = rules[i];
        const laterPredicate = later.deny_if || later.allow_only_if;
        const laterDir = laterPredicate ? direction[laterPredicate] : undefined;
        const laterValue = threshold(later.value);
        if (!laterDir || !later.field || laterValue === undefined) continue;

        for (let j = 0; j < i; j++) {
            const earlier = rules[j];
            const earlierPredicate = earlier.deny_if || earlier.allow_only_if;
            const earlierDir = earlierPredicate ? direction[earlierPredicate] : undefined;
            const earlierValue = threshold(earlier.value);
            if (
                !earlierDir ||
                earlierDir !== laterDir ||
                earlier.field !== later.field ||
                earlierValue === undefined
            ) {
                continue;
            }
            // An earlier rule that matches everything the later one would.
            const swallows =
                laterDir === 'upper'
                    ? earlierValue <= laterValue
                    : earlierValue >= laterValue;
            if (swallows) {
                out.push({
                    index: i,
                    shadowedBy: j,
                    field: later.field,
                    message:
                        `Rule ${i + 1} can never run: rule ${j + 1} already matches every ` +
                        `${later.field} it would. Put the tighter threshold first.`,
                });
                break;
            }
        }
    }
    return out;
}

/**
 * The severity a rule actually carries — its own, or the policy's.
 */
export function effectiveSeverity(
    rule: ArgumentPolicyRule,
    policySeverity: Severity,
): Severity {
    return rule.severity ?? policySeverity;
}

/**
 * Render a rule operand for reading. Rule values arrive untyped from the
 * API, so an object would otherwise stringify to `[object Object]` — which
 * tells an operator nothing about the threshold they are about to save.
 */
function operandText(value: unknown): string {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
        return String(value);
    }
    return JSON.stringify(value) ?? '';
}

/**
 * A one-line summary of what a band does, in the order it does it. Shown
 * under the rule list so an operator can read the band back rather than
 * simulating the evaluator in their head.
 */
export function describeBand(
    rules: ArgumentPolicyRule[],
    policySeverity: Severity,
): string[] {
    return rules
        .filter((r) => isComparisonRule(r) && r.field)
        .map((r) => {
            const predicate = r.deny_if || r.allow_only_if || '';
            const verb =
                {
                    field_gt: 'above',
                    field_gte: 'at or above',
                    field_lt: 'below',
                    field_lte: 'at or below',
                    field_eq: 'equal to',
                    field_ne: 'not equal to',
                    field_in: 'one of',
                    field_not_in: 'not one of',
                }[predicate] ?? predicate;
            const operand = PREDICATES_REQUIRING_FIELD.has(predicate) && r.value !== undefined
                ? operandText(r.value)
                : (r.values ?? []).map(operandText).join(', ');
            const severity = effectiveSeverity(r, policySeverity);
            const outcome =
                severity === 'deny'
                    ? 'refuse'
                    : severity === 'approval_required'
                      ? 'hold for a human'
                      : 'log only';
            return `${r.field} ${verb} ${operand} → ${outcome}`;
        });
}

/** Sort helper for presenting severities strictest-first. */
export function bySeverityStrictestFirst(a: Severity, b: Severity): number {
    return SEVERITY_RANK[b] - SEVERITY_RANK[a];
}

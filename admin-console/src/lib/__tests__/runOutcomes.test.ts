/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Declared outcomes — the pure half. The labels are pinned against the
 * spec's vocabularies (every error cause, every run class), the id grammar
 * against the wire grammar, and the catalogue validation against the control
 * plane's rules (`validate_catalogue`), case by case.
 */

import { describe, expect, it } from 'vitest';

import {
    CONFORMANCE_LABELS,
    ERROR_CAUSES,
    ERROR_CAUSE_HELP,
    ERROR_CAUSE_LABELS,
    RUN_CLASSES,
    RUN_CLASS_CLASSES,
    RUN_CLASS_COLORS,
    RUN_CLASS_HELP,
    RUN_CLASS_LABELS,
    catalogueIdChanges,
    catalogueServerErrors,
    catalogueToDrafts,
    cataloguesDiffer,
    capabilityList,
    draftsToCatalogue,
    emptyOutcomeDraft,
    emptyVariantDraft,
    driftOutcomeBucket,
    errorCauseLabel,
    expectSummary,
    outcomeTermKey,
    readCatalogue,
    splitEnvelopeTerm,
    findDeclared,
    isErrorCause,
    isRunClass,
    outcomeLabel,
    outcomeMixGroups,
    parseOutcomeId,
    parseQuietBound,
    quietBoundParts,
    reportableIds,
    runClassBreakdown,
    validateCatalogueDraft,
    type OutcomeDecl,
    type OutcomeDraft,
    type OutcomeRow,
    recentRunChipText,
} from '../runOutcomes';

/** The Brutor Demo screening agent's catalogue, normalised. */
const DEMO: OutcomeDecl[] = [
    {
        id: 'application_processed',
        label: 'Application processed',
        counts_toward_baseline: true,
        variants: [
            { id: 'loan_approved', label: 'Recommended: approve', expect: { requires_capabilities: ['applications_set_recommendation'] } },
            { id: 'loan_rejected', label: 'Recommended: decline' },
            { id: 'underwriter_decision_applied', label: 'Underwriter-approved decision applied', expect: { max_actions: 2, max_model_calls: 0 } },
        ],
    },
    {
        id: 'no_applications_available',
        label: 'Nothing to screen',
        counts_toward_baseline: false,
        expect: { max_model_calls: 0, max_cost_usd: 0 },
    },
];

describe('vocabularies', () => {
    it('labels every error cause in the spec vocabulary, in precedence order', () => {
        expect([...ERROR_CAUSES]).toEqual([
            'blocked_by_policy',
            'rate_limited',
            'model_unavailable',
            'model_rejected',
            'tool_failed',
            'skill_failed',
            'agent_unreachable',
            'delegate_deviated',
            'iteration_limit',
            'client_reported',
        ]);
        for (const c of ERROR_CAUSES) {
            expect(ERROR_CAUSE_LABELS[c].length).toBeGreaterThan(3);
            expect(ERROR_CAUSE_HELP[c].length).toBeGreaterThan(10);
            expect(errorCauseLabel(c)).toBe(ERROR_CAUSE_LABELS[c]);
            expect(isErrorCause(c)).toBe(true);
        }
        expect(errorCauseLabel('model_unavailable')).toBe('Model unavailable');
        expect(errorCauseLabel('blocked_by_policy')).toBe('Blocked by policy');
        expect(errorCauseLabel('delegate_deviated')).toBe('Delegate answered outside its contract');
    });

    it('labels the extra keys the health verdict and the error signal send', () => {
        expect(errorCauseLabel('unknown')).toBe('Cause not recorded');
        expect(errorCauseLabel('outcome_undeclared')).toBe('Ended in an undeclared outcome');
        expect(errorCauseLabel('outcome_unreported')).toBe('Reported no outcome');
        expect(errorCauseLabel(null)).toBe('—');
        expect(errorCauseLabel('')).toBe('—');
        // A cause this build does not know is humanised, never dropped.
        expect(errorCauseLabel('quota_exceeded')).toBe('Quota exceeded');
        expect(isErrorCause('quota_exceeded')).toBe(false);
    });

    it('labels every run class, and never paints no-work green', () => {
        expect(RUN_CLASSES).toEqual(['success', 'no_work', 'deviation', 'failed', 'not_decisive']);
        for (const c of RUN_CLASSES) {
            expect(RUN_CLASS_LABELS[c]).toBeTruthy();
            expect(RUN_CLASS_HELP[c].length).toBeGreaterThan(20);
            expect(isRunClass(c)).toBe(true);
        }
        expect(RUN_CLASS_LABELS.no_work).toBe('No work');
        expect(RUN_CLASS_CLASSES.no_work).not.toMatch(/green|emerald|lime/);
        expect(RUN_CLASS_CLASSES.no_work).toMatch(/slate/);
        expect(RUN_CLASS_CLASSES.success).toMatch(/emerald/);
        expect(RUN_CLASS_COLORS.no_work).not.toBe(RUN_CLASS_COLORS.success);
        expect(RUN_CLASS_COLORS.no_work).toBe('#94a3b8');
        expect(isRunClass('blocked')).toBe(false);
    });

    it('labels every conformance', () => {
        expect(CONFORMANCE_LABELS).toEqual({ declared: 'Declared', undeclared: 'Undeclared', unreported: 'Not reported' });
    });
});

describe('parseOutcomeId', () => {
    it('splits outcome and variant', () => {
        expect(parseOutcomeId('application_processed.loan_approved')).toEqual({
            outcome: 'application_processed',
            variant: 'loan_approved',
        });
        expect(parseOutcomeId('no_applications_available')).toEqual({ outcome: 'no_applications_available', variant: null });
        expect(parseOutcomeId('a-b_9')).toEqual({ outcome: 'a-b_9', variant: null });
    });

    it('refuses what the wire grammar refuses', () => {
        for (const bad of ['', 'Upper', 'a.b.c', 'a.', '.b', 'a b', 'x'.repeat(49), `a.${'y'.repeat(49)}`]) {
            expect(parseOutcomeId(bad)).toBeNull();
        }
        expect(parseOutcomeId(null)).toBeNull();
        expect(parseOutcomeId(undefined)).toBeNull();
        expect(parseOutcomeId('x'.repeat(48))).not.toBeNull();
    });
});

describe('findDeclared / reportableIds / outcomeLabel', () => {
    it('requires the variant when the outcome declares variants, and a bare id when it does not', () => {
        expect(findDeclared(DEMO, 'application_processed.loan_approved')?.variant?.id).toBe('loan_approved');
        expect(findDeclared(DEMO, 'application_processed')).toBeNull();
        expect(findDeclared(DEMO, 'application_processed.maybe')).toBeNull();
        expect(findDeclared(DEMO, 'no_applications_available')?.outcome.id).toBe('no_applications_available');
        expect(findDeclared(DEMO, 'no_applications_available.x')).toBeNull();
        expect(findDeclared(DEMO, 'unknown')).toBeNull();
        expect(findDeclared(null, 'unknown')).toBeNull();
    });

    it('lists every reportable id, sorted', () => {
        expect(reportableIds(DEMO)).toEqual([
            'application_processed.loan_approved',
            'application_processed.loan_rejected',
            'application_processed.underwriter_decision_applied',
            'no_applications_available',
        ]);
        expect(reportableIds(null)).toEqual([]);
    });

    it('labels a declared id from the catalogue and shows anything else verbatim', () => {
        expect(outcomeLabel('application_processed.loan_approved', DEMO)).toBe('Application processed · Recommended: approve');
        expect(outcomeLabel('no_applications_available', DEMO)).toBe('Nothing to screen');
        expect(outcomeLabel('rogue.thing', DEMO)).toBe('rogue.thing');
        expect(outcomeLabel(null, DEMO)).toBe('No outcome reported');
        expect(outcomeLabel('task_completed')).toBe('task_completed');
    });
});

describe('catalogue comparison', () => {
    it('names the ids the live catalogue adds and drops against the active one', () => {
        const live: OutcomeDecl[] = [
            { ...DEMO[0], variants: [...(DEMO[0].variants ?? []).slice(1), { id: 'loan_referred', label: 'Refer' }] },
            DEMO[1],
        ];
        expect(catalogueIdChanges(DEMO, live)).toEqual({
            added: ['application_processed.loan_referred'],
            removed: ['application_processed.loan_approved'],
        });
        expect(catalogueIdChanges(null, DEMO).added).toHaveLength(4);
        expect(catalogueIdChanges(DEMO, null).removed).toHaveLength(4);
    });

    it('differs on any change the contract carries, not on identity', () => {
        expect(cataloguesDiffer(DEMO, JSON.parse(JSON.stringify(DEMO)) as OutcomeDecl[])).toBe(false);
        expect(cataloguesDiffer(DEMO, [{ ...DEMO[0], counts_toward_baseline: false }, DEMO[1]])).toBe(true);
        expect(cataloguesDiffer(null, undefined)).toBe(false);
        expect(cataloguesDiffer(null, DEMO)).toBe(true);
    });
});

describe('outcomeMixGroups', () => {
    const row = (outcome_id: string | null, run_class: OutcomeRow['run_class'], runs: number, extra: Partial<OutcomeRow> = {}): OutcomeRow => ({
        outcome_id,
        run_class,
        runs,
        share: runs / 20,
        cost_usd: runs * 0.01,
        conformance: outcome_id ? 'declared' : 'unreported',
        counts_toward_baseline: true,
        ...extra,
    });

    it('groups variants under their outcome, largest first, unreported last', () => {
        const groups = outcomeMixGroups([
            row(null, 'deviation', 6),
            row('application_processed.loan_rejected', 'success', 2),
            row('no_applications_available', 'no_work', 5, { counts_toward_baseline: false }),
            row('application_processed.loan_approved', 'success', 4),
            row('application_processed.loan_approved', 'failed', 1),
        ]);
        expect(groups.map((g) => g.outcome)).toEqual(['application_processed', 'no_applications_available', null]);
        expect(groups[0].runs).toBe(7);
        expect(groups[0].share).toBeCloseTo(0.35);
        expect(groups[0].cost_usd).toBeCloseTo(0.07);
        expect(groups[0].rows.map((r) => `${r.outcome_id}:${r.run_class}`)).toEqual([
            'application_processed.loan_approved:success',
            'application_processed.loan_rejected:success',
            'application_processed.loan_approved:failed',
        ]);
        // Unreported runs are listed, not hidden, even when they are the largest.
        expect(groups[2].runs).toBe(6);
    });

    it('keeps a malformed reported id as its own visible group', () => {
        const groups = outcomeMixGroups([row('Not Valid!', 'deviation', 1, { conformance: 'undeclared' })]);
        expect(groups).toHaveLength(1);
        expect(groups[0].outcome).toBe('Not Valid!');
    });

    it('is empty for no rows', () => {
        expect(outcomeMixGroups(undefined)).toEqual([]);
    });
});

describe('runClassBreakdown', () => {
    it('lists every class in order with its share of all runs', () => {
        const b = runClassBreakdown({ success: 6, no_work: 10, failed: 4 });
        expect(b.map((x) => x.cls)).toEqual(RUN_CLASSES);
        expect(b.find((x) => x.cls === 'no_work')).toEqual({ cls: 'no_work', runs: 10, share: 0.5 });
        expect(b.find((x) => x.cls === 'deviation')?.runs).toBe(0);
        expect(runClassBreakdown(undefined).every((x) => x.share === 0)).toBe(true);
    });
});

describe('catalogue editor', () => {
    function draft(overrides: Partial<OutcomeDraft> = {}): OutcomeDraft {
        return { ...emptyOutcomeDraft(), id: 'task_completed', label: 'Task completed', ...overrides };
    }

    it('round-trips a catalogue through the form', () => {
        const drafts = catalogueToDrafts(DEMO);
        expect(drafts[0].variants[0].expect.requires_capabilities).toBe('applications_set_recommendation');
        expect(drafts[0].variants[2].expect.max_actions).toBe('2');
        expect(drafts[1].expect.max_cost_usd).toBe('0');
        expect(draftsToCatalogue(drafts)).toEqual([
            {
                id: 'application_processed',
                label: 'Application processed',
                counts_toward_baseline: true,
                variants: [
                    { id: 'loan_approved', label: 'Recommended: approve', expect: { requires_capabilities: ['applications_set_recommendation'] } },
                    { id: 'loan_rejected', label: 'Recommended: decline' },
                    { id: 'underwriter_decision_applied', label: 'Underwriter-approved decision applied', expect: { max_actions: 2, max_model_calls: 0 } },
                ],
            },
            {
                id: 'no_applications_available',
                label: 'Nothing to screen',
                counts_toward_baseline: false,
                expect: { max_model_calls: 0, max_cost_usd: 0 },
            },
        ]);
        expect(catalogueToDrafts(null)).toEqual([]);
    });

    it('trims text, drops empty optional members and parses capability lists', () => {
        const d = draft({
            id: ' task_completed ',
            label: '  Task completed ',
            description: '   ',
            expect: { max_actions: ' 3 ', max_model_calls: '', max_cost_usd: '0.05', requires_capabilities: 'a, b\nb', forbids_capabilities: ' , ' },
        });
        expect(draftsToCatalogue([d])).toEqual([
            {
                id: 'task_completed',
                label: 'Task completed',
                counts_toward_baseline: true,
                expect: { max_actions: 3, max_cost_usd: 0.05, requires_capabilities: ['a', 'b'] },
            },
        ]);
        expect(capabilityList('x,,y, x')).toEqual(['x', 'y']);
    });

    it('accepts a valid catalogue, including an empty one', () => {
        expect(validateCatalogueDraft(catalogueToDrafts(DEMO))).toEqual({});
        expect(validateCatalogueDraft([])).toEqual({});
    });

    it('mirrors the id rules: grammar and uniqueness, per catalogue and per outcome', () => {
        const e = validateCatalogueDraft([
            draft({ id: 'Bad Id' }),
            draft({ id: 'dup' }),
            draft({ id: 'dup', variants: [{ ...emptyVariantDraft(), id: 'v', label: 'V' }, { ...emptyVariantDraft(), id: 'v', label: 'V2' }, { ...emptyVariantDraft(), id: '', label: 'x' }] }),
        ]);
        expect(e['outcomes.0.id']).toBe('id must match [a-z0-9_-]{1,48}');
        expect(e['outcomes.1.id']).toBeUndefined();
        expect(e['outcomes.2.id']).toBe("duplicate outcome id 'dup'");
        expect(e['outcomes.2.variants.1.id']).toBe("duplicate variant id 'v'");
        expect(e['outcomes.2.variants.2.id']).toBe('id must match [a-z0-9_-]{1,48}');
        // The same variant id under two outcomes is fine.
        expect(
            validateCatalogueDraft([
                draft({ id: 'a', variants: [{ ...emptyVariantDraft(), id: 'v', label: 'V' }] }),
                draft({ id: 'b', variants: [{ ...emptyVariantDraft(), id: 'v', label: 'V' }] }),
            ]),
        ).toEqual({});
    });

    it('mirrors the label and description bounds', () => {
        const e = validateCatalogueDraft([
            draft({ id: 'a', label: '   ' }),
            draft({ id: 'b', label: 'x'.repeat(129) }),
            draft({ id: 'c', label: 'x'.repeat(128), description: 'd'.repeat(2001) }),
        ]);
        expect(e['outcomes.0.label']).toBe('label is required (1–128 characters)');
        expect(e['outcomes.1.label']).toBe('label must be at most 128 characters');
        expect(e['outcomes.2.label']).toBeUndefined();
        expect(e['outcomes.2.description']).toBe('description must be at most 2000 characters');
    });

    it('mirrors the size limits: 32 outcomes, 32 variants each', () => {
        const many = Array.from({ length: 33 }, (_, i) => draft({ id: `o${i}` }));
        expect(validateCatalogueDraft(many).outcomes).toBe('at most 32 outcomes');
        expect(validateCatalogueDraft(many.slice(0, 32)).outcomes).toBeUndefined();
        const variants = Array.from({ length: 33 }, (_, i) => ({ ...emptyVariantDraft(), id: `v${i}`, label: 'V' }));
        expect(validateCatalogueDraft([draft({ variants })])['outcomes.0.variants']).toBe('at most 32 variants per outcome');
    });

    it('mirrors the expect rules: whole maxima, a number for cost, no capability both required and forbidden', () => {
        const e = validateCatalogueDraft([
            draft({
                id: 'a',
                expect: { max_actions: '1.5', max_model_calls: '-1', max_cost_usd: 'cheap', requires_capabilities: 'x, y', forbids_capabilities: 'y, z, x' },
            }),
            draft({
                id: 'b',
                variants: [
                    {
                        ...emptyVariantDraft(),
                        id: 'v',
                        label: 'V',
                        expect: { max_actions: '2', max_model_calls: '0', max_cost_usd: '0.5', requires_capabilities: '', forbids_capabilities: '' },
                    },
                    { ...emptyVariantDraft(), id: 'w', label: 'W', expect: { ...emptyVariantDraft().expect, max_model_calls: 'x' } },
                ],
            }),
        ]);
        expect(e['outcomes.0.expect.max_actions']).toBe('max_actions must be a whole number ≥ 0');
        expect(e['outcomes.0.expect.max_model_calls']).toBe('max_model_calls must be a whole number ≥ 0');
        expect(e['outcomes.0.expect.max_cost_usd']).toBe('max_cost_usd must be a number ≥ 0');
        expect(e['outcomes.0.expect']).toBe('capabilities both required and forbidden: x, y');
        expect(e['outcomes.1.variants.0.expect.max_actions']).toBeUndefined();
        expect(e['outcomes.1.variants.1.expect.max_model_calls']).toBe('max_model_calls must be a whole number ≥ 0');
    });

    it('reads the server 422 back onto the form paths', () => {
        const err = {
            status: 422,
            details: {
                detail: [
                    { loc: ['outcomes', 0, 'variants', 1, 'id'], msg: "duplicate variant id 'v'" },
                    { loc: ['outcomes', 2, 'expect', 'max_actions'], msg: 'max_actions must be a whole number ≥ 0' },
                    { loc: ['body', 'outcomes'], msg: 'Input should be a valid list' },
                    { loc: ['outcomes', 2, 'expect', 'max_actions'], msg: 'second' },
                    { msg: 'no loc' },
                    'garbage',
                ],
            },
        };
        expect(catalogueServerErrors(err)).toEqual({
            'outcomes.0.variants.1.id': "duplicate variant id 'v'",
            'outcomes.2.expect.max_actions': 'max_actions must be a whole number ≥ 0; second',
            outcomes: 'Input should be a valid list',
        });
        expect(catalogueServerErrors({ validationErrors: [{ loc: ['outcomes', 0, 'label'], msg: 'required' }] })).toEqual({
            'outcomes.0.label': 'required',
        });
        expect(catalogueServerErrors(new Error('boom'))).toEqual({});
        expect(catalogueServerErrors(undefined)).toEqual({});
    });
});

describe('the no-processed-run bound', () => {
    it('parses a value and unit into seconds, empty clears', () => {
        expect(parseQuietBound('6', 'hours')).toEqual({ seconds: 21600 });
        expect(parseQuietBound('1.5', 'hours')).toEqual({ seconds: 5400 });
        expect(parseQuietBound('', 'days')).toEqual({ seconds: null });
        expect(parseQuietBound('  ', 'days')).toEqual({ seconds: null });
    });

    it('refuses what the control plane refuses (1 minute – 366 days)', () => {
        expect(parseQuietBound('30', 'minutes').error).toBeUndefined();
        expect(parseQuietBound('0.5', 'minutes').error).toBe('At least 1 minute.');
        expect(parseQuietBound('367', 'days').error).toBe('At most 366 days.');
        expect(parseQuietBound('366', 'days')).toEqual({ seconds: 366 * 86400 });
        expect(parseQuietBound('soon', 'hours').error).toBe('Enter a number.');
        expect(parseQuietBound('-2', 'hours').error).toBe('Enter a number.');
    });

    it('shows a stored bound in the largest unit that divides it', () => {
        expect(quietBoundParts(172800)).toEqual({ value: '2', unit: 'days' });
        expect(quietBoundParts(21600)).toEqual({ value: '6', unit: 'hours' });
        expect(quietBoundParts(5400)).toEqual({ value: '90', unit: 'minutes' });
        expect(quietBoundParts(null)).toEqual({ value: '', unit: 'hours' });
        expect(quietBoundParts(61)).toEqual({ value: '1', unit: 'minutes' });
    });
});

describe('readCatalogue (a sealed closure section)', () => {
    it('reads well-formed entries and drops what declares nothing', () => {
        const read = readCatalogue([
            {
                id: 'a', label: 'A', counts_toward_baseline: true, description: 'd',
                expect: { max_actions: 2, max_cost_usd: 0, requires_capabilities: ['x', 3], unknown: 1 },
                variants: [{ id: 'v', label: 'V', expect: { forbids_capabilities: ['y'] } }, { id: 'nolabel' }, 'junk'],
            },
            { id: 'b', label: 'B', counts_toward_baseline: 'yes' },
            { label: 'no id', counts_toward_baseline: true },
            null,
            { id: 'c', label: 'C', counts_toward_baseline: false, variants: [] },
        ]);
        expect(read).toEqual([
            {
                id: 'a', label: 'A', counts_toward_baseline: true, description: 'd',
                expect: { max_actions: 2, max_cost_usd: 0, requires_capabilities: ['x'] },
                variants: [{ id: 'v', label: 'V', description: null, expect: { forbids_capabilities: ['y'] } }],
            },
            { id: 'c', label: 'C', counts_toward_baseline: false, description: null, expect: null, variants: null },
        ]);
    });

    it('reads anything that is not a list as no catalogue', () => {
        expect(readCatalogue(null)).toEqual([]);
        expect(readCatalogue({ outcomes: [] })).toEqual([]);
        expect(readCatalogue(undefined)).toEqual([]);
    });
});

describe('expectSummary', () => {
    it('says each bound in words', () => {
        expect(
            expectSummary({ max_actions: 2, max_model_calls: 0, max_cost_usd: 0, requires_capabilities: ['a', 'b'], forbids_capabilities: ['c'] }),
        ).toBe('≤ 2 actions · no model calls · costs nothing · requires a, b · forbids c');
        expect(expectSummary({ max_actions: 1, max_model_calls: 1, max_cost_usd: 0.05 })).toBe('≤ 1 action · ≤ 1 model call · ≤ $0.05');
        expect(expectSummary({ max_actions: 0 })).toBe('no actions');
        expect(expectSummary(null)).toBe('');
        expect(expectSummary({})).toBe('');
    });
});

describe('envelope terms that name an outcome', () => {
    it('splits a key into its term and outcome', () => {
        expect(splitEnvelopeTerm('max_outcome_share:no_applications_available')).toEqual({
            base: 'max_outcome_share',
            outcome: 'no_applications_available',
        });
        expect(splitEnvelopeTerm('avg_cost_per_outcome:a.b')).toEqual({ base: 'avg_cost_per_outcome', outcome: 'a.b' });
        expect(splitEnvelopeTerm('max_error_rate')).toEqual({ base: 'max_error_rate', outcome: null });
    });

    it('builds a key only for a well-formed outcome id', () => {
        expect(outcomeTermKey('max_outcome_share', ' application_processed.loan_approved ')).toBe(
            'max_outcome_share:application_processed.loan_approved',
        );
        expect(outcomeTermKey('max_outcome_share', 'Bad Id')).toBeNull();
        expect(outcomeTermKey('max_outcome_share', '')).toBeNull();
        expect(outcomeTermKey('max_outcome_share', 'a.b.c')).toBeNull();
    });
});

describe('driftOutcomeBucket', () => {
    it('names an outcome bucket and nothing for the system-wide one', () => {
        expect(driftOutcomeBucket({ bucket: 'application_processed.loan_approved' })).toBe('application_processed.loan_approved');
        expect(driftOutcomeBucket({ bucket: 'all' })).toBeNull();
        expect(driftOutcomeBucket({})).toBeNull();
        expect(driftOutcomeBucket({ bucket: 3 })).toBeNull();
        expect(driftOutcomeBucket(null)).toBeNull();
    });
});

describe('recentRunChipText', () => {
    it('names the class and why a failed run failed', () => {
        expect(recentRunChipText({ terminal_state: 'errored', run_class: 'failed', error_cause: 'model_unavailable' }))
            .toMatch(/^Failed: /);
    });
    it('names the outcome a successful or no-work run ended in', () => {
        expect(recentRunChipText({ terminal_state: 'completed', run_class: 'success', outcome_id: 'application_processed.loan_approved' }))
            .toBe('Success: application_processed.loan_approved');
        expect(recentRunChipText({ terminal_state: 'completed', run_class: 'no_work', outcome_id: 'no_applications_available' }))
            .toBe('No work: no_applications_available');
    });
    it('says Deviation for an undeclared end, and falls back to the state without a class', () => {
        expect(recentRunChipText({ terminal_state: 'completed', run_class: 'deviation', outcome_id: 'made_up' })).toBe('Deviation');
        expect(recentRunChipText({ terminal_state: 'completed' })).toBe('completed');
    });
});

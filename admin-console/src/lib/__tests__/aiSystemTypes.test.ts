/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The severity stripe is the one colour every surface uses to judge a
 * system, so its ladder is worth pinning precisely: green is earned twice
 * (healthy AND assured, never averaged), and a learning system is never
 * green.
 */

import { describe, it, expect } from 'vitest';
import {
    AUTONOMY_LABELS,
    DRIFT_METRIC_EXPLAINERS,
    DRIFT_METRIC_LABELS,
    HEALTH_LABELS,
    TERMINAL_STATE_CLASSES,
    TERMINAL_STATE_LABELS,
    VERDICT_LABELS,
    assuranceStripe,
    driftMetricLabel,
    humanizeSeconds,
    describeRunTiming,
    envelopeTermStatusLabel,
    humanDurationMs,
} from '../aiSystemTypes';

describe('humanizeSeconds', () => {
    it('picks the largest unit that fits', () => {
        expect(humanizeSeconds(45)).toBe('45s');
        expect(humanizeSeconds(60)).toBe('1 min');
        expect(humanizeSeconds(3600)).toBe('1.0 hours');
        expect(humanizeSeconds(86400)).toBe('1.0 days');
    });

    it('rounds minutes and keeps one decimal on hours and days', () => {
        expect(humanizeSeconds(90)).toBe('2 min');
        expect(humanizeSeconds(5400)).toBe('1.5 hours');
        expect(humanizeSeconds(129600)).toBe('1.5 days');
    });

    it('renders an em dash for an unknown duration, but keeps a real zero', () => {
        expect(humanizeSeconds(null)).toBe('—');
        expect(humanizeSeconds(undefined)).toBe('—');
        expect(humanizeSeconds(0)).toBe('0s');
    });

    it('does not fall off the ladder just below each boundary', () => {
        expect(humanizeSeconds(59)).toBe('59s');
        expect(humanizeSeconds(3599)).toBe('60 min');
        expect(humanizeSeconds(86399)).toBe('24.0 hours');
    });
});

describe('assuranceStripe', () => {
    it('red for a contract violation or a suspended system, whatever else reads well', () => {
        expect(assuranceStripe('healthy', 'violations')).toBe('bg-red-500');
        expect(assuranceStripe('suspended', 'assured')).toBe('bg-red-500');
    });

    it('orange for the detector states — behaving differently, or stopped', () => {
        const detectors = ['drifting', 'stalled', 'silent'];
        expect(detectors.map((s) => assuranceStripe(s, 'assured'))).toEqual(
            detectors.map(() => 'bg-orange-500'),
        );
    });

    it('yellow for degraded health or a verdict with exceptions', () => {
        expect(assuranceStripe('degraded', 'assured')).toBe('bg-yellow-500');
        expect(assuranceStripe('healthy', 'assured_with_exceptions')).toBe('bg-yellow-500');
    });

    it('green only when health AND verdict both say so', () => {
        expect(assuranceStripe('healthy', 'assured')).toBe('bg-green-500');
        expect(assuranceStripe('healthy', 'insufficient_evidence')).not.toContain('green');
        expect(assuranceStripe('healthy', null)).not.toContain('green');
        expect(assuranceStripe(null, 'assured')).not.toContain('green');
    });

    it('never paints a learning system green', () => {
        expect(assuranceStripe('learning', 'assured')).toBe('bg-blue-400');
        expect(assuranceStripe('learning', null)).toBe('bg-blue-400');
    });

    it('greys out a system nothing has judged', () => {
        expect(assuranceStripe(null, null)).toContain('gray');
        expect(assuranceStripe(undefined, undefined)).toContain('gray');
        expect(assuranceStripe('unknown', 'insufficient_evidence')).toContain('gray');
    });

    it('ranks worse over better when health and verdict disagree', () => {
        // Verdict violations outrank a merely degraded health.
        expect(assuranceStripe('degraded', 'violations')).toBe('bg-red-500');
        // A drift detector outranks an exception-carrying verdict.
        expect(assuranceStripe('drifting', 'assured_with_exceptions')).toBe('bg-orange-500');
    });
});

describe('driftMetricLabel', () => {
    it('uses the known label for a known metric key', () => {
        const [key, label] = Object.entries(DRIFT_METRIC_LABELS)[0];
        expect(driftMetricLabel(key)).toBe(label);
    });

    it('names an operating-envelope breach by its term', () => {
        expect(driftMetricLabel('envelope:p95_latency_ms')).toBe(
            'Envelope bound: p95_latency_ms',
        );
    });

    it('humanises an unknown key rather than printing the raw token', () => {
        expect(driftMetricLabel('tool_call_entropy')).toBe('Tool call entropy');
        expect(driftMetricLabel('x')).toBe('X');
    });

    it('does not throw on an empty key', () => {
        expect(driftMetricLabel('')).toBe('');
    });

    it('prefers the envelope prefix over any table entry of the same name', () => {
        expect(driftMetricLabel('envelope:')).toBe('Envelope bound: ');
    });

    it('names a never-seen tool by the tool (shape finding)', () => {
        expect(driftMetricLabel('shape:new_tool:delete_customer')).toBe('New tool: delete_customer');
    });

    it('labels a never-seen tool combination without its hash', () => {
        expect(driftMetricLabel('shape:new_tool_set:9f2c1ab0')).toBe('New combination of tools');
    });

    it('labels a reach-limit breach without its rule id or hash', () => {
        expect(driftMetricLabel('reach:rrl-01ABC:deadbeef')).toBe('Record reach limit');
    });
});

describe('label tables', () => {
    it('labels every health state, verdict and autonomy level', () => {
        const unlabelled = [HEALTH_LABELS, VERDICT_LABELS, AUTONOMY_LABELS]
            .flatMap((table) => Object.entries(table))
            .filter(([, label]) => !label)
            .map(([key]) => key);
        expect(unlabelled).toEqual([]);
    });
});

describe('run timing (active vs awaiting a human)', () => {
    it('formats spans in their two largest units', () => {
        expect(humanDurationMs(850)).toBe('850 ms');
        expect(humanDurationMs(41_000)).toBe('41 s');
        expect(humanDurationMs(185_000)).toBe('3 m 05 s');
        expect(humanDurationMs(41_520_000)).toBe('11 h 32 m');
        expect(humanDurationMs(187_200_000)).toBe('2 d 4 h');
    });

    it('names the wait separately from the active time', () => {
        expect(
            describeRunTiming({ duration_ms: 41_561_000, active_duration_ms: 41_000, awaiting_human_ms: 41_520_000 }),
        ).toBe('active 41 s · awaited a person 11 h 32 m');
    });

    it('a run that never waited reads as active time only', () => {
        expect(describeRunTiming({ duration_ms: 41_000, active_duration_ms: 41_000, awaiting_human_ms: 0 })).toBe(
            'active 41 s',
        );
    });

    it('says nothing when the run has no duration', () => {
        expect(describeRunTiming({ duration_ms: null })).toBeNull();
    });
});

describe('outcome vocabularies', () => {
    it('has no blocked_policy terminal state — a gateway refusal is an errored run with a cause', () => {
        expect(Object.keys(TERMINAL_STATE_LABELS)).toEqual([
            'completed', 'completed_degraded', 'exhausted', 'errored', 'abandoned', 'cancelled',
        ]);
        expect(Object.keys(TERMINAL_STATE_CLASSES)).toEqual(Object.keys(TERMINAL_STATE_LABELS));
        // A completed execution is not automatically a success: not green.
        expect(TERMINAL_STATE_CLASSES.completed).not.toMatch(/emerald|green/);
    });

    it('names the two new drift metrics and explains them', () => {
        expect(DRIFT_METRIC_LABELS.outcome_mix).toBe('Outcome mix');
        expect(DRIFT_METRIC_LABELS.error_cause_mix).toBe('Error cause mix');
        expect(driftMetricLabel('outcome_mix')).toBe('Outcome mix');
        expect(DRIFT_METRIC_EXPLAINERS.outcome_mix).toMatch(/declared outcome/);
        expect(DRIFT_METRIC_EXPLAINERS.error_cause_mix).toMatch(/Why runs fail/);
        expect(DRIFT_METRIC_EXPLAINERS.terminal_state_mix).not.toMatch(/blocked/);
    });
});

describe('envelopeTermStatusLabel', () => {
    it('puts a report status of an envelope term in words', () => {
        expect(envelopeTermStatusLabel('met')).toBe('met');
        expect(envelopeTermStatusLabel('breached')).toBe('breached');
        expect(envelopeTermStatusLabel('insufficient_evidence')).toBe('insufficient evidence');
    });

    it('says an enforced term the report does not grade is enforced, not graded', () => {
        expect(envelopeTermStatusLabel('not_graded')).toBe('enforced, not graded');
    });

    it('shows a status it does not know as sent', () => {
        expect(envelopeTermStatusLabel('something_new')).toBe('something_new');
    });
});

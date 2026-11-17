/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The lifecycle rail may only offer the ONE step forward and the ONE step
 * back that the API actually allows. Offering a jump the control plane will
 * refuse turns the rail into a lie about what the operator may do.
 */

import { describe, it, expect } from 'vitest';
import {
    GATE_ACTIONS,
    REQUIREMENT_SHORT,
    STAGE_ORDER,
    backwardTransition,
    forwardTransition,
} from '../lifecycleGates';
import type { TransitionOption } from '../aiSystemTypes';

const opt = (to: string): TransitionOption => ({
    to_stage: to,
    allowed: true,
    requirements: [],
    unmet: [],
});

const all = STAGE_ORDER.map((s) => opt(s));

describe('forwardTransition', () => {
    it('returns the next stage in order, not any other offered stage', () => {
        expect(forwardTransition('proposed', all)?.to_stage).toBe('approved');
        expect(forwardTransition('active', all)?.to_stage).toBe('deprecated');
    });

    it('is undefined at the end of the ladder', () => {
        expect(forwardTransition('retired', all)).toBeUndefined();
    });

    it('is undefined when the API does not offer the next stage', () => {
        expect(forwardTransition('proposed', [opt('active'), opt('retired')])).toBeUndefined();
    });

    it('returns nothing for a missing or unknown stage', () => {
        expect(forwardTransition(null, all)).toBeUndefined();
        expect(forwardTransition(undefined, all)).toBeUndefined();
        expect(forwardTransition('', all)).toBeUndefined();
        expect(forwardTransition('archived', all)).toBeUndefined();
    });

    it('returns nothing when transitions have not loaded', () => {
        expect(forwardTransition('proposed', undefined)).toBeUndefined();
        expect(forwardTransition('proposed', [])).toBeUndefined();
    });
});

describe('backwardTransition', () => {
    it('returns the previous stage in order', () => {
        expect(backwardTransition('active', all)?.to_stage).toBe('approved');
        expect(backwardTransition('retired', all)?.to_stage).toBe('deprecated');
    });

    it('has nowhere to go back to from the first stage', () => {
        expect(backwardTransition('proposed', all)).toBeUndefined();
    });

    it('is undefined for an unknown stage rather than wrapping to the last one', () => {
        // indexOf returns -1; a naive `i - 1` would index the array's end.
        expect(backwardTransition('archived', all)).toBeUndefined();
    });

    it('is undefined when the API does not offer the previous stage', () => {
        expect(backwardTransition('active', [opt('deprecated')])).toBeUndefined();
    });

    it('returns nothing without a stage or transitions', () => {
        expect(backwardTransition(null, all)).toBeUndefined();
        expect(backwardTransition('active', undefined)).toBeUndefined();
    });
});

describe('gate vocabulary', () => {
    it('gives a button only to requirements one operation can satisfy', () => {
        // Waiting satisfies `no_recent_runs`; nothing an operator does
        // satisfies `valid_transition`. Neither may offer a button.
        expect(GATE_ACTIONS.no_recent_runs).toBeUndefined();
        expect(GATE_ACTIONS.valid_transition).toBeUndefined();
        expect(GATE_ACTIONS.replay_passed).toBe('Run replay');
    });

    it('has a short label for every requirement that has an action', () => {
        const unlabelled = Object.keys(GATE_ACTIONS).filter((k) => !REQUIREMENT_SHORT[k]);
        expect(unlabelled).toEqual([]);
    });

    it('orders the stages from proposal to retirement with no duplicates', () => {
        expect(STAGE_ORDER[0]).toBe('proposed');
        expect(STAGE_ORDER[STAGE_ORDER.length - 1]).toBe('retired');
        expect(new Set(STAGE_ORDER).size).toBe(STAGE_ORDER.length);
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The governance lifecycle's shared vocabulary: stage order, the short gate
 * labels the rail draws, and the operations that can open a gate. Kept out
 * of the diagram component so it can be imported by tests and other panels
 * without tripping fast-refresh.
 *
 * Requirement keys are the control plane's (`ai_system_lifecycle_gate.py`).
 */

import type { TransitionOption } from './aiSystemTypes';

export const STAGE_ORDER = ['proposed', 'approved', 'active', 'deprecated', 'retired'] as const;
export type Stage = (typeof STAGE_ORDER)[number];

/** Short labels for the diagram; the checklist below uses the control
 *  plane's full sentences. */
export const REQUIREMENT_SHORT: Record<string, string> = {
    contract_minted: 'contract minted',
    owner_set: 'owner set',
    intended_use_set: 'intended use',
    approver_recorded: 'approver recorded',
    liveness_expectation_set: 'liveness set',
    replay_passed: 'REPLAY PASSED',
    successor_named: 'successor named',
    no_recent_runs: 'no recent runs',
    no_unapproved_widening: 'no unapproved widening',
    human_approval: 'named approver',
    impact_assessment_on_file: 'impact assessment',
    valid_transition: 'valid transition',
};

/**
 * The operations that can open a gate, keyed by the requirement they satisfy.
 * Only requirements with one unambiguous fix get a button; `no_recent_runs`
 * is satisfied by waiting, and `valid_transition` by nothing an operator does.
 */
export const GATE_ACTIONS: Record<string, string> = {
    contract_minted: 'Mint contract',
    owner_set: 'Set owner',
    intended_use_set: 'Describe intended use',
    approver_recorded: 'Approve contract',
    liveness_expectation_set: 'Set liveness',
    replay_passed: 'Run replay',
    successor_named: 'Name successor',
    human_approval: 'Record approver',
    impact_assessment_on_file: 'Attach impact assessment',
    no_unapproved_widening: 'Review the contract',
};

/** The forward gate leaving the current stage, if the API offers it. */
export function forwardTransition(
    currentStage: string | null | undefined,
    transitions: TransitionOption[] | undefined,
): TransitionOption | undefined {
    if (!currentStage || !transitions) return undefined;
    const i = STAGE_ORDER.indexOf(currentStage as Stage);
    if (i < 0 || i + 1 >= STAGE_ORDER.length) return undefined;
    const next = STAGE_ORDER[i + 1];
    return transitions.find((t) => t.to_stage === next);
}

/** The always-allowed step back, if the API offers it. */
export function backwardTransition(
    currentStage: string | null | undefined,
    transitions: TransitionOption[] | undefined,
): TransitionOption | undefined {
    if (!currentStage || !transitions) return undefined;
    const i = STAGE_ORDER.indexOf(currentStage as Stage);
    if (i <= 0) return undefined;
    const prev = STAGE_ORDER[i - 1];
    return transitions.find((t) => t.to_stage === prev);
}

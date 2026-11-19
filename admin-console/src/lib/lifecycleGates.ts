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

import type { GateRequirement, TransitionOption } from './aiSystemTypes';
import { errorStatus, getArray, getBoolean, getRecord, getString, isRecord } from './narrow';

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
    workload_identity_enforced: 'workload identity',
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

/**
 * A requirement the gate re-adds after any tenant override of its policy,
 * as the gate vocabulary states it (`immutable_floors`): when it applies, in
 * words, and the requirement keys it re-adds.
 */
export interface GateFloor {
    when: string;
    alwaysRequired: string[];
}

/** The floors of a gate vocabulary answer; an entry that names no requirement is left out. */
export function gateFloorsOf(vocabulary: unknown): GateFloor[] {
    return (getArray(vocabulary, 'immutable_floors') ?? []).flatMap((floor): GateFloor[] => {
        const alwaysRequired = (getArray(floor, 'always_required') ?? []).filter((key): key is string => typeof key === 'string');
        if (alwaysRequired.length === 0) return [];
        const condition = getString(floor, 'condition') ?? '';
        const transitions = (getArray(floor, 'transitions') ?? []).filter((key): key is string => typeof key === 'string');
        const into = /^to_stage == '([a-z_]+)'$/.exec(condition)?.[1];
        const when =
            condition === "eu_ai_act_risk_tier == 'high'"
                ? 'On a high-risk system'
                : into
                  ? `On every way into ${into}${transitions.length ? ` (${transitions.join(', ')})` : ''}`
                  : `When ${condition || 'always'}`;
        return [{ when, alwaysRequired }];
    });
}

/**
 * The unmet requirements of a refused lifecycle move: the gate's decision
 * the Control Plane's 409 carries (`{detail: {message, decision: {unmet}}}`),
 * each requirement as the checklist reads it, its offending credentials
 * included. Empty for any other error.
 */
export function refusedRequirementsOf(error: unknown): GateRequirement[] {
    if (errorStatus(error) !== 409) return [];
    const decision = getRecord(getRecord(getRecord(error, 'details'), 'detail'), 'decision');
    return (getArray(decision, 'unmet') ?? []).flatMap((requirement): GateRequirement[] => {
        const key = getString(requirement, 'key');
        if (!isRecord(requirement) || key === undefined) return [];
        // The whole requirement is kept: its offending credentials are read from it where they are shown.
        return [{ ...requirement, key, label: getString(requirement, 'label') ?? key, satisfied: getBoolean(requirement, 'satisfied') ?? false, detail: getString(requirement, 'detail') ?? null }];
    });
}

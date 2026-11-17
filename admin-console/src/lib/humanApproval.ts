/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where an AI System is declared to wait for a person — the Composition
 * views' reading of the control plane's `human_approval` (derived from the
 * closure). One module, so the Simple graph, the Diagram and the List cannot
 * disagree.
 *
 * Declared, not observed: what actually happened is on the connection lines
 * (`held`, `held_lapsed`). A resource with no gate is not marked at all. No
 * gate may be the owner's deliberate choice, so nothing here reads it as a
 * fault, and nothing says "no human oversight".
 */

import type { AISystemCompositionView, HumanApprovalGate, HumanApprovalScope } from './aiSystemTypes';

/** The run overlay's glyph for a held call: a gate is where holds happen. */
export const HUMAN_APPROVAL_GLYPH = '⏸';

export const HUMAN_APPROVAL_META: Record<HumanApprovalScope, { label: string; meaning: string }> = {
    always: {
        label: 'Needs approval',
        meaning: 'A person must approve every matching call before it goes through.',
    },
    conditional: {
        label: 'Approval if args match',
        meaning: 'A person must approve only the calls whose arguments match a policy rule.',
    },
};

export interface NodeApproval {
    /** `always` when any gate always fires; a resource can carry both. */
    scope: HumanApprovalScope;
    /** Every gate on the node, one line each (the tooltip and the List). */
    details: string[];
    /** How long a person has to decide, when the system's chain states it. */
    windowSeconds: number | null;
}

function approvalOf(view: AISystemCompositionView, gates: HumanApprovalGate[]): NodeApproval | null {
    if (gates.length === 0) return null;
    return {
        scope: gates.some((g) => g.scope === 'always') ? 'always' : 'conditional',
        details: gates.map((g) => g.detail),
        windowSeconds: view.human_approval.approval_window_seconds,
    };
}

/** The gates on one drawn resource, by the ids it goes by. */
export function nodeApproval(view: AISystemCompositionView, targetIds: readonly string[]): NodeApproval | null {
    return approvalOf(view, view.human_approval.gates.filter((g) => g.resource_ids.some((id) => targetIds.includes(id))));
}

/** Gates that name nothing a bound resource reports (a tool no server lists).
 *  Real gates all the same, so they go on the lane rather than disappear. */
export function laneApproval(view: AISystemCompositionView, lane: string): NodeApproval | null {
    return approvalOf(view, view.human_approval.gates.filter((g) => g.lane === lane && g.resource_ids.length === 0));
}

export const AUTONOMY_APPROVAL_TEXT = 'Side-effecting agent calls need approval';

function windowText(seconds: number): string {
    if (seconds % 3600 === 0) return `${seconds / 3600} h`;
    if (seconds % 60 === 0) return `${seconds / 60} min`;
    return `${seconds} s`;
}

export function approvalTitle(approval: NodeApproval): string {
    const window = approval.windowSeconds ? [`A person has ${windowText(approval.windowSeconds)} to decide.`] : [];
    return [...approval.details, ...window].join('\n');
}

/** "2 of 9 resources" plus the system-wide rule; null when nothing is
 *  declared, so a system with no gate shows no fact at all. */
export function humanApprovalSummary(
    view: AISystemCompositionView,
    gatedResources: number,
    totalResources: number
): string | null {
    const unplaced = view.human_approval.gates.some((g) => g.resource_ids.length === 0);
    const parts: string[] = [];
    if (gatedResources > 0) parts.push(`${gatedResources} of ${totalResources} resources`);
    else if (unplaced) parts.push('declared on tools no bound server reports');
    if (view.human_approval.autonomy_requires_approval) parts.push('side-effecting agent calls');
    return parts.length ? parts.join(' · ') : null;
}

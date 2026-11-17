/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * "N argument policies also gate this tool".
 *
 * Capability filters and argument policies are separate surfaces that meet
 * at one decision. A filter is the unconditional switch: this tool always
 * needs a human, or never runs. An argument policy is conditional: only
 * above a threshold, only for these tables, only outside this root.
 *
 * An operator setting unconditional approval on a tool has to be able to see
 * the conditional rules that also apply to it, or the two surfaces disagree
 * silently — someone loosens the filter, the band still bites, and nobody
 * can find why. Read-only on purpose: this is a pointer to the other screen,
 * not a second place to edit the same rule.
 *
 * The badge reads nothing itself: the dialog reads the policies once and
 * hands every row the same answer.
 */

import React from 'react';
import { Scale } from 'lucide-react';

import type { ArgumentPolicy } from '../../../lib/argumentPolicyTypes';
import { policyAppliesToGroup, policyTargets } from '../../../lib/argumentPolicyGates';

interface Props {
    /** The capability whose gates we are summarising. */
    capabilityName: string;
    /** Group the filter dialog is scoped to. */
    groupId: string;
    /**
     * The argument policies of the tool-input surface, as the dialog read
     * them for all of its rows; null while they are not known (not answered
     * yet, or the read failed: the dialog says that once, above the rows).
     */
    policies: readonly ArgumentPolicy[] | null;
}

export const ArgumentPolicyGateBadge: React.FC<Props> = ({ capabilityName, groupId, policies }) => {
    // Unknown is not "none": nothing is said of a row until the policies are known.
    if (policies === null) return null;
    const matches = policies.filter((p) => policyTargets(p, capabilityName) && policyAppliesToGroup(p, groupId));
    if (matches.length === 0) return null;

    const names = matches.map((p) => p.name).join(', ');
    return (
        <span
            className="inline-flex items-center gap-1 rounded-full bg-surface-alt px-2 py-0.5 text-[11px] text-secondary"
            title={`Also gated by: ${names}. Conditional rules live on the group's Argument Policies tab.`}
            data-testid={`argument-policy-gate-${capabilityName}`}
        >
            <Scale className="h-3 w-3" />
            {matches.length === 1
                ? '1 argument policy also gates this'
                : `${matches.length} argument policies also gate this`}
        </span>
    );
};

export default ArgumentPolicyGateBadge;

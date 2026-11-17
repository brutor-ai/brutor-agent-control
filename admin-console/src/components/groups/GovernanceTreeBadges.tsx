/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The two small marks on a group in the hierarchy tree: does this group set
 * limits and policy of its own, and is it bound by something above it.
 *
 * The tree is where an operator forms their mental model of an estate;
 * without these marks a department that caps every model beneath it looks
 * exactly like one that governs nothing.
 * A filled mark means this group carries a document of its own on some
 * surface; an outline means it only inherits; nothing means it is
 * ungoverned on that axis, which is itself worth seeing at a glance.
 *
 * Deliberately two marks, not four: the tree answers "where does governance
 * live", and the detail tabs answer "what does it say".
 */

import React from 'react';
import { Gauge, Scale } from 'lucide-react';

export interface GovernanceMarks {
    sets_own_limits?: boolean;
    sets_own_policy?: boolean;
    inherits_limits?: boolean;
    inherits_policy?: boolean;
}

type Standing = 'own' | 'inherited' | 'none';

function standing(own?: boolean, inherited?: boolean): Standing {
    if (own) return 'own';
    if (inherited) return 'inherited';
    return 'none';
}

const TITLES: Record<Standing, (axis: string) => string> = {
    own: (axis) => `Sets its own ${axis}`,
    inherited: (axis) => `Inherits ${axis} from a parent group`,
    none: (axis) => `No ${axis} apply`,
};

interface MarkProps {
    axis: string;
    state: Standing;
    Icon: React.ComponentType<{ className?: string }>;
    testId: string;
}

const Mark: React.FC<MarkProps> = ({ axis, state, Icon, testId }) => {
    if (state === 'none') return null;
    return (
        <span
            className={`ml-1 flex-shrink-0 ${
                state === 'own' ? 'text-primary-600' : 'text-muted opacity-60'
            }`}
            title={TITLES[state](axis)}
            data-testid={`${testId}-${state}`}
            aria-label={TITLES[state](axis)}
        >
            <Icon className="w-3 h-3" />
        </span>
    );
};

export const GovernanceTreeBadges: React.FC<{ node: GovernanceMarks }> = ({ node }) => (
    <>
        <Mark
            axis="limits"
            state={standing(node.sets_own_limits, node.inherits_limits)}
            Icon={Gauge}
            testId="tree-limits"
        />
        <Mark
            axis="policy"
            state={standing(node.sets_own_policy, node.inherits_policy)}
            Icon={Scale}
            testId="tree-policy"
        />
    </>
);

export default GovernanceTreeBadges;

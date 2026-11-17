/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The banner that appears when a group's policy form holds a value looser
 * than the policy it inherits, naming each offending field.
 *
 * Every governance tab (LLM, MCP, Skill) blocks Save on the same
 * condition, so the explanation lives in one place. The wording says what
 * is wrong and why saving is refused: the ancestor's stricter value
 * applies regardless, so storing the looser one would record a decision
 * the platform does not honour.
 */

import React from 'react';
import type { GovernanceRelaxation } from '../../../lib/governanceInheritance';

interface Props {
    relaxations: GovernanceRelaxation[];
}

export const GovernanceRelaxationNotice: React.FC<Props> = ({ relaxations }) => {
    if (relaxations.length === 0) return null;
    return (
        <div
            className="rounded-lg border border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/40 p-4"
            role="alert"
            data-testid="governance-relaxations"
        >
            <p className="text-sm font-medium text-red-800 dark:text-red-300">
                {relaxations.length === 1
                    ? 'One field is looser than the policy this group inherits.'
                    : `${relaxations.length} fields are looser than the policy this group inherits.`}{' '}
                Saving is blocked until they are equal to or stricter than the inherited value.
            </p>
            <ul className="mt-2 space-y-0.5 text-sm text-red-700 dark:text-red-300 list-disc pl-5">
                {relaxations.map((r) => (
                    <li key={r.path}>
                        <span className="font-medium">{r.label}</span>{' '}
                        — {r.message.replace(' — the save will be rejected', '')}
                    </li>
                ))}
            </ul>
        </div>
    );
};

export default GovernanceRelaxationNotice;

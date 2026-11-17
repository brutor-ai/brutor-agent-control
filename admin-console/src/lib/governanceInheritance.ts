/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Which governance values a child group may set, given what it inherits.
 *
 * Governance merges most-restrictive-wins at request time, so a child value
 * looser than its ancestors' changes nothing about enforcement. That is
 * exactly why it must be refused rather than stored: a saved policy the
 * runtime discards reads like a decision on the group's page, travels into
 * exports and sealed contracts, and becomes real the day an ancestor
 * relaxes its own value.
 *
 * The control plane refuses these on PATCH
 * (`_validate_governance_not_less_restrictive`). This table is its mirror,
 * so the console can say so while the operator types instead of after they
 * save. The two must stay in step — the rules and orderings below are
 * copied field for field from the `*_restrictive_merge` functions.
 *
 * Fields that merge by UNION (blocked tools, banned patterns, mandatory
 * prompt fragments) are deliberately absent: a child can only add to them,
 * so no child value can be a relaxation.
 */

export type GovernanceSurface = 'llm' | 'mcp' | 'skill';

type RuleKind = 'min_wins' | 'strictness' | 'bool_and' | 'list_subset';

interface GovernanceRule {
    /** Dotted path; a single segment means the blob is flat (MCP, skill). */
    path: string;
    kind: RuleKind;
    /** Strictness ordering — higher is stricter. */
    ordering?: Record<string, number>;
    label: string;
}

const ENFORCE_STRICTNESS: Record<string, number> = { clamp: 0, reject: 1 };
const ACCESS_STRICTNESS: Record<string, number> = {
    enabled: 0,
    approval_required: 1,
    disabled: 2,
};
/** A model's tool use is allowed or refused: nothing is held for a person on it. */
const MODEL_TOOL_ACCESS_STRICTNESS: Record<string, number> = {
    enabled: 0,
    disabled: 1,
};
const SKILL_EXECUTION_STRICTNESS: Record<string, number> = {
    unrestricted: 0,
    approval_required: 1,
    disabled: 2,
};

export const GOVERNANCE_RULES: Record<GovernanceSurface, GovernanceRule[]> = {
    llm: [
        { path: 'temperature.max', kind: 'min_wins', label: 'Max Temperature' },
        { path: 'temperature.enforce', kind: 'strictness', ordering: ENFORCE_STRICTNESS, label: 'Enforce Mode' },
        { path: 'context_window.max_input_tokens', kind: 'min_wins', label: 'Max Input Tokens' },
        { path: 'context_window.max_output_tokens', kind: 'min_wins', label: 'Max Output Tokens' },
        { path: 'context_window.enforce', kind: 'strictness', ordering: ENFORCE_STRICTNESS, label: 'Enforce Mode' },
        { path: 'tool_use.access', kind: 'strictness', ordering: MODEL_TOOL_ACCESS_STRICTNESS, label: 'Tool Access' },
        { path: 'tool_use.max_tool_calls_per_request', kind: 'min_wins', label: 'Max Tool Calls per Request' },
        { path: 'tool_use.max_tool_calls_per_hour', kind: 'min_wins', label: 'Max Tool Calls per Hour' },
        { path: 'tool_use.max_tool_calls_per_day', kind: 'min_wins', label: 'Max Tool Calls per Day' },
        { path: 'tool_use.allowed_tools', kind: 'list_subset', label: 'Allowed Tools' },
    ],
    mcp: [
        { path: 'default_tool_access', kind: 'strictness', ordering: ACCESS_STRICTNESS, label: 'Default Tool Access' },
        { path: 'default_resource_access', kind: 'strictness', ordering: ACCESS_STRICTNESS, label: 'Default Resource Access' },
        { path: 'default_prompt_access', kind: 'strictness', ordering: ACCESS_STRICTNESS, label: 'Default Prompt Access' },
        { path: 'default_resource_template_access', kind: 'strictness', ordering: ACCESS_STRICTNESS, label: 'Default Resource Template Access' },
        { path: 'approval_timeout_seconds', kind: 'min_wins', label: 'Approval Timeout' },
    ],
    skill: [
        { path: 'execution_mode', kind: 'strictness', ordering: SKILL_EXECUTION_STRICTNESS, label: 'Execution Mode' },
        { path: 'discovery_enabled', kind: 'bool_and', label: 'Skill Discovery' },
        { path: 'approval_timeout_seconds', kind: 'min_wins', label: 'Approval Timeout' },
        { path: 'max_concurrent_executions', kind: 'min_wins', label: 'Max Concurrent Executions' },
        { path: 'max_tool_calls_per_execution', kind: 'min_wins', label: 'Max Tool Calls per Execution' },
    ],
};

export interface GovernanceRelaxation {
    /** Dotted path, so a form can mark the exact field. */
    path: string;
    label: string;
    /** One sentence an operator can act on. */
    message: string;
}

function dottedGet(blob: unknown, path: string): unknown {
    let node: unknown = blob;
    for (const segment of path.split('.')) {
        if (node === null || typeof node !== 'object') return undefined;
        node = (node as Record<string, unknown>)[segment];
    }
    return node;
}

/**
 * Every value in `value` that is looser than the inherited policy. Empty
 * when there is nothing to inherit, or when every field only tightens.
 */
export function governanceRelaxations(
    value: unknown,
    inherited: unknown,
    surface: GovernanceSurface,
): GovernanceRelaxation[] {
    if (!value || !inherited) return [];

    const found: GovernanceRelaxation[] = [];
    for (const rule of GOVERNANCE_RULES[surface]) {
        const next = dottedGet(value, rule.path);
        const floor = dottedGet(inherited, rule.path);
        if (next === undefined || next === null || floor === undefined || floor === null) continue;

        switch (rule.kind) {
            case 'min_wins': {
                if (typeof next !== 'number' || typeof floor !== 'number') break;
                if (Number.isNaN(next)) break;
                if (next > floor) {
                    found.push({
                        path: rule.path,
                        label: rule.label,
                        message: `Above the inherited maximum of ${floor} — the save will be rejected`,
                    });
                }
                break;
            }
            case 'strictness': {
                // Strictness tokens are always strings on the wire. Anything
                // else cannot be ranked, and coercing it would rank it 0 —
                // i.e. flag a garbage value as a deliberate relaxation.
                if (typeof next !== 'string' || typeof floor !== 'string') break;
                const order = rule.ordering ?? {};
                if ((order[next] ?? 0) < (order[floor] ?? 0)) {
                    found.push({
                        path: rule.path,
                        label: rule.label,
                        message: `Looser than the inherited "${floor.replace(/_/g, ' ')}" — the save will be rejected`,
                    });
                }
                break;
            }
            case 'bool_and': {
                if (floor === false && next === true) {
                    found.push({
                        path: rule.path,
                        label: rule.label,
                        message: 'Disabled by a parent group and cannot be re-enabled here',
                    });
                }
                break;
            }
            case 'list_subset': {
                if (!Array.isArray(next) || !Array.isArray(floor)) break;
                const extra = next.filter((t) => !floor.includes(t));
                if (extra.length) {
                    found.push({
                        path: rule.path,
                        label: rule.label,
                        message: `${extra.join(', ')} not allowed by the inherited policy — the save will be rejected`,
                    });
                }
                break;
            }
        }
    }
    return found;
}

/** Index by dotted path, for a form that renders field by field. */
export function relaxationsByPath(
    relaxations: GovernanceRelaxation[],
): Record<string, GovernanceRelaxation> {
    const out: Record<string, GovernanceRelaxation> = {};
    for (const r of relaxations) out[r.path] = r;
    return out;
}

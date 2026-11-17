/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Argument Policy types — mirrors the control-plane Pydantic shape
 * in `app/api/v1/models/admin/argument_policy.py`.
 *
 * Keep PREDICATES_BY_ANALYZER in sync with the Python registry of
 * the same name (and the Rust evaluator's predicate match arms).
 */

export type Surface =
    | 'llm_output_tool_calls'
    | 'mcp_input'
    | 'skill_input';

export type Analyzer = 'sql' | 'url' | 'shell' | 'path' | 'json';

/**
 * How hard a tripped rule bites.
 *
 * `approval_required` is the band between the other two: the call is neither
 * allowed nor refused, it is held for a human through the approval queue.
 * Without it a threshold policy has to be a hard block, which kills the
 * agent above the threshold rather than supervising it.
 */
export type Severity = 'deny' | 'approval_required' | 'warn';

/**
 * The surface of a model's tool calls. The gateway holds nothing for a person
 * there, so neither a policy nor a rule on it may ask for an approval: the
 * Control Plane refuses one.
 */
export const SURFACE_MODEL_TOOL_CALLS: Surface = 'llm_output_tool_calls';

/** The severities each surface takes, as the Control Plane answers them with the list (`severities_by_surface`). */
export type SeveritiesBySurface = Partial<Record<Surface, readonly Severity[]>>;

/**
 * The severities a policy, or a rule, on `surface` may have: those the
 * Control Plane answers for it. A surface it answered none for is offered
 * the two every surface takes, deny and warn.
 */
export function severitiesOn(surface: Surface, bySurface: SeveritiesBySurface): readonly Severity[] {
    return bySurface[surface] ?? ['deny', 'warn'];
}

/**
 * What a policy's own severity may be: what a tripped rule does when it
 * carries no severity of its own. Any of the three. A rule that guards a
 * compared field must deny, by its own severity or by the policy's.
 */
export type PolicySeverity = Severity;

/**
 * How much of the arguments of one call the gateway's JSON analysis reads:
 * what a policy that states no limit is analysed under, and the most a
 * policy may state in its analyzer config. A call beyond a policy's limits
 * is refused when the policy applies to it, whatever the rules say.
 */
export const ANALYSIS_LIMITS = [
    { key: 'max_leaves', label: 'Most values', help: 'Most values in the arguments of one call', byDefault: 512, most: 4096 },
    { key: 'max_depth', label: 'Most levels of nesting', help: 'Most levels of nesting', byDefault: 8, most: 32 },
    { key: 'max_array_elements', label: 'Most items of a list', help: 'Most items of a list', byDefault: 64, most: 1024 },
] as const;

export type AnalysisLimitKey = (typeof ANALYSIS_LIMITS)[number]['key'];

/** Strictness order, matching the Rust `Severity::rank`. */
export const SEVERITY_RANK: Record<Severity, number> = {
    warn: 1,
    approval_required: 2,
    deny: 3,
};

export const SEVERITY_LABEL: Record<Severity, string> = {
    deny: 'Deny — refuse the call',
    approval_required: 'Approval required — hold for a human',
    warn: 'Warn — log only, do not block',
};

export interface ArgumentPolicyRule {
    deny_if?: string;
    allow_only_if?: string;
    values?: unknown[];
    /** Dotted path inside the argument object, e.g. `amount`, `transfer.amount`. */
    field?: string;
    /** Operand the comparison predicates test `field` against. */
    value?: unknown;
    /**
     * Overrides the policy severity for this rule only. This is what makes a
     * BAND expressible in one policy: rules are evaluated in order and the
     * first match wins, so ordering is the band.
     */
    severity?: Severity;
    message?: string;
}

export interface ArgumentPolicyGroupInfo {
    id: string;
    name: string;
    // Optional, not nullable — matches the `GroupInfo` shape that
    // GroupTreeDisplay accepts (it uses `string | undefined`). The
    // API sends JSON null when unset; coerce on read or treat as
    // undefined via `?? undefined`. We use `?` so the field is
    // structurally compatible with both.
    display_name?: string;
    parent_group_id?: string;
    is_active?: boolean;
}

export interface ArgumentPolicy {
    id: string;
    tenant_id: string;
    name: string;
    description?: string | null;
    surface: Surface;
    target: string;
    argument_key: string;
    analyzer: Analyzer;
    analyzer_config?: Record<string, unknown> | null;
    rules: ArgumentPolicyRule[];
    enabled: boolean;
    severity: PolicySeverity;
    group_ids: string[];
    // Populated by the API for admin-UI rendering (GroupTreeDisplay);
    // the contract makes it optional, and an absent one reads as [].
    groups?: ArgumentPolicyGroupInfo[];
    created_at?: string | null;
    updated_at?: string | null;
}

export interface ArgumentPolicyCreateRequest {
    name: string;
    description?: string | null;
    surface: Surface;
    target: string;
    argument_key: string;
    analyzer: Analyzer;
    analyzer_config?: Record<string, unknown> | null;
    rules: ArgumentPolicyRule[];
    enabled?: boolean;
    severity?: PolicySeverity;
    group_ids?: string[];
}

/**
 * Per-analyzer allowed predicates. Drives the UI's deny_if/allow_only_if
 * dropdown so operators can't pick a predicate the server will reject.
 */
export const PREDICATES_BY_ANALYZER: Record<Analyzer, string[]> = {
    sql: [
        'writes',
        'statement_type_in',
        'statement_type_not_in',
        'touches_any',
        'touches_none_of',
        'subquery_depth_gt',
        'multi_statement',
    ],
    url: [
        'host_in_private_range',
        'scheme_not_in',
        'scheme_in',
        'host_in',
        'host_not_in',
    ],
    shell: [
        'command_in',
        'command_not_in',
        'flag_present',
        'flag_not_present',
    ],
    path: [
        'traverses_parent',
        'absolute_required',
        'inside_root',
        'outside_root',
    ],
    json: [
        'schema_invalid',
        'field_present',
        'field_not_present',
        // The closed comparison set. Must stay in step with
        // PREDICATES_BY_ANALYZER in the control plane and the Rust
        // evaluator: adding a predicate touches all three.
        'field_gt',
        'field_gte',
        'field_lt',
        'field_lte',
        'field_eq',
        'field_ne',
        'field_in',
        'field_not_in',
    ],
};

/** Predicates that read a dotted `field` path rather than the whole value. */
export const PREDICATES_REQUIRING_FIELD: Set<string> = new Set([
    'field_gt',
    'field_gte',
    'field_lt',
    'field_lte',
    'field_eq',
    'field_ne',
    'field_in',
    'field_not_in',
]);

/** Of those, the ones whose operand must be a number. */
export const PREDICATES_REQUIRING_NUMERIC_VALUE: Set<string> = new Set([
    'field_gt',
    'field_gte',
    'field_lt',
    'field_lte',
]);

/** And the ones that take a list in `values` instead of a scalar `value`. */
export const PREDICATES_REQUIRING_LIST_VALUES: Set<string> = new Set([
    'field_in',
    'field_not_in',
]);

export const PREDICATES_REQUIRING_VALUES: Set<string> = new Set([
    'statement_type_in',
    'statement_type_not_in',
    'touches_any',
    'touches_none_of',
    'subquery_depth_gt',
    'scheme_not_in',
    'scheme_in',
    'host_in',
    'host_not_in',
    'command_in',
    'command_not_in',
    'flag_present',
    'flag_not_present',
    'inside_root',
    'outside_root',
    'field_present',
    'field_not_present',
]);

/** Per-predicate human-friendly description for the picker. */
export const PREDICATE_LABEL: Record<string, string> = {
    writes: 'Statement writes (INSERT/UPDATE/DELETE/DDL)',
    statement_type_in: 'Statement type is one of (allowlist)',
    statement_type_not_in: 'Statement type is NOT one of (blocklist)',
    touches_any: 'Touches any of these tables',
    touches_none_of: 'Touches a table NOT in this list',
    subquery_depth_gt: 'Subquery depth greater than',
    multi_statement: 'Payload contains multiple statements (;)',

    host_in_private_range:
        'Host is private / loopback / link-local / metadata IP (SSRF)',
    scheme_not_in: 'Scheme is NOT in this allowlist',
    scheme_in: 'Scheme is in this allowlist',
    host_in: 'Host is in this allowlist',
    host_not_in: 'Host is NOT in this allowlist',

    command_in: 'Command is in this allowlist',
    command_not_in: 'Command is NOT in this allowlist',
    flag_present: 'Any of these flags present',
    flag_not_present: 'None of these flags present',

    traverses_parent: 'Path contains .. traversal',
    absolute_required: 'Path is absolute',
    inside_root: 'Path is inside one of these roots',
    outside_root: 'Path is outside all of these roots',

    schema_invalid: 'JSON Schema validation failed',
    field_gt: 'Field is greater than',
    field_gte: 'Field is greater than or equal to',
    field_lt: 'Field is less than',
    field_lte: 'Field is less than or equal to',
    field_eq: 'Field equals',
    field_ne: 'Field does not equal',
    field_in: 'Field is one of',
    field_not_in: 'Field is NOT one of',
    field_present: 'Any of these top-level fields present',
    field_not_present: 'None of these top-level fields present',
};

export const SURFACE_LABEL: Record<Surface, string> = {
    llm_output_tool_calls: 'LLM tool_calls',
    mcp_input: 'MCP inbound tool/call',
    skill_input: 'Skill execution input',
};

export const ANALYZER_LABEL: Record<Analyzer, string> = {
    sql: 'SQL',
    url: 'URL',
    shell: 'Shell command',
    path: 'Filesystem path',
    json: 'JSON',
};

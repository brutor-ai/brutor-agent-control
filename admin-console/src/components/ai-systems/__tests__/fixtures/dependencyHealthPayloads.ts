/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Dependency-health payloads, shaped exactly as the control plane returns
 * them (`ai_system_dependency_health.dependency_health`, the health
 * endpoint's `dependency_cap`, the report's `service` section, the diff
 * engine's `dependency_contract_changed` entry and the closure's
 * `dependencies` section).
 */

import type {
    ClosureDependency,
    DependencyCap,
    DependencyHealthEntry,
    DiffEntry,
    ServiceFacts,
    SystemHealth,
} from '../../../../lib/aiSystemTypes';

/** A critical shared service failing on liveness: caps the root. */
export const SCORING_CAPPING: DependencyHealthEntry = {
    edge_id: 'dep-scoring',
    system_id: 'sys-scoring',
    name: 'Scoring Service',
    role: 'shared_service',
    criticality: 'critical',
    contributes_to_decision: true,
    status: 'degraded',
    failing_signal: 'liveness',
    via_system_id: null,
    via_system_name: null,
    basis: 'tested',
    caps_root: true,
    note: null,
    fallback_check: null,
    boundary: null,
    assumptions: null,
};

/** A degradable shared service whose fallback did not hold. */
export const ADDRESS_DEGRADABLE: DependencyHealthEntry = {
    edge_id: 'dep-address',
    system_id: 'sys-address',
    name: 'Address Validator',
    role: 'shared_service',
    criticality: 'degradable',
    contributes_to_decision: false,
    status: 'degraded',
    failing_signal: 'dependency',
    via_system_id: 'sys-geo',
    via_system_name: 'Geo Lookup',
    basis: 'tested',
    caps_root: false,
    note: "Degradable — declared fallback: 'Accept the applicant-supplied address'. The fallback did NOT hold: 1 of 3 unanswered call(s) left the run incomplete.",
    fallback_check: { unanswered_calls: 3, root_runs_completed: 2, held: false },
    boundary: null,
    assumptions: null,
};

/** An external agent: assumed, boundary metrics and assumptions only. */
export const BUREAU_EXTERNAL: DependencyHealthEntry = {
    edge_id: 'dep-bureau',
    system_id: null,
    name: 'Credit Bureau Agent',
    role: 'external',
    criticality: 'critical',
    contributes_to_decision: true,
    status: 'assumed',
    failing_signal: null,
    via_system_id: null,
    via_system_name: null,
    basis: 'assumed',
    caps_root: false,
    note: "Assumed, not tested: Brutor sees only the boundary of this third-party agent; the root's declared assumptions stand in for evidence.",
    fallback_check: null,
    boundary: { calls: 20, errors: 2, error_rate: 0.1, p95_latency_ms: 840, last_seen_at: null },
    assumptions: { expected_availability_pct: 99.5, data_classes_shared: ['personal_data'] },
};

export const SCORING_CAP: DependencyCap = {
    reason: 'dependency',
    edge_id: 'dep-scoring',
    system_id: 'sys-scoring',
    name: 'Scoring Service',
    failing_signal: 'liveness',
};

export const DEPENDENCY_HEALTH: DependencyHealthEntry[] = [
    ADDRESS_DEGRADABLE,
    BUREAU_EXTERNAL,
    SCORING_CAPPING,
];

/** The root: every own signal fine, capped at degraded by Scoring Service. */
export function cappedRootHealth(overrides: Partial<SystemHealth> = {}): SystemHealth {
    return {
        ai_system_id: 'sys-demo',
        health_state: 'degraded',
        health_score: 0.95,
        liveness_score: 1,
        behaviour_score: 1,
        reliability_score: 0.95,
        cost_score: 1,
        conformance_score: 1,
        oversight_score: 1,
        runs_24h: 40,
        open_drift_events: 0,
        reasons: [
            'Critical dependency Scoring Service is not healthy (liveness) — this system cannot be healthier than a dependency it cannot work without.',
        ],
        is_trustworthy: true,
        computed_at: new Date().toISOString(),
        signal_levels: {
            liveness: { score: 1, level: 'enforced' },
            behaviour: { score: 1, level: 'enforced' },
            reliability: { score: 0.95, level: 'enforced' },
            cost: { score: 1, level: 'enforced' },
            conformance: { score: 1, level: 'enforced' },
            oversight: { score: 1, level: 'enforced' },
            review: { score: 1, level: 'enforced' },
        },
        signal_basis: 'runs',
        dependency_health: DEPENDENCY_HEALTH,
        dependency_cap: SCORING_CAP,
        ...overrides,
    };
}

/** A shared service scored from its served calls: oversight not applicable
 *  (it raised no hold), liveness advisory for its kind. */
export function serviceHealth(overrides: Partial<SystemHealth> = {}): SystemHealth {
    return {
        ai_system_id: 'sys-scoring',
        health_state: 'healthy',
        health_score: 0.9,
        // Advisory for kind `service`: stored and shown, not counted.
        liveness_score: 0.75,
        behaviour_score: 0.9,
        reliability_score: 0.96,
        cost_score: 1,
        conformance_score: 1,
        // The stored column may still hold a number; the level says n/a.
        oversight_score: 0,
        system_kind: 'service',
        runs_24h: 0,
        open_drift_events: 0,
        reasons: [],
        is_trustworthy: true,
        computed_at: new Date().toISOString(),
        signal_levels: {
            liveness: { score: 0.75, level: 'advisory' },
            behaviour: { score: 0.9, level: 'enforced' },
            reliability: { score: 0.96, level: 'enforced' },
            cost: { score: 1, level: 'enforced' },
            conformance: { score: 1, level: 'enforced' },
            oversight: { score: null, level: 'not_applicable' },
            review: { score: 1, level: 'enforced' },
        },
        signal_basis: 'segments',
        dependency_health: [],
        dependency_cap: null,
        ...overrides,
    };
}

export const SERVICE_FACTS: ServiceFacts = {
    served_calls: 128,
    outcome_mix: { answered: 120, failed: 5, rejected: 1, timed_out: 2, blocked: 0 },
    answered_calls: 120,
    rejected_calls: 1,
    success_rate: 120 / 128,
    tool_calls: 300,
    tool_errors: 6,
    tool_error_rate: 0.02,
    total_cost_usd: 1.92,
    cost_per_answered_call: 0.015,
    guardrail_blocks: 0,
    policy_denials: 1,
    approval_requests: 0,
};

/** "dependency Scoring Service widened (v1 → v2): …", with the service's
 *  own diff nested. */
export const DEPENDENCY_WIDENED: DiffEntry = {
    kind: 'dependency_contract_changed',
    section: 'dependencies',
    id: 'sys-scoring',
    detail: 'dependency Scoring Service widened (v1 → v2): model gpt-5 added',
    from_version: 1,
    to_version: 2,
    dependency_diff: {
        widened: [{ kind: 'added', section: 'llm_models', detail: 'model gpt-5 added' }],
        narrowed: [{ kind: 'removed', section: 'mcp_servers', detail: 'MCP server legacy-core removed' }],
        changed: [],
    },
};

export const DEPENDENCY_REATTESTED: DiffEntry = {
    kind: 'dependency_contract_changed',
    section: 'dependencies',
    id: 'sys-address',
    detail: 'Address Validator re-attested (v2 → v3); its terms did not change',
    from_version: 2,
    to_version: 3,
};

export const CLOSURE_DEPENDENCIES: ClosureDependency[] = [
    {
        role: 'external',
        depends_on_system_id: null,
        agent_card_id: 'card-bureau',
        criticality: 'degradable',
        contributes_to_decision: true,
        fallback: 'Manual underwriting',
        separability_rationale: null,
        assumptions: { expected_availability_pct: 99.5 },
        // A conditional closure term.
        supplier_legal_entity: 'Nordic Credit Bureau AB',
    },
    {
        role: 'shared_service',
        depends_on_system_id: 'sys-address',
        agent_card_id: null,
        criticality: 'degradable',
        contributes_to_decision: false,
        fallback: 'Accept the applicant-supplied address',
        separability_rationale: 'Formats addresses; never read by the scoring step.',
        assumptions: {},
        contract_pin: 'ctr-address-2',
        contract: { id: 'ctr-address-2', version: 2, hash: 'abcdef0123456789' },
    },
    {
        role: 'shared_service',
        depends_on_system_id: 'sys-scoring',
        agent_card_id: null,
        criticality: 'critical',
        contributes_to_decision: true,
        fallback: null,
        separability_rationale: null,
        assumptions: {},
        contract_pin: null,
        contract: { id: 'ctr-scoring-2', version: 2, hash: '9876543210fedcba' },
    },
];

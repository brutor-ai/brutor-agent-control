/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * ISO 42001 types, plus the AI System fleet board.
 *
 * Mirrors the control-plane shapes served under `/v1/admin/iso42001` and
 * `/v1/admin/ai-systems`.
 *
 * An AI System is a Resource Group with group_type='ai_system'; there is
 * no separate ai_systems entity and no entity types for one.
 *
 * The fleet-board types at the bottom of this file are a *projection* over
 * those resource groups, not an entity of their own.
 */

import { EuAiActRiskTier, LifecycleStage, SystemKind } from './groupTypes';
import { EVIDENCE_KIND_LABEL } from './complianceEvidenceTypes';
import type { EvidencePointerKind } from './complianceEvidenceTypes';
import type { AgentReleaseRef } from './agentIdentityTypes';
import type { BaselineEpoch, ResolutionKind } from './driftResolutionTypes';
import type { components } from './generated/control-plane-api';
import type { Answered } from './api/controlPlaneTypes';
import { agentFindingLabel, agentFindingOfMetric } from './agentRelease';

// ============================================================================
// AI SYSTEM FLEET BOARD
// ============================================================================
//
// Mirrors `GET /v1/admin/ai-systems`. An AI System is a Resource Group with
// group_type='ai_system'; these shapes are a read-only projection over them.

/** What an AI System is made of — the resource group's member counts. */
export interface AISystemMemberCounts {
    agents: number;
    models: number;
    mcp_servers: number;
    skills: number;
}

/**
 * The system's place in the composition: what it is made of,
 * what it depends on, and who calls it. `idle` is set for a shared service
 * nobody called in the window — the list renders it as "idle", never as an
 * empty cell.
 */
export interface AISystemComposition {
    members: AISystemMemberCounts;
    depends_on: number;
    worst_dependency_health: DependencyHealth | null;
    used_by_declared: number;
    used_by_observed: number;
    idle: boolean;
    /** Direct child resource groups. */
    child_groups: number;
}

/** root = nobody calls it; shared_service = at least one declared or
 *  observed caller system; root_and_service = both called and calling. */
export type AISystemRole = 'root' | 'shared_service' | 'root_and_service';

/** The Role filter's query values (`role=` on the list endpoint). */
export type AISystemRoleFilter = 'root' | 'shared_service' | 'external_deps';

export const AI_SYSTEM_ROLE_FILTER_LABELS: Record<AISystemRoleFilter, string> = {
    root: 'Root systems',
    shared_service: 'Shared services',
    external_deps: 'With external dependencies',
};

export const AI_SYSTEM_ROLE_LABELS: Record<AISystemRole, string> = {
    root: 'Root',
    shared_service: 'Shared service',
    root_and_service: 'Root + service',
};

/** One system whose contributing dependency on this one lifts its
 *  effective risk tier. */
export interface EffectiveTierSource {
    system_id: string;
    system_name: string | null;
    tier: string | null;
}

/** The ONE liveness verdict — the same value the liveness
 *  panel and the health scorer read. idle/unstarted are never "silent". */
export interface AISystemLivenessState {
    state: 'alive' | 'silent' | 'idle' | 'unstarted' | 'not_configured';
    reason: string;
}

/** One row on the fleet board. */
/** Who the agent is: registered identity, the implementation the latest
 *  live run reported, its outcome catalogue and contract. */
export interface AISystemCardAgent {
    identity_id: string | null;
    identity_name: string | null;
    identity_count: number;
    impl_name: string | null;
    impl_version: string | null;
    impl_build: string | null;
    impl_trust: string | null;
    declared_outcomes: number;
    contract_version: number | null;
    contract_status: string | null;
}

/** What the gateway enforces — EFFECTIVE counts over the ancestor chain. */
export interface AISystemCardGateway {
    policies: number;
    guardrails: number;
    limits: number;
    /** Guardrail blocks + policy denials in the KPI window. */
    blocks: number;
}

export interface AISystemCardResources {
    models: number;
    mcp_servers: number;
    skills: number;
    delegate_agents: number;
}

/** Observability + evaluation over the window. Per-run figures are null
 *  when there are no runs — "no runs" is not "zero". */
export interface AISystemCardKpis {
    window_hours: number;
    /** `runs` — the runs this system started. `served_calls` — a shared
     *  service that started none in the window: figures are the calls it
     *  served inside other systems' runs, and `traces`
     *  counts those calls. */
    basis: 'runs' | 'served_calls';
    traces: number;
    spans: number;
    success_rate: number | null;
    failed: number;
    p50_latency_ms: number | null;
    p95_latency_ms: number | null;
    cost_usd: number;
    /** Input + output; `input_tokens + output_tokens === tokens`. */
    tokens: number;
    input_tokens: number;
    output_tokens: number;
    eval_suites: number;
    last_eval_pass_rate: number | null;
    last_eval_at: string | null;
}

export interface AISystemCardFacts {
    agent: AISystemCardAgent;
    gateway: AISystemCardGateway;
    resources: AISystemCardResources;
    kpis: AISystemCardKpis;
}

export interface AISystemSummary {
    id: string;
    name: string;
    display_name?: string | null;
    description?: string | null;
    system_kind?: SystemKind | null;

    owner?: string | null;
    intended_use?: string | null;
    intended_client_labels?: string[] | null;
    intended_client_systems?: string[] | null;
    lifecycle_stage?: LifecycleStage | null;
    eu_ai_act_risk_tier?: EuAiActRiskTier | null;
    eu_ai_act_role?: string | null;
    /** max(own declared tier, tier of every system that declares a
     *  contributing dependency on this one). Read-only. */
    risk_tier_effective?: EuAiActRiskTier | null;
    effective_tier_sources?: EffectiveTierSource[];
    /** A contributes_to_decision=false edge with a rationale touches this
     *  system (either side) — the Art 6(3) separability claim is on file. */
    separability_declared?: boolean;
    /** Every separability claim touching this system is
     *  sealed as a declaration record (false when there is none). */
    separability_sealed?: boolean;
    /** The gateway refuses A2A callers outside
     *  intended_client_systems (otherwise unaccepted callers are advisory). */
    enforce_accepted_callers?: boolean;
    role?: AISystemRole | null;
    liveness?: AISystemLivenessState | null;
    has_external_dependencies?: boolean;
    /** Composition findings — counted by the
     *  governance-gap filter alongside the documentation gaps. */
    composition_finding_count?: number;

    parent_group_id?: string | null;
    hierarchy_level: number;
    hierarchy_path?: string | null;
    is_active: boolean;

    composition: AISystemComposition;

    /**
     * Governance attributes this system has not filled in; empty means
     * fully documented. Computed server-side so the UI and the control
     * plane cannot disagree about what "documented" means.
     */
    governance_gaps: string[];

    /**
     * Assurance rollup — present only when the fleet was fetched with
     * `include_health: true`, read from the materialised health rows.
     * All-null means "never computed": render as `unknown`, never green.
     */
    health_state?: string | null;
    health_score?: number | null;
    open_drift_events?: number | null;
    health_computed_at?: string | null;
    /** Pinned period verdict from the same health row — the row colour
     *  needs BOTH judgments (green = healthy AND assured). */
    verdict_status?: string | null;

    /** Only set for deprecated systems — quiet long enough to retire. */
    retirable?: boolean | null;
    retirable_at?: string | null;
    /** Gallery card facts — only with `include_card: true`. */
    card?: AISystemCardFacts | null;
    created_at?: string | null;
    updated_at?: string | null;
}

/** Aggregates over the whole filtered fleet, not just the current page. */
export interface AISystemFleetStats {
    total: number;
    by_kind: Record<string, number>;
    by_lifecycle_stage: Record<string, number>;
    by_risk_tier: Record<string, number>;
    without_owner: number;
    without_intended_use: number;
    fully_documented: number;
}

export interface AISystemFleetResponse {
    systems: AISystemSummary[];
    stats: AISystemFleetStats;
    total: number;
    limit: number;
    offset: number;
}

export interface AISystemFleetFilters {
    system_kind?: string;
    lifecycle_stage?: string;
    risk_tier?: string;
    owner?: string;
    undocumented_only?: boolean;
    /** Root / shared service / systems with external deps. */
    role?: AISystemRoleFilter;
    include_inactive?: boolean;
    /** Attach the materialised health rollup to each row (one grouped read). */
    include_health?: boolean;
    /** Attach the gallery card facts (agent, gateway, resources, KPIs). */
    include_card?: boolean;
    limit?: number;
    offset?: number;
}

/** Human labels for the governance-gap keys the API returns. */
export const GOVERNANCE_GAP_LABELS: Record<string, string> = {
    owner: 'Owner',
    intended_use: 'Intended use',
    lifecycle_stage: 'Lifecycle stage',
    eu_ai_act_risk_tier: 'Risk tier',
};

// ============================================================================
// RUN LEDGER
// ============================================================================
//
// Mirrors `GET /v1/admin/ai-systems/{id}/runs`, `/runs/stats`,
// `/v1/admin/runs/{id}` and `/v1/admin/runs/unattributed`.

/** How a run ended as an EXECUTION — question 1 of the
 *  three a run is judged on. A gateway refusal that ends a run
 *  is `errored` with error cause `blocked_by_policy`. */
export type TerminalState =
    | 'completed'
    | 'completed_degraded'
    | 'exhausted'
    | 'errored'
    | 'abandoned'
    | 'cancelled';

/** Question 2: was the outcome the agent reported one the
 *  active contract declares? */
export type OutcomeConformance = 'declared' | 'undeclared' | 'unreported';

/** THE run classification — the three answers combined once,
 *  by the control plane. `no_work` (a declared outcome that does not count
 *  toward the baseline, e.g. an empty poll) is never a success. */
export type RunClass = 'success' | 'no_work' | 'deviation' | 'failed' | 'not_decisive';

/** Which mechanism closed the run. */
export type FinalizedBy = 'explicit' | 'terminal' | 'idle' | 'sweeper';

/** How much of the signed chain we actually observed. */
export type ChainIntegrity = 'intact' | 'client_asserted' | 'observed_external' | 'partial' | 'broken';

/**
 * Where a run's steps were witnessed. `gateway` — proxied, every
 * control on the row. `otel` — ingested from an OpenTelemetry export: the
 * exporter's account, no gateway control behind it. `mixed` — both.
 */
export type RunSource = 'gateway' | 'otel' | 'mixed';

export const RUN_SOURCE_LABELS: Record<RunSource, string> = {
    gateway: 'Witnessed by the gateway',
    otel: 'Reported by the system (OpenTelemetry)',
    mixed: 'Partly witnessed, partly reported',
};

export interface RunSummary {
    id: string;
    root_task_id: string;
    ai_system_id?: string | null;
    ai_system_name?: string | null;
    /** The resource group the run started in. */
    resource_group_id?: string | null;
    via_system_ids: string[];
    system_kind?: SystemKind | null;
    actor_type?: string | null;
    actor_id?: string | null;
    /** Who the actor acted FOR (delegation subject). */
    subject_id?: string | null;
    subject_kind?: string | null;
    started_at?: string | null;
    ended_at?: string | null;
    /** Elapsed wall clock, first action to last — INCLUDES time the run sat
     *  parked awaiting a human approval. */
    duration_ms?: number | null;
    /** `duration_ms` minus `awaiting_human_ms`: the system's own
     *  execution time, and what the per-run duration ceiling measures. */
    active_duration_ms?: number | null;
    /** Time spent awaiting a human approval decision (overlapping
     *  holds counted once). */
    awaiting_human_ms?: number | null;
    terminal_state: TerminalState;
    finalized_by: FinalizedBy;
    /** Passes of the agent loop (Run > Turn > Step). Read
     *  `turn_attribution` before presenting this as reported fact.
     *
     *  OPTIONAL on the list row, and absent in practice: the Control Plane's
     *  `RunSummary` declares neither field, so Pydantic strips them from the
     *  response even though the columns exist on `ai_system_runs`. Declaring
     *  them required here made the renderer read `undefined` as a real value —
     *  a blank Turns cell and an "inferred" marker on every row, including
     *  runs whose client did assert turn ids. Treat absence as unknown, not as
     *  zero and not as inferred. Both fields ARE served on `RunDetail`. */
    turn_count?: number | null;
    /** 'client_asserted' | 'inferred' — see `turn_count` on absence. */
    turn_attribution?: string | null;
    /** Leaf unit — one governed call. */
    action_count: number;
    /** DISTINCT client-asserted phases; 0 = none asserted, not "one phase". */
    step_count: number;
    llm_call_count: number;
    tool_call_count: number;
    max_delegation_depth: number;
    distinct_tools: number;
    total_tokens: number;
    total_cost_usd: number;
    error_count: number;
    tool_error_count: number;
    guardrail_block_count: number;
    policy_denial_count: number;
    chain_integrity: ChainIntegrity;
    /** gateway | otel | mixed — read it before trusting any figure on the row. */
    source?: RunSource;
    reopened_count: number;
    is_replay: boolean;
    /** The outcome the agent reported (`<outcome>[.<variant>]`),
     *  verbatim; null when it reported none. */
    outcome_id?: string | null;
    /** Judged against the contract below at finalize; null while open. */
    outcome_conformance?: OutcomeConformance | null;
    /** From the catalogue when declared, else null. */
    outcome_counts_toward_baseline?: boolean | null;
    /** The active contract version the outcome was judged against. */
    outcome_contract_version?: number | null;
    /** THE run class; null while the run is open. */
    run_class?: RunClass | null;
    /** Why the execution failed — set iff errored / exhausted. */
    error_cause?: string | null;
    /** The run's instruction fingerprint. */
    prompt_fingerprint?: string | null;
    rag_collection_ids?: string | null;
    /** The NORMALISED client label: the channel the run came
     *  through (X-Brutor-Client, else the User-Agent product name without its
     *  version), never the HTTP library's version string. */
    client_label?: string | null;
    /** WHAT software ran this and WHICH release, from the
     *  run's first action that carries one. Absent = none declared. */
    agent_impl_name?: string | null;
    agent_impl_version?: string | null;
    agent_impl_build?: string | null;
    agent_impl_trust?: 'verified' | 'declared' | null;
    /** `sha256:` + 64 hex over the first action's User-Agent, MCP clientInfo
     *  and instruction fingerprint. */
    implementation_fingerprint?: string | null;
    /** Tools the run called or tried to call, sorted; null = finalized
     *  before tool lists were kept. */
    tools_used?: string[] | null;
    /** Data-scope facts; null = finalized before the feature. */
    scope_declared?: boolean | null;
    records_touched?: number | null;
    scope_violation_count?: number | null;
    scope_unbound_count?: number | null;
}

/** One gateway step inside a run — the timeline entry. */
export interface RunAction {
    timestamp?: string | null;
    proxy_type: string;
    proxy_subtype?: string | null;
    capability?: string | null;
    model?: string | null;
    status_code?: number | null;
    success?: boolean | null;
    duration_ms?: number | null;
    cost_usd?: number | null;
    total_tokens?: number | null;
    trace_continuity?: string | null;
    delegation_depth?: number | null;
    guardrail_blocked?: boolean | null;
    error_message?: string | null;
    correlation_id?: string | null;
    /** The step and turn this action belongs to, denormalised onto the leaf. */
    step_id?: string | null;
    step_name?: string | null;
    turn_id?: string | null;
    turn_seq?: number | null;
    /*
     * The run overlay's facts — computed ONCE by the control plane
     * (`app/services/run_actions.py`), so the Runs timeline, Run details and
     * the Composition tab's run overlay cannot disagree. Metadata only.
     */
    /** The proxy-log row id. */
    id?: string | null;
    /** Gateway-inferred turn ordinal — shown beside an asserted turn, never instead. */
    inferred_turn?: number | null;
    /** 1, 2, 3 … in the gateway's order; a delegate's steps under the A2A call that entered them (5a, 5b; 5a.1). */
    number?: string | null;
    /** The number of the A2A call this action ran inside; null = top level. */
    entered_by?: string | null;
    nesting?: number;
    outcome?: RunActionOutcome | null;
    target_kind?: RunActionTargetKind | null;
    target_id?: string | null;
    target_name?: string | null;
    kb_ids?: string[];
    /** A skill run's script — only a filename the skill's catalog registers. */
    skill_script?: string | null;
    /** The AI System that took the action. */
    system_id?: string | null;
    system_name?: string | null;
    /** The run's root took it (THE own-rows rule); false = a delegate's step. */
    own?: boolean;
    /** A2A calls: the AI System the call entered. */
    delegate_system_id?: string | null;
    delegate_system_name?: string | null;
    a2a_task_state?: string | null;
}

/** One action's outcome — the connector lines' row classification. */
export type RunActionOutcome = 'answered' | 'error' | 'blocked' | 'held';

/** What an action reached — the connector kinds. */
export type RunActionTargetKind = 'model' | 'mcp' | 'skill' | 'a2a' | 'kb';

export interface RunDetail extends RunSummary {
    actions?: RunAction[];
    agent_id?: string | null;
    /** The contract version that governed this run. */
    contract_id?: string | null;
    contract_hash?: string | null;
    trace_root_kind?: string | null;
    skill_call_count: number;
    a2a_call_count: number;
    /** Passes of the agent loop. Read `turn_attribution` before showing
     *  this as a number the client reported. */
    turn_count: number;
    /** 'client_asserted' | 'inferred' */
    turn_attribution: string;
    tool_signature?: string | null;
    tool_set_signature?: string | null;
    prompt_tokens: number;
    completion_tokens: number;
    /** Per-model outcome within this run, written by the gateway when it
     *  finalizes the run. A `model_change` drift finding names the model
     *  that degraded from it. `p50_ms` is null for a model with no timed
     *  call. */
    models_used: Array<{
        model: string;
        calls: number;
        errors: number;
        blocked: number;
        tokens: number;
        cost: number;
        p50_ms: number | null;
    }>;
    cache_hit_count: number;
    tokens_saved: number;
    retry_count: number;
    fallback_count: number;
    tool_error_types: Record<string, number>;
    guardrail_trip_count: number;
    approval_request_count: number;
    input_bytes: number;
    output_bytes: number;
    first_prompt_tokens?: number | null;
    /** The largest context any call sent its model, cached tokens included.
     *  null = not measured, never 0. */
    peak_context_tokens?: number | null;
    /** The largest share of a model's window any call used; null = unknown. */
    peak_context_utilization?: number | null;
    /** The peak over the same agent's first measured context. */
    context_growth?: number | null;
    rag_used: boolean;
    rag_chunks: number;
    feedback_score?: number | null;
    created_at?: string | null;
    is_successful: boolean;
    is_baseline_eligible: boolean;
    is_fragmented: boolean;
}

export interface RunListResponse {
    runs: RunSummary[];
    total: number;
    limit: number;
    offset: number;
}

/** One call a shared service served inside another system's run — its
 *  segment of that run. Figures are the service's slice;
 *  `run_id` opens the whole run. */
export interface ServedCall {
    segment_id: string;
    run_id: string;
    root_task_id: string;
    root_system_id: string | null;
    root_system_name: string | null;
    caller_system_id: string | null;
    caller_system_name: string | null;
    first_action_at: string | null;
    last_action_at: string | null;
    /** Summed duration of the service's own calls in the run. */
    duration_ms: number | null;
    action_count: number;
    llm_call_count: number;
    tool_call_count: number;
    total_tokens: number;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number;
    models: string[];
    /** answered | rejected | failed | timed_out | blocked (gateway refusal) */
    outcome: string;
    outcome_id: string | null;
    outcome_conformance: string | null;
    error_cause: string | null;
    a2a_task_state: string | null;
    agent_impl_name: string | null;
    agent_impl_version: string | null;
    guardrail_block_count: number;
    policy_denial_count: number;
}

export interface ServedCallListResponse {
    calls: ServedCall[];
    total: number;
    limit: number;
    offset: number;
}

/** Cost of ONE processed outcome (a full outcome id), over its `success`
 *  runs only. */
export type OutcomeCostRow = components['schemas']['OutcomeCostRow'];

/** Run length of ONE processed outcome, over its `success` runs. */
export type OutcomeActionRow = components['schemas']['OutcomeActionRow'];

/** Signal 1 — cost per *processed outcome*. The population is
 *  the WORK: `success` runs. Empty polls are no-work overhead; errored,
 *  exhausted and deviating runs are "spent, nothing produced". */
export interface CostPerRunSignal {
    /** `success` runs — the population every per-run figure averages. */
    processed_runs: number;
    total_cost_usd: number;
    cost_per_processed_outcome?: number | null;
    median_cost_per_processed_outcome?: number | null;
    p95_cost_per_processed_outcome?: number | null;
    /** Per outcome / variant — never one blended average. */
    by_outcome?: OutcomeCostRow[];
    /** Spent, nothing produced: failed + deviating runs. */
    wasted_cost_usd: number;
    /** No-work overhead: completed runs whose declared outcome does not count. */
    no_work_runs: number;
    no_work_cost_usd: number;
    total_tokens: number;
    tokens_per_processed_outcome?: number | null;
}

export interface ActionDistributionBucket {
    lower: number;
    upper?: number | null;
    count: number;
}

/** Signal 2 — run length (over the WORK population: `success` runs) and
 *  exhaustion (over every finished run). */
export interface ActionCountSignal {
    /** `success` runs the length figures describe. */
    runs: number;
    /** Every finished run — the exhaustion-rate denominator. */
    finished_runs: number;
    by_outcome?: OutcomeActionRow[];
    median_actions?: number | null;
    p95_actions?: number | null;
    max_actions?: number | null;
    distribution: ActionDistributionBucket[];
    exhausted_runs: number;
    exhaustion_rate?: number | null;
    /**
     * False when nothing in the window was closed by an explicit client
     * signal. Exhaustion cannot be observed from the proxy, so a 0% rate
     * without reporting means "unknown" — the UI must not render it as
     * healthy.
     */
    exhaustion_reporting_available: boolean;
    abandoned_runs: number;
}

export interface ToolErrorRow {
    tool: string;
    calls: number;
    errors: number;
    error_rate: number;
    error_types: Record<string, number>;
}

/** Signal 3 — tool error rate. */
export interface ToolErrorSignal {
    total_tool_calls: number;
    total_tool_errors: number;
    overall_error_rate?: number | null;
    by_tool: ToolErrorRow[];
}

export type StepKind = 'model' | 'tool' | 'skill' | 'agent' | 'other';

export interface StepErrorKindRow {
    kind: StepKind;
    calls: number;
    errors: number;
    error_rate?: number | null;
    /** Errors by HTTP status code ("none" when the call never got one). */
    status_codes: Record<string, number>;
}

/** Every failed call inside the window's runs, by kind — the gateway's own
 *  definition of an error (a call held for approval is not one). */
export interface StepErrorSignal {
    total_calls: number;
    total_errors: number;
    overall_error_rate?: number | null;
    by_kind: StepErrorKindRow[];
}

export const STEP_KIND_LABELS: Record<StepKind, string> = {
    model: 'Model calls',
    tool: 'Tool calls',
    skill: 'Skill runs',
    agent: 'Agent calls',
    other: 'Other calls',
};

export interface TerminalStateRow {
    state: TerminalState;
    count: number;
    share: number;
}

/** Signal 4 — the execution mix (question 1). `success_rate` is THE
 *  success rate: success / (success + deviation + failed). */
export interface TerminalStateSignal {
    total_runs: number;
    by_state: TerminalStateRow[];
    success_rate?: number | null;
}

/** One reported outcome (or none) in one run class. */
export type OutcomeRow = components['schemas']['OutcomeRow'];

/** How runs ended: by declared outcome, conformance and run class.
 *  Undeclared and unreported runs are listed, never hidden. */
export type OutcomeSignal = components['schemas']['OutcomeSignal'];

/** Why runs failed (errored / exhausted), by error cause. */
export type ErrorCauseSignal = components['schemas']['ErrorCauseSignal'];

/** Context size over the work population, with its coverage. */
export type ContextSignal = components['schemas']['ContextSignal'];

export interface RunStatsResponse {
    ai_system_id?: string | null;
    period_hours: number;
    window_start?: string | null;
    window_end?: string | null;
    cost: CostPerRunSignal;
    actions: ActionCountSignal;
    /** How large a context the work took; judged by drift, not by a threshold. */
    context?: ContextSignal;
    tool_errors: ToolErrorSignal;
    /** Every failed call in the window's runs, by kind of call. */
    step_errors?: StepErrorSignal;
    terminal_states: TerminalStateSignal;
    /** The outcome mix and the run classes. */
    outcomes?: OutcomeSignal;
    /** Failed runs by error cause. */
    error_causes?: ErrorCauseSignal;
    chain_integrity_pct?: number | null;
    /** Share observed completely (signed + client-asserted). Coverage — the
     *  number that qualifies every signal. */
    observed_pct?: number | null;
    /** Share grouped by a client-asserted run id. Provenance, disclosed. */
    asserted_pct?: number | null;
    /** Share of runs ingested from external telemetry; inside observed_pct, disclosed apart. */
    external_pct?: number | null;
    fragmented_runs: number;
}

/** One time bucket of run activity — the chartable shape of the signals.
 *  Sparse: quiet buckets are omitted; the chart zero-fills, because an
 *  empty bucket is real data (quiet), not a gap to interpolate over. */
export interface RunTimeseriesPoint {
    bucket_start: string;
    runs: number;
    /** Run classes per bucket. */
    success: number;
    no_work: number;
    deviation: number;
    failed: number;
    not_decisive: number;
    cost_usd: number;
    /** What the bucket's `success` runs cost. */
    processed_cost_usd: number;
    cost_per_processed_outcome?: number | null;
    tool_calls: number;
    tool_errors: number;
    tool_error_rate?: number | null;
    model_calls?: number;
    model_errors?: number;
    model_error_rate?: number | null;
    /** Mean over the bucket's `success` runs (the tiles keep the exact median). */
    avg_actions?: number | null;
}

export interface RunTimeseriesResponse {
    ai_system_id: string;
    period_hours: number;
    bucket: 'hour' | 'day';
    window_start?: string | null;
    window_end?: string | null;
    points: RunTimeseriesPoint[];
}

export interface UnattributedRunGroup {
    resource_group_id?: string | null;
    resource_group_name?: string | null;
    runs: number;
    total_cost_usd: number;
    first_seen?: string | null;
    last_seen?: string | null;
    sample_root_task_ids: string[];
}

export interface UnattributedRunsResponse {
    total_unattributed_runs: number;
    total_cost_usd: number;
    groups: UnattributedRunGroup[];
    period_hours: number;
}

export interface RunListFilters {
    terminal_state?: string;
    /** THE run class. */
    run_class?: RunClass | '';
    period_hours?: number;
    include_replays?: boolean;
    limit?: number;
    offset?: number;
    /** Find a run: substring of its id, root task id, subject, release or client label. */
    q?: string;
}

/** Human labels for terminal states (the execution, question 1). */
export const TERMINAL_STATE_LABELS: Record<TerminalState, string> = {
    completed: 'Completed',
    completed_degraded: 'Completed (degraded)',
    exhausted: 'Exhausted',
    errored: 'Errored',
    abandoned: 'Abandoned',
    cancelled: 'Cancelled',
};

/**
 * Tailwind classes per terminal state. Completed is not painted green here:
 * a completed execution is a success only when it also ended in a declared
 * outcome that counts toward the baseline — that is the run class's call.
 */
export const TERMINAL_STATE_CLASSES: Record<TerminalState, string> = {
    completed: 'bg-sky-100 text-sky-800',
    completed_degraded: 'bg-lime-100 text-lime-800',
    exhausted: 'bg-orange-100 text-orange-800',
    errored: 'bg-red-100 text-red-800',
    abandoned: 'bg-amber-100 text-amber-800',
    cancelled: 'bg-slate-100 text-slate-700',
};

export const CHAIN_INTEGRITY_LABELS: Record<ChainIntegrity, string> = {
    intact: 'Intact',
    client_asserted: 'Client-asserted — complete, linked on the client\'s word',
    observed_external: 'External — complete as reported by the exporter, not witnessed',
    partial: 'Partial — a client dropped the chain',
    broken: 'Broken — signature failed',
};

// ============================================================================
// LIVENESS
// ============================================================================
//
// The only signal that fires on the ABSENCE of traffic. Every other number
// in this file is a function of traffic that arrived, which is precisely
// why a system that stopped calling looks perfect on all of them.

export type LivenessMode = 'continuous' | 'scheduled' | 'sporadic' | 'on_demand';

export interface LivenessExpectation {
    id: string;
    tenant_id?: string | null;
    ai_system_id: string;
    mode: LivenessMode;
    expected_min_runs: number;
    window_seconds: number;
    grace_seconds: number;
    enabled: boolean;
    learned_from_runs?: number | null;
    learned_median_gap_seconds?: number | null;
    learned_p95_gap_seconds?: number | null;
    learned_at?: string | null;
    accepted_from_learned: boolean;
    last_seen_run_at?: string | null;
    /** Null means the detector has never evaluated this system — which
     *  looks identical to "healthy" if only findings are recorded. */
    last_checked_at?: string | null;
    last_alert_at?: string | null;
    alerts_on_silence: boolean;
    deadline_seconds: number;
    has_learned_cadence: boolean;
    /** At most this long without a PROCESSED run (an outcome
     *  that counts toward the baseline) before `work_quiet`; null = off. */
    max_quiet_processed_seconds?: number | null;
    created_at?: string | null;
    updated_at?: string | null;
}

export interface LivenessStatus {
    ai_system_id: string;
    /** False when nothing has been saved; the expectation then carries the
     *  kind-derived default rather than the endpoint 404-ing. */
    configured: boolean;
    expectation: LivenessExpectation;
    /** The ONE liveness verdict. Render it — and `reason` —
     *  through `lib/livenessState.ts`; never re-derive a label. */
    state: AISystemLivenessState['state'];
    /** Why the state is what it is, in one sentence. */
    reason: string;
    /** Judgment window the state was computed over. */
    window_hours: number;
    /** Inbound calls (run segments) in the window. */
    inbound_calls: number;
    /** Latest own run or run segment, whichever is later. */
    last_activity_at?: string | null;
    last_run_at?: string | null;
    quiet_for_seconds?: number | null;
    /** The latest `success` run: work getting done, not only the
     *  agent being alive and checking. */
    last_processed_run_at?: string | null;
    processed_quiet_for_seconds?: number | null;
    /** No processed run within `max_quiet_processed_seconds` while the
     *  system still runs — its own signal, never `silent`. */
    work_quiet: boolean;
    work_quiet_reason?: string | null;
    /** Wire compatibility only. A label derived from this cannot express
     *  idle / unstarted — read `state` (guarded by
     *  lib/__tests__/livenessSingleMapping.test.ts). */
    has_ever_run: boolean;
    /** Wire compatibility only — `state === 'silent'`. Read `state`. */
    is_silent: boolean;
}

export interface CadenceProposal {
    mode: LivenessMode;
    window_seconds: number;
    grace_seconds: number;
    expected_min_runs: number;
    learned_from_runs: number;
    median_gap_seconds?: number | null;
    /** The window is sized from this, not the median: a window at the
     *  median alerts on half of all normal quiet periods. */
    p95_gap_seconds?: number | null;
    confident: boolean;
    rationale: string;
}

export interface LivenessExpectationUpdate {
    mode?: LivenessMode;
    expected_min_runs?: number;
    window_seconds?: number;
    grace_seconds?: number;
    enabled?: boolean;
    /** null clears the bound (60 s – 366 d when set). */
    max_quiet_processed_seconds?: number | null;
}

export const LIVENESS_MODE_LABELS: Record<LivenessMode, string> = {
    continuous: 'Continuous',
    scheduled: 'Scheduled',
    sporadic: 'Sporadic',
    on_demand: 'Not monitored',
};

/**
 * What each mode means, in the operator's terms rather than the schema's.
 *
 * `on_demand` is described as a decision, not an absence — the whole point
 * of the mode is that "we do not monitor this" is on the record rather
 * than being indistinguishable from nobody having got round to it.
 */
export const LIVENESS_MODE_DESCRIPTIONS: Record<LivenessMode, string> = {
    continuous: 'Expect a steady stream of runs. Alert if it stops or collapses.',
    scheduled: 'Expect at least one run per window. Alert if a window passes with none.',
    sporadic: 'Runs irregularly — never alert on quiet, only on outright failure.',
    on_demand: 'Deliberately not monitored for liveness.',
};

/** Human duration for the liveness card. */
export function humanizeSeconds(seconds?: number | null): string {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} days`;
    if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} hours`;
    if (seconds >= 60) return `${Math.round(seconds / 60)} min`;
    return `${seconds}s`;
}

// ============================================================================
// DRIFT
// ============================================================================

export type DriftClass =
    | 'behavioural' | 'cost' | 'dependency' | 'model' | 'input' | 'conformance'
    // A signed operating-envelope term breached (CP-emitted).
    | 'contract'
    // An agent implementation finding (CP-emitted): an
    // unapproved release, a name mismatch, a missing or undeclared release.
    | 'agent';

export type DriftStatus = 'active' | 'acknowledged' | 'resolved' | 'false_positive';

export type DriftSeverity = 'critical' | 'high' | 'medium' | 'low';

export type AttributedCause =
    // Rung 0: the change point lies at the edge of an incident
    // window a human already declared; its runs are not judged.
    | 'incident_window'
    | 'contract_change'
    // The agent shipped a new release (or kept its version and
    // started behaving like different software). After contract_change,
    // before model_change on the ladder.
    | 'agent_release'
    | 'model_change' | 'prompt_change' | 'dependency_change'
    | 'dependency_degraded' | 'composition_change' | 'corpus_change' | 'input_drift'
    | 'traffic_mix' | 'unexplained'
    // Not an attribution: a signed bound the ledger says was exceeded.
    | 'contract_breach';

/** Which population a baseline (and the drift measured
 *  against it) describes: the system's own runs, or the calls it served
 *  as a shared service (run segments). */
export type SignalBasis = 'runs' | 'segments';

/** The caller sentinel on an aggregate (all-callers) baseline row. */
export const ALL_CALLERS = 'all';

export interface DriftEvent {
    id: string;
    ai_system_id: string;
    actor_id?: string | null;
    basis?: SignalBasis;
    /** Set when the baseline was a per-caller sub-baseline of a service. */
    caller_system_id?: string | null;
    drift_class: DriftClass;
    metric_key: string;
    detected_at?: string | null;
    /** When the behaviour moved, not when we noticed. */
    change_point_at?: string | null;
    baseline_value?: number | null;
    observed_value?: number | null;
    effect_size?: number | null;
    direction?: string | null;
    severity: DriftSeverity;
    attributed_cause: AttributedCause;
    cause_confidence?: number | null;
    cause_evidence: Record<string, unknown>;
    correlated_metrics: unknown[];
    response_taken: Record<string, unknown>;
    system_alert_id?: string | null;
    status: DriftStatus;
    resolution_note?: string | null;
    resolved_by?: string | null;
    resolved_at?: string | null;
    /** How it was closed; null while open (and on findings
     *  closed before the kinds existed). */
    resolution_kind?: ResolutionKind | null;
    is_open: boolean;
    is_explained: boolean;
}

export interface DriftListResponse {
    events: DriftEvent[];
    total: number;
    open_count: number;
    /** A high proportion is a signal about the *detector*, not the systems. */
    unexplained_count: number;
}

export interface Baseline {
    id: string;
    ai_system_id: string;
    metric_key: string;
    bucket: string;
    actor_id?: string | null;
    basis?: SignalBasis;
    /** `all` (ALL_CALLERS) on the aggregate row; a calling system's id on a
     *  per-caller sub-baseline. */
    caller_system_id?: string;
    median?: number | null;
    mad?: number | null;
    p50?: number | null;
    p90?: number | null;
    p99?: number | null;
    distribution: Record<string, number>;
    sample_count: number;
    window_start?: string | null;
    window_end?: string | null;
    state: 'learning' | 'ready' | 'stale';
    is_ready: boolean;
    computed_at?: string | null;
    /** The latest epoch in force for this baseline: samples
     *  before it are excluded and the baseline re-learns from it. */
    epoch?: BaselineEpoch | null;
}

export interface BaselineListResponse {
    baselines: Baseline[];
    total: number;
    learning: number;
}

export const DRIFT_CLASS_LABELS: Record<DriftClass, string> = {
    behavioural: 'Behaviour',
    cost: 'Cost',
    dependency: 'Dependency',
    model: 'Model',
    input: 'Input',
    conformance: 'Conformance',
    contract: 'Contract breach',
    agent: 'Agent release',
};

export const DRIFT_SEVERITY_CLASSES: Record<DriftSeverity, string> = {
    critical: 'bg-red-200 text-red-900',
    high: 'bg-red-100 text-red-800',
    medium: 'bg-amber-100 text-amber-800',
    low: 'bg-slate-100 text-slate-700',
};

/**
 * Plain-language names for the drift metric keys (wire-stable slugs from
 * gateway-core `services/drift/mod.rs::metric`). The slug is the join key
 * between baseline, event and UI — never rename the keys, only the labels.
 */
export const DRIFT_METRIC_LABELS: Record<string, string> = {
    t_class: 'Workload class',
    action_count: 'Actions per run',
    cost_per_run: 'Cost per run',
    duration_ms: 'Run duration (active)',
    tool_call_count: 'Tool calls per run',
    max_delegation_depth: 'Delegation depth',
    tool_error_rate: 'Tool error rate',
    exhaustion_rate: 'Runs ending exhausted',
    tool_mix: 'Tool usage mix',
    terminal_state_mix: 'Run execution mix',
    // The declared-outcome mix over processed runs, and why runs failed.
    outcome_mix: 'Outcome mix',
    error_cause_mix: 'Error cause mix',
    model_mix: 'Model mix',
    first_prompt_tokens: 'Task size (first prompt)',
    peak_context_tokens: 'Peak context size',
    context_growth: 'Context growth',
    input_bytes: 'Input size',
    client_mix: 'Caller mix',
    // Service (segment-basis) metrics. On basis `segments`
    // the existing keys read "per served call" (see driftMetricLabel).
    llm_call_count: 'LLM calls per run',
    total_tokens: 'Tokens per run',
    error_rate: 'Error rate',
};

/** What a shift in each metric actually means, in operator terms. */
export const DRIFT_METRIC_EXPLAINERS: Record<string, string> = {
    t_class:
        'This system’s Big-T workload class changed — how its cost GROWS with usage, not what it costs today. ' +
        'Measured from the run ledger over the 30-day window; the cause line says what changed, or says plainly that nothing lines up.',
    action_count: 'How many governed calls a typical run makes moved away from this system’s baseline.',
    cost_per_run: 'What a typical run costs moved away from this system’s baseline.',
    duration_ms:
        'How long runs spend executing moved away from this system’s baseline. ' +
        'Time a run waited for a person to approve a held call is not counted.',
    tool_call_count: 'How many tool calls a typical run makes moved away from baseline.',
    max_delegation_depth: 'How deeply the system delegates (agent calling agent) moved away from baseline.',
    tool_error_rate: 'The share of tool calls that error moved away from baseline.',
    exhaustion_rate: 'The share of runs that hit their limits and stopped moved away from baseline.',
    tool_mix: 'Which set of tools runs use has changed relative to baseline.',
    terminal_state_mix: 'The distribution of how runs end as executions — completed, errored, exhausted, abandoned — moved away from this system’s baseline.',
    outcome_mix:
        'The share of each declared outcome among processed runs moved away from this system’s baseline — more of one kind of work, not each run behaving differently.',
    error_cause_mix:
        'Why runs fail changed — for example from tool failures to the model being unavailable — relative to this system’s baseline.',
    model_mix: 'Which models actually served this system has changed relative to baseline.',
    first_prompt_tokens: 'The size of the task as posed (first prompt) has changed — the inputs, not the system.',
    peak_context_tokens:
        'The largest context a typical run sends its model moved away from baseline. Cached tokens count, so better caching alone does not move it.',
    context_growth:
        'How far an agent’s context grows from its own first call moved away from baseline. If the task size held steady, the agent changed, not the questions.',
    input_bytes: 'The size of the inputs has changed relative to baseline.',
    client_mix: 'Which callers drive this system has changed relative to baseline.',
    llm_call_count: 'How many model calls a typical run (or served call) makes moved away from baseline.',
    total_tokens: 'How many tokens a typical run (or served call) consumes moved away from baseline.',
    error_rate: 'The share of runs (or served calls) that end in an error moved away from baseline.',
};

/** Human title for a drift event: what shifted, in words. Envelope
 *  breaches carry `envelope:<term>` keys; unknown slugs are humanized
 *  rather than shown raw. */
/**
 * The severity stripe ladder — one definition for every surface that
 * colour-codes a system's judgments (Mission Control cards, the fleet
 * board rows). Worst of health AND verdict:
 *
 *   red    — violations, or suspended: the contract was breached / traffic denied
 *   orange — drifting, stalled or silent: behaving differently, or stopped
 *   yellow — degraded, or assured with exceptions
 *   blue   — learning: watched, not yet proven (never green while learning)
 *   green  — healthy AND assured; earned twice, never averaged
 *   gray   — no judgment exists (never computed / unknown)
 */
export type AssuranceTone = 'red' | 'orange' | 'yellow' | 'green' | 'blue' | 'none';

export function assuranceTone(
    healthState?: string | null,
    verdictStatus?: string | null,
): AssuranceTone {
    if (verdictStatus === 'violations' || healthState === 'suspended' || healthState === 'failing') return 'red';
    if (['drifting', 'stalled', 'silent'].includes(healthState ?? '')) return 'orange';
    if (healthState === 'degraded' || verdictStatus === 'assured_with_exceptions') return 'yellow';
    if (healthState === 'healthy' && verdictStatus === 'assured') return 'green';
    if (healthState === 'learning') return 'blue';
    return 'none';
}

// Literal class strings (not built at runtime) so Tailwind's scanner keeps them.
const TONE_FILL: Record<AssuranceTone, string> = {
    red: 'bg-red-500',
    orange: 'bg-orange-500',
    yellow: 'bg-yellow-500',
    green: 'bg-green-500',
    blue: 'bg-blue-400',
    none: 'bg-gray-300 dark:bg-gray-600',
};

const TONE_BORDER: Record<AssuranceTone, string> = {
    red: 'border-red-500',
    orange: 'border-orange-500',
    yellow: 'border-yellow-500',
    green: 'border-green-500',
    blue: 'border-blue-400',
    none: 'border-gray-300 dark:border-gray-600',
};

/** Fill class of the ladder — the thin stripe on rows. */
export function assuranceStripe(
    healthState?: string | null,
    verdictStatus?: string | null,
): string {
    return TONE_FILL[assuranceTone(healthState, verdictStatus)];
}

/** Border class of the same ladder — cards outlined all the way round. */
export function assuranceBorder(
    healthState?: string | null,
    verdictStatus?: string | null,
): string {
    return TONE_BORDER[assuranceTone(healthState, verdictStatus)];
}

/** Border class for a tone computed elsewhere (e.g. a pseudo card's `none`). */
export const toneBorder = (tone: AssuranceTone): string => TONE_BORDER[tone];

/** Metric-key prefix of the finding the dependency-health emitter raises on
 *  a ROOT for a failing dependency (`dependency:<target system or edge>`). */
export const DEPENDENCY_METRIC_PREFIX = 'dependency:';

/** The dependency a `dependency:<target>` finding names, or null. */
export function dependencyMetricTarget(metricKey: string): string | null {
    return metricKey.startsWith(DEPENDENCY_METRIC_PREFIX)
        ? metricKey.slice(DEPENDENCY_METRIC_PREFIX.length) || null
        : null;
}

/**
 * Human name of a metric. On basis `segments` (a shared service's served
 * calls) "per run" reads "per served call" — the unit the baseline was
 * learned over. `dependencyName` names a `dependency:<target>` finding.
 */
export function driftMetricLabel(
    metricKey: string,
    basis?: SignalBasis | null,
    dependencyName?: string | null,
): string {
    if (metricKey.startsWith('envelope:')) {
        return `Envelope bound: ${metricKey.slice('envelope:'.length)}`;
    }
    const dep = dependencyMetricTarget(metricKey);
    if (dep !== null) return `Dependency ${dependencyName || dep}`;
    // Agent findings are keyed `<code>:<identity>`.
    const agentCode = agentFindingOfMetric(metricKey);
    if (agentCode !== null) return agentFindingLabel(agentCode);
    // Shape findings (one run used a never-seen tool or tool
    // combination) and reach findings (a requester exceeded a reach limit).
    if (metricKey.startsWith('shape:new_tool:')) {
        return `New tool: ${metricKey.slice('shape:new_tool:'.length)}`;
    }
    if (metricKey.startsWith('shape:new_tool_set:')) return 'New combination of tools';
    if (metricKey.startsWith('reach:')) return 'Record reach limit';
    const label =
        DRIFT_METRIC_LABELS[metricKey] ??
        metricKey.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
    return basis === 'segments' ? label.replace(/per run\b/, 'per served call') : label;
}

/** "own runs" / "served calls" — the population a baseline describes. */
export const SIGNAL_BASIS_LABELS: Record<SignalBasis, string> = {
    runs: 'own runs',
    segments: 'served calls',
};

/**
 * Plain-language labels for the cause ladder.
 *
 * `unexplained` says so rather than dressing itself up — an operator who
 * chases one fabricated explanation stops believing the true ones.
 */
export const CAUSE_LABELS: Record<AttributedCause, string> = {
    incident_window: 'Inside a declared incident window',
    contract_change: 'Contract changed',
    agent_release: 'The agent shipped a new release',
    model_change: 'The model changed',
    prompt_change: 'The instructions changed',
    dependency_change: 'A tool definition changed',
    dependency_degraded: 'A tool is degrading',
    composition_change: 'The system composition changed',
    corpus_change: 'The retrieval collections changed',
    input_drift: 'The inputs changed, not the system',
    traffic_mix: 'A new caller appeared',
    unexplained: 'No cause identified',
    contract_breach: 'A signed envelope bound was exceeded',
};

// ---------------------------------------------------------------------------
// Contracts, autonomy and the lifecycle gate
// ---------------------------------------------------------------------------

export type ContractStatus = 'draft' | 'active' | 'superseded' | 'rejected';

export type AutonomyLevel =
    | 'autonomous'
    | 'approval_required'
    | 'restricted'
    | 'suspended';

/** One entry in a contract diff. */
export interface DiffEntry {
    kind: string;
    detail: string;
    section?: string;
    path?: string;
    id?: string;
    field?: string;
    from?: unknown;
    to?: unknown;
    /** Set on an outcome that stopped counting toward the
     *  baseline: its runs leave the success rate, the cost of the work and
     *  drift detection, so the approver must read why. */
    called_out?: boolean;
    /** `dependency_contract_changed`: the service's
     *  contract versions either side of the change. */
    from_version?: number | null;
    to_version?: number | null;
    /** The service's own classified diff (one level deep). `null` when the
     *  service's contracts could not be resolved. */
    dependency_diff?: DependencyContractDiff | null;
    /** `agent_implementation`: the member agent whose
     *  declaration changed (`field` = approved_versions | require_release |
     *  implementation_name), and the version pattern added or removed. */
    agent?: string;
    agent_id?: string;
    version?: string;
}

/** A shared service's own diff, nested inside its caller's entry. */
export interface DependencyContractDiff {
    widened: DiffEntry[];
    narrowed: DiffEntry[];
    changed: DiffEntry[];
}

/**
 * Split by *direction*, not by field. `widened` is the list an approver has
 * to read; everything else is context.
 */
export interface DiffSummary {
    widened: DiffEntry[];
    narrowed: DiffEntry[];
    changed: DiffEntry[];
    widened_count: number;
    narrowed_count: number;
    changed_count: number;
    has_widened_permissions: boolean;
}

export interface AISystemContract {
    id: string;
    ai_system_id: string;
    version: number;
    contract_hash: string;
    short_hash: string;
    status: ContractStatus;
    diff_from_version?: number | null;
    diff_summary?: DiffSummary | null;
    lifecycle_stage_at_promotion?: string | null;
    dry_run_summary?: Record<string, unknown> | null;
    replay_run_id?: string | null;
    created_by?: string | null;
    created_at?: string | null;
    approved_by?: string | null;
    approved_at?: string | null;
    promoted_at?: string | null;
    rejection_reason?: string | null;
    is_active: boolean;
    is_approved: boolean;
    /** Only present on single-contract responses. */
    resolved_closure?: Record<string, unknown> | null;
}

export interface ContractListResponse {
    contracts: AISystemContract[];
    total: number;
    active_version?: number | null;
    /** Live config no longer matches the active contract (conformance). */
    config_drifted: boolean;
}

export interface MintContractResponse {
    contract: AISystemContract;
    /** False when config already matched the latest contract. */
    created: boolean;
}

export interface GateRequirement {
    key: string;
    label: string;
    satisfied: boolean;
    detail?: string | null;
}

export interface TransitionOption {
    to_stage: string;
    allowed: boolean;
    requirements: GateRequirement[];
    unmet: GateRequirement[];
}

/** Where a deprecated system's capability went, if anywhere. */
export interface Succession {
    successor_system_id?: string | null;
    successor_name?: string | null;
    /** True = explicitly withdrawn. null = the question was never put. */
    no_successor_declared?: boolean | null;
}

export interface SuccessorCandidate {
    id: string;
    name: string;
    lifecycle_stage?: string | null;
}

/** One lifecycle transition as recorded: the declared approver and
 *  the admin who actually performed it, kept apart on purpose. */
export interface LifecycleEvent {
    id: string;
    ai_system_id: string;
    /** `stage_transition` (history) | `agent.new_release` (events). */
    event_type?: string;
    /** agent.new_release: the release id. */
    subject_id?: string | null;
    /** agent.new_release: agent_id, agent_name, release_id, name, version,
     *  build, trust, approved. */
    details?: Record<string, unknown> | null;
    from_stage?: string | null;
    /** Null only on events that move no stage. */
    to_stage: string | null;
    approved_by?: string | null;
    recorded_by: string;
    successor_system_id?: string | null;
    no_successor?: boolean;
    note?: string | null;
    at?: string | null;
}

export interface LifecycleResponse {
    ai_system_id: string;
    /** Stage transitions only, newest first (`history[0]` = "in this stage
     *  since"). */
    history?: LifecycleEvent[];
    /** Events that move no stage, newest first:
     *  `agent.new_release` for each release a member agent was first seen
     *  running. */
    events?: LifecycleEvent[];
    current_stage?: string | null;
    eu_ai_act_risk_tier?: string | null;
    autonomy_level?: AutonomyLevel | null;
    transitions: TransitionOption[];
    succession?: Succession | null;
    candidate_successors?: SuccessorCandidate[];
    /** Set once deprecated — when this system becomes retirable. */
    retirement?: RetirementReadiness | null;
}

/** When a deprecated system can be retired, not merely whether it can yet. */
export interface RetirementReadiness {
    quiet_days_required: number;
    last_run_at?: string | null;
    retirable_at?: string | null;
    retirable: boolean;
    days_remaining: number;
}

export interface TransitionRequest {
    to_stage: string;
    approved_by?: string;
    successor_id?: string;
    no_successor?: boolean;
}

// ---------------------------------------------------------------------------
// External evidence attachments — pointers with fingerprints, not
// documents. Read by the lifecycle gate (high-risk FRIA floor) and the
// Assurance Report.
// ---------------------------------------------------------------------------

/** Every kind the control plane accepts
 *  (`app/database/models/ai_system_evidence.py::EVIDENCE_KINDS`). One
 *  source of truth: `complianceEvidenceTypes.EVIDENCE_KINDS` mirrors that
 *  set, and this alias keeps the evidence panel and the compliance console
 *  on the same label map, so a kind the API accepts can never render as a
 *  raw key here. */
export type EvidenceKind = EvidencePointerKind;

export const EVIDENCE_KIND_LABELS: Record<EvidenceKind, string> = EVIDENCE_KIND_LABEL;

export interface AISystemEvidenceItem {
    id: string;
    ai_system_id: string;
    kind: EvidenceKind;
    title: string;
    notes?: string | null;
    uri?: string | null;
    sha256?: string | null;
    assessor?: string | null;
    /** Produced by a party separate from the team that built or operates
     *  the system. Null means unstated — a different claim from "no". */
    independent?: boolean | null;
    produced_at?: string | null;
    valid_until?: string | null;
    /** The dependency edge a `supplier_information` pointer
     *  covers. NULL on every other kind, and on a supplier pointer whose
     *  edge was removed (the agreement is kept, detached). */
    dependency_id?: string | null;
    is_expired: boolean;
    created_by?: string | null;
    updated_by?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
}

export interface EvidenceListResponse {
    evidence: AISystemEvidenceItem[];
    total: number;
}

export interface EvidenceCreateRequest {
    kind: EvidenceKind;
    title: string;
    notes?: string;
    uri?: string;
    sha256?: string;
    assessor?: string;
    independent?: boolean;
    produced_at?: string;
    valid_until?: string;
    /** Required for `supplier_information` (one of this system's edges),
     *  rejected (422) on every other kind. */
    dependency_id?: string;
}

// ---------------------------------------------------------------------------
// Operating envelope — the "what it should achieve" half of the
// contract, and the per-term evaluation the report renders.
// ---------------------------------------------------------------------------

export type EnvelopeTermKey =
    // Enforced ceilings for ONE run — refused at the gateway.
    | 'max_cost_per_run_usd'
    | 'max_tokens_per_run'
    | 'max_llm_calls_per_run'
    | 'max_delegation_depth_per_run'
    | 'max_duration_seconds_per_run'
    | 'max_actions_per_run'
    // Enforced: how many runs one credential may start in a window, and
    // the window's length in seconds.
    | 'max_new_runs_per_window'
    | 'new_runs_window_seconds'
    // Graded over the reporting window — never blocks a call.
    | 'max_avg_cost_per_completed_task_usd'
    | 'max_monthly_cost_usd'
    | 'min_completion_rate'
    | 'max_error_rate'
    | 'max_abandoned_rate'
    | 'max_approval_escalation_rate'
    // Keyed per outcome: `<term>:<outcome>[.<variant>]`.
    | `max_outcome_share:${string}`
    | `avg_cost_per_outcome:${string}`;

/**
 * One term an operating envelope can declare, as the Control Plane serves
 * it: which way is stricter, its unit, label and signal, whether the gateway
 * refuses calls on it (`enforced`) and whether the report grades it
 * (`graded`), whether it is keyed per outcome (`parameterized`), its
 * explanation (`help`) and, for a term that applies when another is
 * declared, its default.
 */
export type EnvelopeTermSpec = Answered<components['schemas']['EnvelopeTerm']>;

export type EnvelopeResponse = Answered<components['schemas']['EnvelopeResponse']>;

export type EnvelopeTermStatus = 'met' | 'breached' | 'insufficient_evidence' | 'not_graded';

/** A term's report status in words. `not_graded` is an enforced term the
 *  report states without grading; a status this table does not hold is
 *  shown as sent. */
const ENVELOPE_TERM_STATUS_LABELS: Record<string, string> = {
    met: 'met',
    breached: 'breached',
    insufficient_evidence: 'insufficient evidence',
    not_graded: 'enforced, not graded',
};

export function envelopeTermStatusLabel(status: string): string {
    return ENVELOPE_TERM_STATUS_LABELS[status] ?? status;
}

export interface EnvelopeTermResult {
    term: string;
    label: string;
    unit: 'usd' | 'rate';
    direction: 'max' | 'min';
    declared: number;
    observed?: number | null;
    status: EnvelopeTermStatus;
    severity?: string | null;
    detail?: string | null;
}

export interface EnvelopeEvaluation {
    terms: EnvelopeTermResult[];
    breached: number;
    insufficient: number;
    evaluated_runs: number;
    period_hours: number;
}

export type VerdictStatus =
    | 'assured'
    | 'assured_with_exceptions'
    | 'violations'
    | 'insufficient_evidence';

// The label carries its basis. Plain "Assured" read as a verdict on the
// system; what is asserted is envelope conformance over observed runs.
export const VERDICT_LABELS: Record<VerdictStatus, string> = {
    assured: 'Assured · operating envelope',
    assured_with_exceptions: 'Assured, with exceptions · operating envelope',
    violations: 'Contract violations',
    insufficient_evidence: 'Insufficient evidence',
};

export const VERDICT_CLASSES: Record<VerdictStatus, string> = {
    assured: 'bg-green-100 text-green-800',
    assured_with_exceptions: 'bg-amber-100 text-amber-800',
    violations: 'bg-red-100 text-red-800',
    insufficient_evidence: 'bg-slate-100 text-slate-700',
};

export interface ReportVerdict {
    status: VerdictStatus;
    conclusion: string;
    /** Spec 1.2 — what the verdict was measured against. `observed_history`
     *  means no approved envelope was in force: the verdict is conditional
     *  and formal assurance is incomplete. */
    basis?: 'signed_envelope' | 'observed_history' | 'reported_telemetry';
    envelope_terms_breached: number;
    envelope_terms_insufficient: number;
    exceptions_total: number;
    open_drift_findings: number;
    /** Critical dependencies not healthy in the period; empty when all are. */
    failing_dependencies: string[];
}

export const CONTRACT_STATUS_CLASSES: Record<ContractStatus, string> = {
    active: 'bg-green-100 text-green-800',
    draft: 'bg-amber-100 text-amber-800',
    superseded: 'bg-slate-100 text-slate-700',
    rejected: 'bg-red-100 text-red-800',
};

export const AUTONOMY_LABELS: Record<AutonomyLevel, string> = {
    autonomous: 'Autonomous',
    approval_required: 'Approval required',
    restricted: 'Restricted (read-only)',
    suspended: 'Suspended',
};

/**
 * What each level actually does at the gate. The source of truth is the
 * enforcement path (core `services/autonomy.rs`), and the fact operators
 * most often get wrong is the last clause of each line: read-only work is
 * unaffected by everything except suspension.
 */
export const AUTONOMY_HELP: Record<AutonomyLevel, string> = {
    autonomous:
        'Acts without a human. Side-effecting calls — tools that change '
        + 'something, delegations to other agents — proceed on the system’s '
        + 'own authority.',
    approval_required:
        'A human answers first. Side-effecting calls are held and routed '
        + 'through the approval flow; completions and read-only calls are '
        + 'unaffected.',
    restricted:
        'Read-only. Completions and reads keep serving; anything that can '
        + 'change the world is denied. The safe holding state while something '
        + 'is being investigated.',
    suspended:
        'Everything is denied, reads included, until an operator restores it.',
};

/** How the level was arrived at — the same rule whatever the level is. */
export const AUTONOMY_RESOLUTION_HELP =
    'Resolved most-restrictive-wins across the group chain and the acting '
    + 'agent: an agent identity can be more restricted than its AI System, '
    + 'never less. A level nobody has set does not vote.';

/** Read as a warning ladder: the lower the level, the louder it should look. */
export const AUTONOMY_CLASSES: Record<AutonomyLevel, string> = {
    autonomous: 'bg-slate-100 text-slate-700',
    approval_required: 'bg-blue-100 text-blue-800',
    restricted: 'bg-amber-100 text-amber-800',
    suspended: 'bg-red-100 text-red-800',
};

// ---------------------------------------------------------------------------
// Response policies (the closed loop) and replay
// ---------------------------------------------------------------------------

export type SignalKind = 'drift' | 'liveness';
export type ResponseSeverity = 'low' | 'medium' | 'high' | 'critical';

export interface ResponseAction {
    kind: 'notify' | 'set_autonomy' | 'pin_model' | 'tighten_grants' | 'open_incident';
    level?: AutonomyLevel;
    targets?: string[];
    classes?: string[];
    [key: string]: unknown;
}

export interface ResponsePolicy {
    id: string;
    ai_system_id: string;
    name: string;
    description?: string | null;
    enabled: boolean;
    signal_kind: SignalKind;
    drift_class?: DriftClass | null;
    severity_at_least: ResponseSeverity;
    actions: ResponseAction[];
    allow_suspend: boolean;
    require_human_to_restore: boolean;
    response_cooldown_seconds: number;
    last_fired_at?: string | null;
    last_fired_drift_event_id?: string | null;
    fire_count: number;
    is_actuating: boolean;
    target_autonomy?: AutonomyLevel | null;
    created_by?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
}

export interface ResponsePolicyListResponse {
    policies: ResponsePolicy[];
    total: number;
    /** Policies applying here but attached to an ancestor. */
    inherited_count: number;
}

/**
 * Autonomy resolves min-wins *down* the tree, so a policy on a parent
 * restricts every inheriting system below it.
 */
export interface BlastRadius {
    target_id: string;
    inherited_by: string[];
    total: number;
    is_wide: boolean;
}

export interface PlannedActionView {
    kind: string;
    params: Record<string, unknown>;
    inverse?: Record<string, unknown> | null;
}

export interface PolicyPreview {
    ai_system_id: string;
    current_autonomy?: AutonomyLevel | null;
    blast_radius?: BlastRadius | null;
    would_do: PlannedActionView[];
    would_refuse: { code: string; detail: string }[];
    warnings: string[];
}

export interface ResponseRecord {
    drift_event_id: string;
    detected_at?: string | null;
    drift_class: DriftClass;
    severity: ResponseSeverity;
    attributed_cause?: string | null;
    acted: boolean;
    policy_id?: string | null;
    policy_name?: string | null;
    actions: PlannedActionView[];
    refusals: { code: string; detail: string }[];
    /** What an action that WAS taken does not cover (e.g. an approval hold
     *  does not stop plain API keys or people that act without a run scope). */
    limitations?: ResponseLimitation[];
    blast_radius?: BlastRadius | null;
    responded_at?: string | null;
}

export interface ResponseLimitation {
    code: string;
    detail: string;
    unbound_active_keys?: number | null;
    recent_runs?: number | null;
    unbound_recent_runs?: number | null;
    recent_days?: number | null;
}

export interface ResponseHistory {
    responses: ResponseRecord[];
    total: number;
    acted_count: number;
    /** Findings the loop has not considered. A number that stays high means it stopped. */
    pending_count: number;
}

export type ReplayGateState =
    | 'no_suite'
    | 'never_run'
    | 'not_replayable'
    | 'running'
    | 'passing'
    | 'failing';

/** One execution of a suite against a candidate contract.
 *
 * `matched` / `shape_changed` / `policy_changed` / `outcome_changed` are the
 * per-case verdicts rolled up. They are reported separately rather than as one
 * pass/fail count because they mean different things to a release decision: a
 * changed *shape* is usually a schema edit, while a changed *outcome* is a call
 * that would now be allowed or denied differently — and only the second is a
 * reason to stop a promotion.
 *
 * `skipped` is excluded from `pass_rate` entirely: counting skips as passes
 * would let a suite whose every case was skipped report 100%.
 */
export interface ReplayRun {
    id: string;
    suite_id: string;
    ai_system_id: string;
    contract_id?: string | null;
    status: string;
    triggered_by?: string | null;
    triggered_by_actor?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
    total_cases: number;
    matched: number;
    shape_changed: number;
    policy_changed: number;
    outcome_changed: number;
    failed: number;
    skipped: number;
    pass_rate?: number | null;
    /** A COUNT, not a flag — outcome changes on cases marked critical.
     *  Rendering it with `x && …` puts a literal 0 on the page. */
    critical_outcome_changed: number;
    error_message?: string | null;
    is_finished: boolean;
}

export interface ReplayStatus {
    suite_id?: string | null;
    suite_name?: string | null;
    bound: boolean;
    replayable: boolean;
    not_replayable_reason?: string | null;
    enabled: boolean;
    pass_threshold?: number | null;
    case_count: number;
    coverage: Record<string, number>;
    last_run?: ReplayRun | null;
    gate_state: ReplayGateState;
}

export interface BuildSuiteRequest {
    name: string;
    description?: string;
    target_case_count?: number;
    window_days?: number;
    pass_threshold?: number | null;
    /** Bind to the promotion gate immediately. Off by default on purpose: an
     *  operator should read the coverage report before a suite starts deciding
     *  whether releases ship. */
    bind_to_gate?: boolean;
}

export interface BuildSuiteResponse {
    suite_id: string;
    name: string;
    eligible_runs: number;
    cases_created: number;
    coverage: Record<string, number>;
    /** Strata present in the population but absent from the sample. A gate is
     *  only as good as what its evidence covers. */
    coverage_gaps: string[];
    bound_to_gate: boolean;
    warnings: string[];
}

export interface RequestRunResponse {
    replay_run_id: string;
    status: string;
    note: string;
}

export const REFUSAL_LABELS: Record<string, string> = {
    no_matching_policy: 'No policy matched this finding',
    cooldown_active: 'A response already fired recently',
    already_responded: 'Already responded to',
    would_loosen: 'Would have given the system more freedom',
    no_change_needed: 'Already at the target level',
};

export const REPLAY_GATE_LABELS: Record<ReplayGateState, string> = {
    no_suite: 'No replay suite bound',
    never_run: 'Suite has never been run',
    not_replayable: 'Suite is marked not replayable',
    running: 'Replay in progress',
    passing: 'Last replay passed',
    failing: 'Last replay did not pass',
};

// ---------------------------------------------------------------------------
// Health score + the Assurance Report
// ---------------------------------------------------------------------------

export type HealthState =
    | 'healthy'
    | 'learning'
    | 'degraded'
    | 'drifting'
    | 'stalled'
    /** Calling, but every one of its latest finished runs failed — down now,
     *  whatever the 30-day rates say. */
    | 'failing'
    | 'silent'
    | 'suspended'
    | 'unknown';

/** One finished run the "failing now" check read. */
export interface RecentRun {
    run_id: string;
    terminal_state: string;
    /** The run's class, declared outcome and (failed) cause. */
    run_class?: string | null;
    outcome_id?: string | null;
    outcome_conformance?: string | null;
    error_cause?: string | null;
    ended_at?: string | null;
    failed: boolean;
}

/** THE down rule over the system's own latest finished runs: all failed ⇒
 *  `failing`; some failed ⇒ reliability capped at 50%. */
export interface RecentRunsVerdict {
    verdict: 'ok' | 'degraded' | 'down' | null;
    considered: number;
    failed: number;
    /** How many latest finished runs the rule reads. */
    rule_runs: number;
    /** Newest first. */
    latest: RecentRun[];
    /** THE success rate over the health window — shown for contrast. */
    window_success_rate?: number | null;
    window_hours?: number | null;
}

/** What each part of the health verdict is computed over. */
export interface HealthWindows {
    /** Every component is graded over this trailing window. Fixed — it does
     *  NOT follow the page's range selector. */
    health_period_hours: number;
    /** The latest this-many finished runs are judged for `failing`. */
    recent_runs: number;
    /** A stored verdict older than this is recomputed when read. */
    freshness_seconds: number;
}

export interface SystemHealth {
    ai_system_id: string;
    health_state: HealthState;
    /** Worst-of the components, not their mean. Null when unknown. */
    health_score?: number | null;
    liveness_score?: number | null;
    behaviour_score?: number | null;
    reliability_score?: number | null;
    cost_score?: number | null;
    conformance_score?: number | null;
    /** Human-oversight effectiveness. Null means no approval gate was
     *  exercised in the window, which is NOT a failing score. */
    oversight_score?: number | null;
    /** The kind whose signal profile produced this verdict. */
    system_kind?: string | null;
    /** Per-signal {score, level}. `level` is enforced | advisory |
     *  not_applicable. Render not_applicable as "n/a" — never as 0% and
     *  never as a pass; a signal that was not asked has not been satisfied. */
    signal_levels?: Record<string, { score: number | null; level: string }>;
    /** Signals this kind does not fully enforce, with the reason. Disclosed
     *  so choosing a kind cannot quietly drop an inconvenient signal. */
    profile_relaxations?: { signal: string; level: string; reason: string }[];
    chain_integrity_pct?: number | null;
    baseline_state?: string | null;
    runs_24h: number;
    open_drift_events: number;
    reasons: string[];
    /** Whether enough runs were observed intact for the score to mean anything. */
    is_trustworthy: boolean;
    /** When this verdict was computed. A health state of unknown age is not
     *  a health state — the strip renders this as "as of". */
    computed_at?: string | null;
    /** `segments` when the system roots no runs in the
     *  window but served calls as a shared service: every signal is then
     *  computed from the calls it served. */
    signal_basis?: SignalBasis;
    /** Every declared dependency's health as this system
     *  sees it: `tested` for shared services, `assumed` for external agents. */
    dependency_health?: DependencyHealthEntry[];
    /** Set when a critical shared-service dependency that is not healthy
     *  caps this system at degraded (reason `dependency`). */
    dependency_cap?: DependencyCap | null;
    /** The "failing now" verdict, stored with the state. Null for a shared
     *  service (judged on the calls it serves). */
    recent_runs?: RecentRunsVerdict | null;
    windows?: HealthWindows | null;
}

export interface AssuranceSummary {
    id: string;
    ai_system_id: string;
    period_hours: number;
    health_state?: string | null;
    contract_version?: number | null;
    contract_hash?: string | null;
    trigger: string;
    captured_by?: string | null;
    created_at?: string | null;
    /** Attested with the deployment audit key at capture time. */
    signed?: boolean;
    verdict_status?: VerdictStatus | null;
}

export interface AssuranceHistory {
    reports: AssuranceSummary[];
    total: number;
}

export interface HealthStyle {
    chip: string;
    stripe: string;
    label: string;
}

const HEALTH_STYLE_BY_STATE: Record<HealthState, HealthStyle> = {
    healthy: { chip: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300', stripe: 'bg-green-500', label: 'Healthy' },
    learning: { chip: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300', stripe: 'bg-blue-400', label: 'Still learning' },
    degraded: { chip: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300', stripe: 'bg-yellow-500', label: 'Degraded' },
    drifting: { chip: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300', stripe: 'bg-orange-500', label: 'Drifting' },
    stalled: { chip: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300', stripe: 'bg-orange-500', label: 'Stalled' },
    failing: { chip: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300', stripe: 'bg-red-600', label: 'Failing' },
    silent: { chip: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300', stripe: 'bg-orange-500', label: 'Silent' },
    suspended: { chip: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300', stripe: 'bg-red-600', label: 'Suspended' },
    unknown: { chip: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300', stripe: 'bg-gray-400', label: 'Not enough data' },
};

/**
 * THE colour and label of every health state — the strip, the Signals
 * overview, the Assurance panel and the Mission Control cards all read this
 * one map, so the same state can never be red on one surface and yellow on
 * the next (it was). Only `healthy` is green: `learning` and `unknown` are
 * the two states an optimistic UI rounds up, and "not measured yet" must
 * never look like "fine". Keyed by `HealthState`, so a new state does not
 * compile until it has an entry; typed by string for callers holding a raw
 * API value. See `assuranceStripe` for the combined worst-of ladder.
 */
export const HEALTH_STYLE: Record<string, HealthStyle> = HEALTH_STYLE_BY_STATE;

/** Labels of `HEALTH_STYLE`, for callers that only need the word. */
export const HEALTH_LABELS = Object.fromEntries(
    Object.entries(HEALTH_STYLE_BY_STATE).map(([k, v]) => [k, v.label]),
) as Record<HealthState, string>;

/** Chip classes of `HEALTH_STYLE`, for callers that only need the colour. */
export const HEALTH_CLASSES = Object.fromEntries(
    Object.entries(HEALTH_STYLE_BY_STATE).map(([k, v]) => [k, v.chip]),
) as Record<HealthState, string>;

export const VERDICT_STYLE: Record<string, { chip: string; label: string }> = {
    assured: { chip: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300', label: 'Assured' },
    assured_with_exceptions: { chip: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300', label: 'Assured w/ exceptions' },
    insufficient_evidence: { chip: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300', label: 'Insufficient evidence' },
    violations: { chip: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300', label: 'Violations' },
};

export const HEALTH_COMPONENTS: {
    key: keyof SystemHealth;
    label: string;
}[] = [
    { key: 'liveness_score', label: 'Liveness' },
    { key: 'behaviour_score', label: 'Behaviour' },
    { key: 'reliability_score', label: 'Reliability' },
    { key: 'cost_score', label: 'Cost' },
    { key: 'conformance_score', label: 'Conformance' },
    { key: 'oversight_score', label: 'Oversight' },
];

/* ── Trajectory search ─────────────────────────────────
   Run > Step (asserted phase) > Turn (loop pass) > Action (governed call).
   Every level reports its provenance: a turn the gateway inferred is
   never presented as one the client reported.                          */

export interface TrajectoryStep {
    step_id: string;
    step_name?: string | null;
    turns: number;
    actions: number;
    cost_usd: number;
    tokens: number;
    first_at?: string | null;
}

export interface TrajectoryTurn {
    turn_id?: string | null;
    turn_seq?: number | null;
    /** Set when the client asserted no turn id and the gateway inferred one. */
    inferred_turn?: number | null;
    step_id?: string | null;
    step_name?: string | null;
    action_count: number;
    tool_action_count: number;
    opened_by_model?: string | null;
    tools: string[];
    cost_usd: number;
    first_at?: string | null;
    blocked: boolean;
}

export interface TrajectoryResponse {
    run_id: string;
    root_task_id?: string | null;
    action_count: number;
    turn_count: number;
    turn_attribution?: string | null;
    step_count: number;
    /** True when no steps were asserted — NOT the same as "one phase". */
    unphased: boolean;
    steps: TrajectoryStep[];
    turns: TrajectoryTurn[];
}

export interface ConsequencePair {
    trip_action_no: number;
    trip_turn_id?: string | null;
    trip_step_id?: string | null;
    trip_step_name?: string | null;
    trip_check?: string | null;
    trip_blocked?: boolean | null;
    trip_pii?: boolean | null;
    trip_policy_denied?: boolean | null;
    trip_at?: string | null;
    after_action_no: number;
    after_turn_id?: string | null;
    after_step_id?: string | null;
    after_step_name?: string | null;
    after_type?: string | null;
    after_capability?: string | null;
    after_server_id?: string | null;
    after_at?: string | null;
    actions_apart: number;
}

export interface ConsequenceResponse {
    run_id: string;
    within: number;
    pairs: ConsequencePair[];
}

export interface ActionHit {
    root_task_id?: string | null;
    step_id?: string | null;
    step_name?: string | null;
    turn_id?: string | null;
    turn_seq?: number | null;
    proxy_type?: string | null;
    mcp_capability_name?: string | null;
    llm_model_used?: string | null;
    success?: boolean | null;
    guardrail_blocked?: boolean | null;
    argument_policy_denied?: boolean | null;
    duration_ms?: number | null;
    llm_estimated_cost_usd?: number | null;
    timestamp?: string | null;
    correlation_id?: string | null;
    ai_system_id?: string | null;
    terminal_state?: string | null;
}

export interface ActionSearchResponse {
    actions: ActionHit[];
    total: number;
}


// ── Run interventions ─────────────────────────────────

/**
 * An operator's intervention on one run.
 *
 * `status` and `took_effect` answer different questions and both matter:
 * `requested` means the abort was written, `effective` means a gateway
 * actually refused something. "I clicked stop" and "the run stopped" are
 * different claims, and a control that conflates them is one nobody trusts
 * in an incident.
 */
export interface RunIntervention {
    id: string;
    tenant_id: string;
    root_task_id: string;
    action: 'abort';
    reason: string;
    requested_at?: string | null;
    first_blocked_at?: string | null;
    steps_blocked: number;
    status: 'requested' | 'effective' | 'closed';
    /** True once the abort has actually refused an action. */
    took_effect: boolean;
    created_by?: string | null;
    created_at?: string | null;
    updated_by?: string | null;
    updated_at?: string | null;
}

export interface LiveRun {
    root_task_id: string;
    first_step_at?: string | null;
    last_step_at?: string | null;
    step_count: number;
    ai_system_id?: string | null;
    intervention?: {
        id: string;
        status: string;
        reason: string;
    } | null;
}

export interface LiveRunsResponse {
    runs: LiveRun[];
    total: number;
    window_seconds: number;
}

export interface AbortRunResponse {
    intervention: RunIntervention;
    /** True when an abort was already in force; the operator's intent is
     *  satisfied either way, so this is information, not an error. */
    already_requested: boolean;
    note?: string;
}

/** A millisecond span in the largest two units that matter:
 *  `850 ms`, `41 s`, `3 m 05 s`, `11 h 32 m`, `2 d 4 h`. */
export function humanDurationMs(ms: number): string {
    const v = Math.max(0, Math.round(ms));
    if (v < 1000) return `${v} ms`;
    const s = Math.floor(v / 1000);
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} m ${String(s % 60).padStart(2, '0')} s`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} h ${String(m % 60).padStart(2, '0')} m`;
    return `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** One line on how long a run took, separating the system's own execution
 *  from time it spent parked awaiting a human approval:
 *  `active 41 s · awaited a person 11 h 32 m`. A run that never waited reads
 *  `active 41 s`. `null` when the run has no duration at all.
 *
 *  The split is the point: the per-run duration ceiling measures active
 *  time, so an elapsed figure shown alone makes a slow approval look like a
 *  breach. */
export function describeRunTiming(
    run: Pick<RunSummary, 'duration_ms' | 'active_duration_ms' | 'awaiting_human_ms'>,
): string | null {
    const waited = run.awaiting_human_ms ?? 0;
    const active =
        run.active_duration_ms ??
        (run.duration_ms !== null && run.duration_ms !== undefined
            ? Math.max(0, run.duration_ms - waited)
            : null);
    if (active === null) return null;
    const parts = [`active ${humanDurationMs(active)}`];
    if (waited > 0) parts.push(`awaited a person ${humanDurationMs(waited)}`);
    return parts.join(' · ');
}

// ============================================================================
// COMPOSITE AI SYSTEMS
// ============================================================================
//
// Mirrors `GET /v1/admin/ai-systems/{id}/composition` and
// `GET|POST|PATCH|DELETE /v1/admin/ai-systems/{id}/dependencies[/{edge_id}]`.
//
// Three roles: components are MEMBERS of the system's resource group
// (agent identities); shared services and external agents are declared
// DEPENDENCY edges. The composition payload is built from the contract
// closure, so what the Composition tab draws is what an approver signs.

/** shared_service = another AI System in this tenant; external = an agent
 *  card with no Brutor AI System behind it (assessed at the boundary only). */
export type DependencyRole = 'shared_service' | 'external';

/** critical = the caller cannot do its job without it; degradable = the
 *  caller has a declared fallback. */
export type DependencyCriticality = 'critical' | 'degradable';

/** Per-dependency health. `assumed` is the external role: Brutor sees only
 *  the boundary, so the node says so rather than implying assurance. */
export type DependencyHealth = 'healthy' | 'degraded' | 'idle' | 'unknown' | 'assumed';

/** One declared edge (`ai_system_dependencies`). */
export interface DependencyEdge {
    id: string;
    ai_system_id: string;
    role: DependencyRole;
    depends_on_system_id: string | null;
    depends_on_system_name: string | null;
    agent_card_id: string | null;
    agent_card_name: string | null;
    criticality: DependencyCriticality;
    fallback: string | null;
    contributes_to_decision: boolean;
    separability_rationale: string | null;
    /** The sealed `ai.brutor.declaration` of the separability claim; null =
     *  not sealed (no claim, or a seal that has not succeeded yet). */
    separability_record_id: string | null;
    separability_sealed_at: string | null;
    /** NULL = follow the target's active contract; otherwise a contract id
     *  of the target (shared services only). */
    contract_pin: string | null;
    /** The legal entity that provides the dependency when it is not the
     *  deployer (required for external; EU AI Act Art 25(4)). */
    supplier_legal_entity: string | null;
    /** The delegate outcomes (`<outcome>[.<variant>]`) this
     *  system is built to handle; null = not declared (no check). */
    expected_outcomes?: string[] | null;
    /** Stored assumptions (`DependencyAssumptions` keys on every edge
     *  written through the strict schema; typed as the wire's open object). */
    assumptions: Record<string, unknown>;
    declared_by: string | null;
    declared_at: string | null;
    updated_by: string | null;
    updated_at: string | null;
    /** Write responses only: what THIS save sealed; null when it sealed
     *  nothing (optional: list responses omit it). */
    separability_seal?: DependencySealStatus | null;
}

/** The outcome of sealing a declaration on a write (a sealed record). */
export interface DependencySealStatus {
    sealed: boolean;
    record_id?: string | null;
    seal_error?: string | null;
    seal_detail?: string | null;
}

/** Evidence an external dependency provides of its own. */
export type DependencyEvidenceProvided =
    | 'signed_agent_card'
    | 'attestation'
    | 'certification'
    | 'contract'
    | 'other';

export const DEPENDENCY_EVIDENCE_PROVIDED: readonly DependencyEvidenceProvided[] = [
    'signed_agent_card',
    'attestation',
    'certification',
    'contract',
    'other',
];

export const DEPENDENCY_EVIDENCE_PROVIDED_LABELS: Record<DependencyEvidenceProvided, string> = {
    signed_agent_card: 'Signed agent card',
    attestation: 'Attestation',
    certification: 'Certification',
    contract: 'Contract',
    other: 'Other',
};

/**
 * The root's side of an assume-guarantee contract — what it
 * assumes about a dependency it cannot test. The server schema is STRICT:
 * an unknown key is a 422, so the form only ever sends these keys.
 */
export interface DependencyAssumptions {
    data_classes_shared?: string[] | null;
    retention_at_delegate?: string | null;
    human_oversight_at_delegate?: boolean | null;
    /** 0–100. */
    expected_availability_pct?: number | null;
    /** Integer, >= 0. */
    expected_p95_latency_ms?: number | null;
    evidence_provided?: DependencyEvidenceProvided[] | null;
}

export const DEPENDENCY_ASSUMPTION_LABELS: Record<keyof DependencyAssumptions, string> = {
    data_classes_shared: 'Data classes shared',
    retention_at_delegate: 'Retention at the delegate',
    human_oversight_at_delegate: 'Human oversight at the delegate',
    expected_availability_pct: 'Expected availability',
    expected_p95_latency_ms: 'Expected p95 latency',
    evidence_provided: 'Evidence it provides',
};

export interface DependencyListResponse {
    dependencies: DependencyEdge[];
    total: number;
}

/** The writable fields of an edge — POST body, and PATCH body (partial). */
export interface DependencyDraft {
    role: DependencyRole;
    depends_on_system_id: string | null;
    agent_card_id: string | null;
    criticality: DependencyCriticality;
    fallback: string | null;
    contributes_to_decision: boolean;
    separability_rationale: string | null;
    contract_pin: string | null;
    supplier_legal_entity: string | null;
    assumptions: DependencyAssumptions;
    /** null = not declared; a list = what the caller is built to handle. */
    expected_outcomes: string[] | null;
}

export interface DependencyObserved {
    calls: number;
    answered: number;
    failed: number;
    rejected: number;
    timed_out: number;
    blocked: number;
    last_seen_at: string | null;
}

/**
 * A delegation-agent node: one per agent card the system's a2a_call grants
 * reach, joined with its declared edge — or, when `declared` is false, a
 * card reached with no edge (the edge fields are then null) — plus what the
 * run segments observed in the window.
 */
export interface CompositionDependency {
    id: string | null;
    ai_system_id: string;
    role: DependencyRole | null;
    depends_on_system_id: string | null;
    depends_on_system_name: string | null;
    agent_card_id: string | null;
    agent_card_name: string | null;
    criticality: DependencyCriticality | null;
    fallback: string | null;
    contributes_to_decision: boolean | null;
    separability_rationale: string | null;
    separability_record_id: string | null;
    separability_sealed_at: string | null;
    contract_pin: string | null;
    supplier_legal_entity: string | null;
    assumptions: Record<string, unknown>;
    declared_by: string | null;
    declared_at: string | null;
    updated_by: string | null;
    updated_at: string | null;
    /** A dependency edge is declared for this node. */
    declared: boolean;
    /** An enabled, non-deny a2a_call grant of a member agent names the card. */
    granted: boolean;
    health: DependencyHealth;
    observed: DependencyObserved;
    target_contract: { id: string; version: number; pinned: boolean } | null;
    /** This system's connection to the delegation agent in the window. */
    connection: CompositionConnection;
}

/** One connector line of the Simple graph. THE rule is the
 *  control plane's `ai_system_connection_status.connection_status`:
 *  down = a down health signal (disabled, retired, unhealthy / circuit
 *  open, not answering) or the last 3 calls all failed or were blocked;
 *  degraded = a degraded health signal or error+block rate >= 5 %;
 *  ok = calls in the window and neither; idle = no calls in the window
 *  and no negative health signal — no evidence, NOT "all good". */
export type CompositionConnectionStatus = 'ok' | 'degraded' | 'down' | 'idle';

export interface CompositionConnection {
    status: CompositionConnectionStatus;
    /** Why, in words — the line's tooltip. */
    reason: string;
    calls: number;
    errors: number;
    /** Calls the gateway refused — holds for approval are not included. */
    blocked: number;
    /** Calls held for a human approval decision: counted apart, never a failure. */
    held: number;
    /** Of `held`: nobody decided before the approval timeout — a warning. */
    held_lapsed: number;
    last_call_at: string | null;
}

export interface CompositionInheritedFrom {
    id: string;
    name: string | null;
}

export interface CompositionResource {
    id: string;
    name: string | null;
    inherited_from: CompositionInheritedFrom | null;
    /** This system's own connection to the resource in the window. */
    connection: CompositionConnection;
}

/** An MCP server binding. `system` marks the platform's own system MCP
 *  servers (Agent Skills, …) — gateway plumbing, not something the
 *  system's owner connected; the simple graph leaves them out. */
export interface CompositionMcpResource extends CompositionResource {
    system: boolean;
    /** The mcp_servers id behind the binding (`id` is the tenant's
     *  server_config) — what a run action's `target_id` names. */
    mcp_server_id?: string | null;
}

export type CompositionPolicyKind =
    | 'guardrail'
    | 'argument_policy'
    | 'response_policy'
    | 'governance'
    | 'cache';

export interface CompositionPolicy {
    kind: CompositionPolicyKind;
    id: string | null;
    name: string;
    summary: string | null;
    inherited_from: CompositionInheritedFrom | null;
}

export type CompositionLimitKind =
    | 'budget'
    | 'rate'
    | 'tokens'
    | 'run_cap'
    | 'envelope'
    | 'concurrency';

export interface CompositionLimit {
    kind: CompositionLimitKind;
    key: string;
    /** Whatever the limit's value is (number, string, object) — shown via
     *  `summary`. */
    value: unknown;
    summary: string | null;
    inherited_from: CompositionInheritedFrom | null;
}

export interface CompositionSystemFacts {
    id: string;
    name: string;
    /** A `SystemKind` (open on the wire: rendered by label, else slug). */
    system_kind: string | null;
    owner: string | null;
    lifecycle_stage: string | null;
    autonomy_level: string | null;
    active_contract: { id: string; version: number } | null;
    risk_tier_declared: string | null;
    risk_tier_effective: string | null;
    effective_tier_sources: EffectiveTierSource[];
    separability_declared: boolean;
    /** Every separability claim touching this system,
     *  on either side, with its sealed declaration record. */
    separability_declarations: SeparabilityDeclaration[];
    /** Callers outside intended_client_systems are
     *  refused at the gateway (else the finding is advisory). */
    enforce_accepted_callers: boolean;
}

/** A non-contributing edge's separability claim, shown on BOTH systems. `declares` = this system's own edge; `declared_on` =
 *  a caller's edge to this system. */
export interface SeparabilityDeclaration {
    edge_id: string;
    ai_system_id: string;
    ai_system_name: string | null;
    depends_on_system_id: string | null;
    depends_on_system_name: string | null;
    agent_card_id: string | null;
    agent_card_name: string | null;
    side: 'declares' | 'declared_on';
    separability_rationale: string | null;
    record_id: string | null;
    sealed_at: string | null;
    sealed: boolean;
}

export interface CompositionLocationStep {
    id: string;
    name: string | null;
    group_type: string | null;
}

export interface ObservedUndeclared {
    system_id: string;
    system_name: string | null;
    calls: number;
    last_seen_at: string | null;
    connection: CompositionConnection;
}

export interface CompositionDependent {
    system_id: string;
    system_name: string | null;
    /** Accepted by this system (listed in its intended_client_systems). */
    declared: boolean;
    /** The caller declares a dependency edge to this system. */
    edge_declared: boolean;
    observed_calls: number;
    last_seen_at: string | null;
}

export interface CompositionClientLabel {
    label: string;
    declared: boolean;
    observed_calls: number;
}

export type CompositionFindingCode =
    | 'composition.undeclared_dependency'
    | 'composition.unaccepted_caller'
    | 'composition.declared_never_observed'
    | 'composition.role_mismatch'
    | 'composition.dependency_cycle'
    // Grants bound to declarations, and pins behind their target.
    | 'composition.grant_without_edge'
    | 'composition.edge_without_grant'
    | 'composition.dependency_pin_stale';

export type CompositionFindingSeverity = 'info' | 'warning' | 'critical';

export interface CompositionFinding {
    /** A `CompositionFindingCode`, or a code added since: so
     *  the wire type is open and unknown codes render by their message. */
    code: string;
    severity: CompositionFindingSeverity;
    message: string;
    subject_id: string | null;
}

/** A member agent: its declaration (read from the contract
 *  closure — the terms the contract signs) and its CURRENT release (the one
 *  seen last), or `release: null` when it never declared one. */
export interface CompositionAgent {
    id: string;
    name: string | null;
    implementation_name: string | null;
    approved_versions: string[];
    require_release: boolean;
    release: AgentReleaseRef | null;
}

export type HumanApprovalScope = 'always' | 'conditional';

export interface HumanApprovalGate {
    source: string;
    scope: HumanApprovalScope;
    lane: string;
    target: string;
    /** Empty = a real gate naming nothing a bound resource reports. */
    resource_ids: string[];
    detail: string;
}

export interface HumanApproval {
    autonomy_requires_approval: boolean;
    approval_window_seconds: number | null;
    gates: HumanApprovalGate[];
}

export interface AISystemCompositionView {
    system: CompositionSystemFacts;
    /** The observation window the observed figures cover. */
    window_hours: number;
    /** Root-first resource-group path ending with the system itself. */
    location: CompositionLocationStep[];
    /** The system's own member agent identities (components), each with
     *  its implementation declaration and current release. */
    agents: CompositionAgent[];
    resources: {
        models: CompositionResource[];
        mcp_servers: CompositionMcpResource[];
        skills: CompositionResource[];
        knowledge_bases: CompositionResource[];
    };
    /** The system → gateway line, from the system's stored health. */
    system_status: CompositionConnection;
    gateway: {
        policies: CompositionPolicy[];
        limits: CompositionLimit[];
    };
    /** Where the system is declared to wait for a person (from the closure). */
    human_approval: HumanApproval;
    dependencies: CompositionDependency[];
    observed_undeclared: ObservedUndeclared[];
    dependents: CompositionDependent[];
    client_labels: CompositionClientLabel[];
    findings: CompositionFinding[];
}

export const COMPOSITION_FINDING_LABELS: Record<CompositionFindingCode, string> = {
    'composition.undeclared_dependency': 'Undeclared dependency',
    'composition.unaccepted_caller': 'Unaccepted caller',
    'composition.declared_never_observed': 'Declared, never observed',
    'composition.role_mismatch': 'Role mismatch',
    'composition.dependency_cycle': 'Dependency cycle',
    'composition.grant_without_edge': 'Grant without a declared dependency',
    'composition.edge_without_grant': 'Dependency without a grant',
    'composition.dependency_pin_stale': 'Pinned contract is behind',
};

export const DEPENDENCY_ROLE_LABELS: Record<DependencyRole, string> = {
    shared_service: 'Shared service',
    external: 'External',
};

export const DEPENDENCY_CRITICALITY_LABELS: Record<DependencyCriticality, string> = {
    critical: 'Critical',
    degradable: 'Degradable',
};

export const DEPENDENCY_HEALTH_LABELS: Record<DependencyHealth, string> = {
    healthy: 'Healthy',
    degraded: 'Degraded',
    idle: 'Idle',
    unknown: 'Unknown',
    assumed: 'Assumed, not tested',
};

/** Worst first — the list's "worst dependency health" reads this order. */
export const DEPENDENCY_HEALTH_CLASSES: Record<DependencyHealth, string> = {
    degraded: 'bg-orange-100 text-orange-800',
    unknown: 'bg-slate-100 text-slate-700',
    assumed: 'bg-slate-100 text-slate-700',
    idle: 'bg-slate-100 text-slate-600',
    healthy: 'bg-green-100 text-green-800',
};

// ─── Dependency health in the root's verdict ────

/** Which signal of the dependency fails. `dependency` = a critical
 *  dependency OF the dependency fails (see `via_system_id`). */
export type DependencyFailingSignal =
    | 'liveness'
    | 'autonomy'
    | 'reliability'
    | 'behaviour'
    | 'cost'
    | 'conformance'
    | 'oversight'
    | 'review'
    | 'dependency';

export const DEPENDENCY_FAILING_SIGNAL_LABELS: Record<DependencyFailingSignal, string> = {
    liveness: 'liveness',
    autonomy: 'autonomy (suspended)',
    reliability: 'reliability',
    behaviour: 'behaviour (drifting)',
    cost: 'cost',
    conformance: 'conformance',
    oversight: 'oversight',
    review: 'review',
    dependency: 'its own dependency',
};

/** Unknown future signals render by their slug. */
export function failingSignalLabel(signal: string | null | undefined): string | null {
    if (!signal) return null;
    return DEPENDENCY_FAILING_SIGNAL_LABELS[signal as DependencyFailingSignal] ?? signal;
}

/** Did the root complete the runs in which a degradable dependency failed? */
export interface FallbackCheck {
    unanswered_calls: number;
    root_runs_completed: number;
    /** null = not exercised in the window; false = a run did not complete. */
    held: boolean | null;
}

/** An external dependency, seen only at the gateway boundary. */
export interface BoundaryMetrics {
    calls: number;
    errors: number;
    error_rate: number | null;
    p95_latency_ms: number | null;
    last_seen_at: string | null;
    /** Terminal A2A task states the root received
     *  (completed, failed, canceled, rejected, input_required, …) → calls.
     *  Optional in the report. */
    task_states?: Record<string, number>;
    /** Calls whose run lies inside an incident window: counted
     *  in `calls`, left out of errors, error_rate, p95 and task_states.
     *  Optional in the report. */
    excluded_calls?: number;
}

export type DependencyBasis = 'tested' | 'assumed';

/** One declared dependency's health, as the root sees it. */
export interface DependencyHealthEntry {
    edge_id: string;
    system_id: string | null;
    name: string | null;
    role: DependencyRole;
    criticality: DependencyCriticality;
    contributes_to_decision: boolean;
    status: DependencyHealth;
    failing_signal: string | null;
    via_system_id: string | null;
    via_system_name: string | null;
    /** `tested` = a shared service assured from its own evidence;
     *  `assumed` = an external agent, never a tested status. */
    basis: DependencyBasis;
    /** A critical shared service that is not healthy: the root is capped. */
    caps_root: boolean;
    note: string | null;
    fallback_check: FallbackCheck | null;
    boundary: BoundaryMetrics | null;
    assumptions: Record<string, unknown> | null;
}

/** Why the root's health is capped at degraded. */
export interface DependencyCap {
    reason: 'dependency';
    edge_id: string;
    system_id: string | null;
    name: string | null;
    failing_signal: string | null;
}

/** What a shared service did for its callers in the
 *  report's period (the report's `service` section; null unless the
 *  system's signals are on the segment basis). */
export interface ServiceFacts {
    served_calls: number;
    outcome_mix: Record<string, number>;
    /** Answered-class calls: answered + rejected (declining a task is an
     *  answer — `ai_system_down_rule.classify_segment_outcome`). */
    answered_calls: number;
    /** Declined by the service; part of `answered_calls`, shown apart. */
    rejected_calls: number;
    /** Refused by the gateway — never reached the service. */
    blocked_calls?: number;
    /** The reliability denominator: served − blocked. */
    reliability_calls?: number;
    /** answered_calls / reliability_calls. */
    success_rate: number | null;
    tool_calls: number;
    tool_errors: number;
    tool_error_rate: number | null;
    total_cost_usd: number;
    cost_per_answered_call: number | null;
    guardrail_blocks: number;
    policy_denials: number;
    approval_requests: number;
}

/** Supplier information (Art 25(4)) on file for one edge. */
export interface SupplierInformationBlock {
    /** An external edge, or a shared service with a supplier entity. */
    required: boolean;
    on_file: number;
    unexpired: number;
    evidence: {
        id: string;
        title: string;
        sha256: string | null;
        valid_until: string | null;
        expired: boolean;
    }[];
}

/** One edge of the dependency register, joined with its current status
 *  (the SAME dependency-health entry the report's health block carries). */
export interface CompositionReportEdge {
    edge_id: string;
    role: DependencyRole;
    depends_on_system_id: string | null;
    depends_on_system_name: string | null;
    agent_card_id: string | null;
    agent_card_name: string | null;
    criticality: DependencyCriticality;
    fallback: string | null;
    contributes_to_decision: boolean;
    /** Null when the edge contributes (there is no claim). */
    separability: {
        rationale: string | null;
        record_id: string | null;
        sealed_at: string | null;
        sealed: boolean;
    } | null;
    /** Shared services only: the contract it runs against. */
    contract: { id: string; version: number; hash: string | null; pinned: boolean } | null;
    assurance_basis: DependencyBasis;
    /** External edges only. */
    assumptions: Record<string, unknown> | null;
    supplier_legal_entity: string | null;
    supplier_information: SupplierInformationBlock;
    status: DependencyHealth | null;
    failing_signal: string | null;
    caps_root: boolean;
    boundary: BoundaryMetrics | null;
}

/** The assurance report's "Composition and dependencies" section (spec
 *  1.6). Frozen inside the report, so the digest and the
 *  signature cover it. Optional in the report schema. */
export interface CompositionReportSection {
    title: string;
    window_hours: number;
    /** An `AISystemRole` (open on the wire: rendered by label, else slug). */
    role: string | null;
    risk_tier_declared: string | null;
    risk_tier_effective: string | null;
    effective_tier_sources: EffectiveTierSource[];
    separability_declarations: SeparabilityDeclaration[];
    enforce_accepted_callers: boolean;
    edges: CompositionReportEdge[];
    callers: {
        system_id: string;
        system_name: string | null;
        /** Listed in this system's intended_client_systems. */
        accepted: boolean;
        observed_calls: number;
    }[];
    findings: CompositionFinding[];
    /** Each member agent's CURRENT release, its trust and approval.
     *  Optional in the section. */
    agents?: CompositionAgent[];
    /** Spec 1.7 — open agent implementation findings on the system. */
    agent_findings?: CompositionReportAgentFinding[];
}

/** One agent implementation finding as the report seals it. */
export interface CompositionReportAgentFinding {
    code: string;
    severity: 'high' | 'medium';
    agent_id: string;
    agent_name?: string | null;
    release?: Record<string, unknown> | null;
    message?: string;
}

/** One dependency edge as a sealed contract term. */
export interface ClosureDependency {
    role: DependencyRole;
    depends_on_system_id: string | null;
    agent_card_id: string | null;
    criticality: DependencyCriticality;
    contributes_to_decision: boolean;
    fallback: string | null;
    separability_rationale: string | null;
    assumptions: Record<string, unknown>;
    /** Present only when set (a conditional key, so an
     *  edge without one keeps its canonical JSON). */
    supplier_legal_entity?: string;
    /** Shared services only. */
    contract_pin?: string | null;
    /** The target's contract this edge ran against: the active one when
     *  following, the pinned one otherwise. Shared services only. */
    contract?: { id: string; version: number; hash: string } | null;
    /** Present only when set: the delegate outcomes this
     *  system was signed off as handling (sorted). */
    expected_outcomes?: string[];
}

export const COMPOSITION_POLICY_KIND_LABELS: Record<CompositionPolicyKind, string> = {
    guardrail: 'Guardrail',
    argument_policy: 'Argument policy',
    response_policy: 'Response policy',
    governance: 'Governance',
    cache: 'Cache',
};

export const COMPOSITION_LIMIT_KIND_LABELS: Record<CompositionLimitKind, string> = {
    budget: 'Budget',
    rate: 'Rate limit',
    tokens: 'Token limit',
    run_cap: 'Run cap',
    envelope: 'Operating envelope',
    concurrency: 'Concurrency',
};

// ---------------------------------------------------------------------------
// Run data scope and cross-run record reach
// ---------------------------------------------------------------------------

export type DataScopeMode = 'off' | 'audit' | 'enforce';
export type DataScopeOnUnbound = 'deny' | 'allow';
/** `declared` = the run states its records; `subject` = the delegation
 *  subject (the person the agent acts for) is the scope value. */
export type DataScopeKeySource = 'declared' | 'subject';

export interface DataScopeKey {
    key: string;
    source: DataScopeKeySource;
}

/** Tool argument `argument` of `tool` names a record of kind `key`. */
export interface DataScopeRule {
    tool: string;
    argument: string;
    key: string;
}

export interface DataScopeConfigBody {
    mode: DataScopeMode;
    on_unbound: DataScopeOnUnbound;
    keys: DataScopeKey[];
    rules: DataScopeRule[];
    unscoped_tools: string[];
}

export interface DataScopeConfig extends DataScopeConfigBody {
    ai_system_id: string;
    configured: boolean;
    version: number;
    updated_at?: string | null;
    updated_by?: string | null;
    warnings: string[];
}

export type DataScopeVerdict =
    | 'in_scope'
    | 'unbound'
    | 'conflict'
    | 'out_of_scope'
    | 'missing_argument'
    | 'unscoped_tool';

export interface DataScopeActivity {
    ai_system_id: string;
    days: number;
    rows: Array<{ tool: string; verdict: string; count: number }>;
}

export type ReachRuleSeverity = 'low' | 'medium' | 'high' | 'critical';

/** `subject` | `api_key` | `agent` | `system` | `scope:<key>`. */
export type ReachGroupBy = string;

export interface ReachRuleBody {
    scope_key: string;
    group_by: ReachGroupBy;
    window_hours: number;
    max_distinct: number;
    severity: ReachRuleSeverity;
    enabled: boolean;
}

export interface ReachRule extends ReachRuleBody {
    id: string;
    ai_system_id: string;
    created_at?: string | null;
    updated_at?: string | null;
    created_by?: string | null;
}

export interface ReachRuleList {
    rules: ReachRule[];
}

export interface ReachPreview {
    days: number;
    groups_seen: number;
    groups_over_limit: number;
    top: Array<{ group: string; max_distinct_records: number }>;
    p95?: number | null;
    p99?: number | null;
    partial: boolean;
}

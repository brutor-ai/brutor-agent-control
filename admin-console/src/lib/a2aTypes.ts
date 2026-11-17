/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import type { components } from './generated/control-plane-api';

/**
 * TypeScript types for A2A (Agent-to-Agent) admin APIs.
 *
 * Mirrors the Pydantic response models in
 * `app/api/v1/models/admin/agent_card.py`,
 * `app/api/v1/models/admin/a2a_delegation.py`, and
 * `app/api/v1/models/admin/a2a_task.py`.
 *
 * Keep these in sync with the backend — adding a field on the server side
 * without also adding it here won't break runtime but will leave the UI blind
 * to new data.
 */

// ────────────────────────────────────────────────────────────────────────────
// Agent Cards
// ────────────────────────────────────────────────────────────────────────────

export type AgentCardTrustTier =
    | 'unsigned'
    | 'tenant_signed'
    | 'partner_signed'
    | 'revoked';

/** Keep in sync with Python `HANDLER_TYPE_*`. */
export type AgentCardCapabilityHandlerType = 'skill' | 'noop';

export interface AgentCardCapabilityResponse {
    id: string;
    capability_name: string;
    description?: string | null;
    timeout_secs?: number | null;
    rate_limit_per_hour?: number | null;
    data_classification?: string[] | null;
    // Handler fields
    handler_type?: AgentCardCapabilityHandlerType | null;
    handler_ref?: string | null;
    enabled: boolean;
}

export type AgentCardHealthStatus = 'unknown' | 'healthy' | 'unhealthy';

export interface AgentCardHealthResult {
    card_id: string;
    name: string;
    status: AgentCardHealthStatus;
    error?: string | null;
    checked_at?: string | null;
    target_url?: string | null;
}

export interface AgentCardBulkHealthResponse {
    total: number;
    healthy: number;
    unhealthy: number;
    unknown: number;
    results: AgentCardHealthResult[];
}

export interface AgentCardListItem {
    id: string;
    tenant_id: string;
    name: string;
    version: string;
    description?: string | null;
    source_url?: string | null;
    /** AgentCard.iconUrl, denormalised from card_json. May be null. */
    icon_url?: string | null;
    trust_tier: AgentCardTrustTier;
    is_trusted: boolean;
    signature_algorithm?: string | null;
    signed_by?: string | null;
    signed_at?: string | null;
    revoked_at?: string | null;
    revoked_reason?: string | null;
    enabled: boolean;
    /** Liveness state populated by the Health Check button. */
    health_status?: AgentCardHealthStatus;
    last_health_check_at?: string | null;
    health_check_error?: string | null;
    created_by?: string | null;
    created_at?: string | null;
    updated_by?: string | null;
    updated_at?: string | null;
    capability_count: number;
    /** Tenant-wide default Limits / Policies (min-wins with each RG). */
    tenant_limits?: Record<string, any> | null;
    tenant_governance?: Record<string, any> | null;
    /** Resource-group bindings; populated by the server when the
     *  caller has permission to see them. */
    group_ids?: string[];
    groups?: Array<{ id: string; name: string; display_name?: string | null }>;
}

export interface AgentCardListResponse {
    cards: AgentCardListItem[];
    total: number;
}

export interface AgentCardDetailResponse extends AgentCardListItem {
    card_json: Record<string, unknown>;
    signature?: string | null;
    signature_valid?: boolean | null;
    capabilities: AgentCardCapabilityResponse[];
}

export interface AgentCardCreateRequest {
    name: string;
    version?: string;
    description?: string | null;
    source_url?: string | null;
    icon_url?: string | null;
    card_json: Record<string, unknown>;
    /** Whether the card is created enabled. Left out, the Control Plane enables it. */
    enabled?: boolean;
}

export interface AgentCardUpdateRequest {
    name?: string;
    version?: string;
    description?: string | null;
    source_url?: string | null;
    icon_url?: string | null;
    card_json?: Record<string, unknown>;
    enabled?: boolean;
    tenant_limits?: Record<string, any> | null;
    tenant_governance?: Record<string, any> | null;
}

export interface AgentCardSignRequest {
    /** Optional operator note describing why the card was signed. */
    signed_by?: string | null;
}

export interface AgentCardRevokeRequest {
    reason: string;
}

/**
 * An import of a card from its URL: the Control Plane's request type. The
 * URL may be a `.well-known` card or a base URL (both well-known paths are
 * tried); `name_override` and `description_override` replace the card's own
 * text in the tenant; `enabled: false` imports it switched off.
 */
export type AgentCardImportRequest = components['schemas']['AgentCardImportRequest'];

// ────────────────────────────────────────────────────────────────────────────
// Discover (Connect step in the Add-Card wizard) — fetches without persisting
// ────────────────────────────────────────────────────────────────────────────

export interface AgentCardDiscoverRequest {
    source_url: string;
    timeout_secs?: number;
}

export interface DiscoveredCapability {
    id?: string | null;
    name: string;
    description?: string | null;
    tags: string[];
}

export interface AgentCardDiscoverResponse {
    source_url: string;
    /** The well-known URL that actually answered. May differ from
     *  source_url when the gateway fell back to /agent.json on a
     *  legacy 0.2.x agent. */
    resolved_url: string;
    name?: string | null;
    description?: string | null;
    version?: string | null;
    icon_url?: string | null;
    protocol_version?: string | null;
    provider_name?: string | null;
    capabilities: DiscoveredCapability[];
    capability_count: number;
    /** Verbatim card body — the dialog can use this for an Advanced view
     *  and/or as the source-of-truth shape for the Create call. */
    card_json: Record<string, unknown>;
    /** True iff a card with this `name` already exists in the tenant.
     *  UI uses this to pre-flag the operator before they hit Create. */
    name_conflict: boolean;
}

// ────────────────────────────────────────────────────────────────────────────
// Bulk import — multi-select from the registry catalog
// ────────────────────────────────────────────────────────────────────────────

/** One card of a bulk import: its URL, its name in the tenant and whether it is enabled at once. */
export type AgentCardBulkImportItem = components['schemas']['AgentCardBulkImportItem'];

export type AgentCardBulkImportRequest = components['schemas']['AgentCardBulkImportRequest'];

export interface BulkImportResult {
    source_url: string;
    success: boolean;
    card_id?: string | null;
    name?: string | null;
    error?: string | null;
}

export interface AgentCardBulkImportResponse {
    results: BulkImportResult[];
    total: number;
    succeeded: number;
    failed: number;
}

// ────────────────────────────────────────────────────────────────────────────
// a2aregistry.org catalog
// ────────────────────────────────────────────────────────────────────────────

export interface CatalogAgentSkill {
    id?: string | null;
    name?: string | null;
    description?: string | null;
}

export interface CatalogAgent {
    id?: string | null;
    name?: string | null;
    description?: string | null;
    base_url?: string | null;
    well_known_url?: string | null;
    icon_url?: string | null;
    protocol_version?: string | null;
    provider_name?: string | null;
    provider_url?: string | null;
    skills: CatalogAgentSkill[];
    skill_count: number;
    healthy?: boolean | null;
    uptime_pct?: number | null;
}

export interface CatalogResponse {
    agents: CatalogAgent[];
    total: number;
    source: string;
    fetched_at: string;
    cached: boolean;
}

export interface TenantA2APublicKeyResponse {
    tenant_id: string;
    public_key: string;
    key_algorithm: string;
    created_at: string;
    rotated_at?: string | null;
}

// ────────────────────────────────────────────────────────────────────────────
// Delegation Graphs
// ────────────────────────────────────────────────────────────────────────────

export interface A2ADelegationGraphSummary {
    root_task_id: string;
    tenant_id: string;
    started_at: string;
    ended_at: string;
    duration_ms?: number | null;
    total_steps: number;
    max_depth: number;
    inbound_steps: number;
    outbound_steps: number;
    success_steps: number;
    failure_steps: number;
    overall_success: boolean;
    root_endpoint?: string | null;
    root_card_id?: string | null;
    root_card_name?: string | null;
    root_actor_agent_id?: string | null;
    root_actor_agent_name?: string | null;
    root_correlation_id?: string | null;
    /**
     * The AI Systems this graph's run touched: the root system
     * (from `ai_system_runs.root_task_id`) and one entry per run segment.
     * Absent when the graph was not attributed to any AI System.
     */
    systems?: A2ADelegationGraphSystem[] | null;
}

export interface A2ADelegationGraphSystem {
    id: string;
    name: string | null;
    role: 'root' | 'segment';
}

export interface A2ADelegationGraphListResponse {
    graphs: A2ADelegationGraphSummary[];
    total: number;
    limit: number;
    offset: number;
}

export interface A2ADelegationStep {
    current_task_id?: string | null;
    parent_task_id?: string | null;
    depth: number;
    direction?: 'inbound' | 'outbound' | null;
    proxy_log_id: string;
    timestamp: string;
    duration_ms?: number | null;
    http_status_code?: number | null;
    success: boolean;
    error_message?: string | null;
    card_id?: string | null;
    endpoint?: string | null;
    target_url?: string | null;
    correlation_id?: string | null;
    // Who → whom. Caller = the acting agent principal; callee = the target card.
    actor_agent_id?: string | null;
    actor_agent_name?: string | null;
    subject_id?: string | null;
    subject_kind?: string | null;
    card_name?: string | null;
}

export interface A2ADelegationGraphDetailResponse {
    root_task_id: string;
    tenant_id: string;
    steps: A2ADelegationStep[];
    total_steps: number;
    max_depth: number;
}

export interface A2ADelegationTopCard {
    card_id: string;
    count: number;
}

export interface A2ADelegationStats {
    tenant_id: string;
    window_start?: string | null;
    window_end?: string | null;
    total_graphs: number;
    total_steps: number;
    inbound_steps: number;
    outbound_steps: number;
    success_steps: number;
    failure_steps: number;
    success_rate?: number | null;
    avg_steps_per_graph?: number | null;
    max_depth_observed?: number | null;
    avg_duration_ms?: number | null;
    p50_duration_ms?: number | null;
    p95_duration_ms?: number | null;
    top_cards: A2ADelegationTopCard[];
}

// ────────────────────────────────────────────────────────────────────────────
// Query-parameter shapes for the list / stats endpoints.
// ────────────────────────────────────────────────────────────────────────────

export interface ListDelegationGraphsParams {
    limit?: number;
    offset?: number;
    since?: string;
    until?: string;
    /** If set, only graphs whose every step matches are returned. */
    success?: boolean;
}

export interface DelegationStatsParams {
    since?: string;
    until?: string;
    top_n_cards?: number;
}

// ────────────────────────────────────────────────────────────────────────────
// V1.0 stateful tasks (read-only admin browser)
// ────────────────────────────────────────────────────────────────────────────

/**
 * Canonical A2A v1.0 task states per the spec. Kept in sync with
 * `TaskState` in `brutor-gateway-core/src/services/a2a_v1/mod.rs`.
 */
export type A2ATaskState =
    | 'TASK_STATE_UNSPECIFIED'
    | 'TASK_STATE_SUBMITTED'
    | 'TASK_STATE_WORKING'
    | 'TASK_STATE_COMPLETED'
    | 'TASK_STATE_FAILED'
    | 'TASK_STATE_CANCELED'
    | 'TASK_STATE_INPUT_REQUIRED'
    | 'TASK_STATE_REJECTED'
    | 'TASK_STATE_AUTH_REQUIRED';

export interface A2ATaskListItem {
    id: string;
    tenant_id: string;
    resource_group_id?: string | null;
    context_id?: string | null;
    state: string;
    status_timestamp: string;
    created_at: string;
    updated_at: string;
}

export interface A2ATaskListResponse {
    tasks: A2ATaskListItem[];
    total: number;
    limit: number;
    offset: number;
}

export interface A2ATaskDetailResponse extends A2ATaskListItem {
    task_json: Record<string, unknown>;
}

export interface ListA2ATasksParams {
    state?: string;
    context_id?: string;
    resource_group_id?: string;
    since?: string;
    until?: string;
    limit?: number;
    offset?: number;
}

export interface A2APushConfigListItem {
    id: string;
    tenant_id: string;
    task_id: string;
    config_json: Record<string, unknown>;
    created_at: string;
}

export interface A2APushConfigListResponse {
    configs: A2APushConfigListItem[];
    total: number;
}

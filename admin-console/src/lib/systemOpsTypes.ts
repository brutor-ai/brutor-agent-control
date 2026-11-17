/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * TypeScript types for the System Operations Console REST API. One file per phase isn't worth it — every system
 * sub-section uses the same `/v1/admin/system/...` namespace and
 * the same audit / NOTIFY shapes, so they all land here.
 *
 * Source-of-truth is `app/api/v1/models/admin/system_ops.py` on the
 * backend. When you add a field there, mirror it here AND update
 * the `SystemConsoleTab` placeholders to consume it.
 */

// ─── Shared enums ────────────────────────────────────────────────

export type HealthStatus =
    | 'healthy'
    | 'degraded'
    | 'down'
    | 'unknown'
    | 'disabled';

export type DependencyKind =
    | 'database'
    | 'redis'
    | 'qdrant'
    | 'llm_upstream'
    | 'mcp_server'
    | 'gateway_core'
    | 'skill_runner'
    | 'kb_connector_sync'
    | 'registry';

export type MaintenanceMode = 'off' | 'read_only' | 'drain' | 'paused';

// ─── Overview ────────────────────────────────────

export interface HealthCard {
    kind: DependencyKind;
    id: string;
    label: string;
    status: HealthStatus;
    detail: string | null;
    latency_ms: number | null;
    target_route: string | null;
    last_checked_at: string | null;
}

export interface OverviewCounters {
    healthy: number;
    degraded: number;
    down: number;
    unknown: number;
    disabled: number;
}

export interface OverviewMaintenance {
    mode: MaintenanceMode;
    since: string | null;
    drain_grace_secs: number | null;
}

export interface SystemOverviewResponse {
    tenant_id: string;
    generated_at: string;
    counters: OverviewCounters;
    maintenance: OverviewMaintenance;
    cards: HealthCard[];
}


// ─── counters-rust-consumer-and-ui ───
// ─── Counters ────────────────────────────────────
//
// Mirror of `app/api/v1/models/admin/system_ops.py` CounterKind /
// CounterEntry / CounterListResponse / CounterResetRequest /
// CounterReleaseCooldownRequest. The control plane reads usage from
// usage_logs over the last 60s; the proxy is the source of truth for
// the cooldown state — surface that distinction in the UI copy.

export type CounterKind =
    | 'llm_request_rate'
    | 'llm_input_tokens'
    | 'llm_output_tokens'
    | 'llm_concurrent'
    | 'llm_spend'
    | 'mcp_tool_rate'
    | 'mcp_session_rate';

// Stable wire enum shared with the backend Pydantic CounterScope.
// Adding a value is a backward-compatible change; the values listed
// here are the surface area of deep-link URLs (?cnt-scope=...).
// Keep in sync with `app/api/v1/models/admin/system_ops.py:CounterScope`.
export type CounterScope =
    | 'tenant'
    | 'group'
    | 'user'
    | 'api_key'
    | 'model'
    | 'mcp_server';

export interface CounterEntry {
    tenant_id: string;
    group_id: string | null;
    subject_id: string | null;
    subject_label: string | null;
    kind: CounterKind;
    window: string;
    used: number;
    limit: number;
    cooldown_active: boolean;
    cooldown_until: string | null;
    last_updated_at: string | null;
}

export interface CounterListResponse {
    tenant_id: string;
    generated_at: string;
    counters: CounterEntry[];
}

export interface CounterFilter {
    group_id?: string;
    kind?: CounterKind;
    subject_id?: string;
    /** Suppress row families whose subject type doesn't match.
     * Set when arriving from a deep link (PagerDuty / Proxy Logs row)
     * that pinned the subject type — the empty state then correctly
     * reads "no rows for scope=X" rather than falling back to every
     * tenant counter. */
    scope?: CounterScope;
    only_cooling_down?: boolean;
}

export interface CounterResetRequest {
    scope: CounterScope;
    id: string;
    limit_types?: string[] | null;
    reason?: string | null;
}

export interface CounterReleaseCooldownRequest {
    scope: CounterScope;
    id: string;
    reason?: string | null;
}


// ─── caches ───
// ─── Caches ──────────────────────────────
//
// Mirrors `app/api/v1/models/admin/system_ops_caches.py` plus the
// pre-existing CacheLayer / CacheScope / CacheFlushRequest enums in
// `app/api/v1/models/admin/system_ops.py`. Keep this block in sync
// with both Python files — the channel registry on the server side
// will reject unknown layer / scope values, so a drift here surfaces
// as a runtime 4xx, not a silent type mismatch.

export type CacheLayer =
    | 'semantic'
    | 'policy'
    | 'group'
    | 'tool'
    | 'response';

export type CacheScope =
    | 'tenant'
    | 'group'
    | 'model'
    | 'mcp_server'
    | 'config';

export interface CacheLayerStats {
    layer: CacheLayer;
    hit_count: number;
    miss_count: number;
    /** Fraction in [0, 1]. Render as percent in the UI. */
    hit_rate: number;
    /** NULL when the control plane can't see the in-process moka store. */
    item_count_approx: number | null;
    last_flushed_at: string | null;
    scopes_available: CacheScope[];
    /** Operator-facing limitation hint when counters are zero by source. */
    detail: string | null;
}

export interface CacheStatsResponse {
    tenant_id: string;
    generated_at: string;
    layers: CacheLayerStats[];
}

export interface CacheFlushRequest {
    layer: CacheLayer;
    scope: CacheScope;
    id: string;
    /** Audited — surfaced in the Audit tab payload column. */
    reason: string | null;
}


// ─── audit ───
// ─── Audit ───────────────────────────────────────
//
// Wire types for the System Audit tab. Source-of-truth on the
// backend is `app/api/v1/models/admin/system_ops.py`
// (`SystemOperationLogResponse`, `SystemOperationLogListResponse`)
// plus `app/api/v1/models/admin/system_ops_audit.py`
// (`AuditFacetsResponse`).

export type ActorType = 'system_admin' | 'tenant_admin';

export interface SystemAuditEntry {
    id: string;
    tenant_id: string | null;
    actor_admin_id: string;
    actor_type: ActorType;
    action: string;
    scope_kind: string | null;
    scope_id: string | null;
    payload: Record<string, any> | null;
    before_state: Record<string, any> | null;
    after_state: Record<string, any> | null;
    ip: string | null;
    user_agent: string | null;
    created_at: string | null;
}

/**
 * Filter envelope shared by /v1/admin/system/audit,
 * /audit/facets, /audit/export. Multi-value arrays are
 * OR-within-field; AND-across-fields. `start`/`end` are ISO-8601
 * timestamps; `q` is a free-text substring search.
 */
export interface SystemAuditFilter {
    actions?: string[];
    scope_kinds?: string[];
    actor_admin_ids?: string[];
    actor_types?: ActorType[];
    start?: string;
    end?: string;
    q?: string;
}

export interface SystemAuditListResponse {
    items: SystemAuditEntry[];
    total: number;
}

export interface SystemAuditFacetValue {
    value: string | number | boolean | null;
    count: number;
}

export interface SystemAuditFacetsResponse {
    facets: Record<string, SystemAuditFacetValue[]>;
    labels: Record<string, string>;
}


// ─── connections ───
// ─── Connections ─────────────────────────────────
//
// Mirror of `app/api/v1/models/admin/system_ops_connections.py`. The
// pool entry is deliberately wide so the same row shape works for
// Postgres (size/checked_out filled), Redis/Qdrant (size null, client_count
// filled), and LLM provider entries (everything null, detail explains).

export type RecyclablePoolKind = 'postgres' | 'redis';

export interface ConnectionPool {
    /**
     * Pool identifier. 'postgres', 'redis', 'qdrant', or
     * 'llm_provider:<provider_id>' for LLM upstream entries. The
     * UI dispatches recycle buttons on the literal 'postgres' /
     * 'redis' values.
     */
    kind: string;
    label: string;

    // Pool-size telemetry — Postgres fills these, others null.
    size: number | null;
    checked_in: number | null;
    checked_out: number | null;
    overflow: number | null;

    /**
     * Fraction of pool currently in use (checked_out / size).
     * Values >1 mean the pool is overflowing; null when size is null
     * or zero (Redis / Qdrant entries).
     */
    saturation: number | null;

    /** Open clients right now — used by per-call pool kinds. */
    client_count: number | null;

    recyclable: boolean;

    /** Most-recent observed round-trip in ms — Qdrant, when available. */
    most_recent_latency_ms: number | null;

    /** One-line operator-facing status — always populated. */
    detail: string;
    observed_at: string;
}

export interface ConnectionListResponse {
    tenant_id: string | null;
    generated_at: string;
    pools: ConnectionPool[];
}

export interface PoolRecycleRequest {
    kind: RecyclablePoolKind;
    reason?: string | null;
}

export interface PoolRecycleResponse {
    kind: RecyclablePoolKind;
    /** 'recycled' | 'noop' | 'error' */
    status: string;
    detail: string;
}


// ─── tasks ───
// ─── Background Tasks ─────────────────────────────
//
// Mirrors `app/api/v1/models/admin/system_ops_tasks.py` on the
// backend. SchedulerActionRequest is the shared body for the three
// action endpoints — the verb rides the URL path.

export type SchedulerState = 'running' | 'paused' | 'unknown';

export interface SchedulerStatus {
    scheduler_name: string;
    paused: boolean;
    paused_at: string | null;
    paused_by: string | null;
    resumed_at: string | null;
    resumed_by: string | null;
    last_run_at: string | null;
    last_run_result: string | null;
    next_run_eta: string | null;
    state: SchedulerState;
    /** Why the scheduler is paused, as the pause gave it; null when it is not paused or was paused without one. */
    pause_reason?: string | null;
}

export interface SchedulerListResponse {
    items: SchedulerStatus[];
}

/**
 * The body of the three actions (`POST /v1/admin/system/tasks/trigger`,
 * `/pause`, `/resume`). The routes have no path parameter: the scheduler is
 * named here, as the list names it, and the Control Plane's handler reads
 * it from the body.
 */
export interface SchedulerActionRequest {
    scheduler_name: string;
    reason?: string;
}


// ─── sessions ───

// ─── Sessions ────────────────────────────────────
//
// One envelope per credential kind — the table renders the same shape
// regardless of tab. `extra` is a free-form bag for per-kind columns
// (admin: risk_score; api_key: scope + usage_count).

export type SessionKind = 'admin' | 'portal' | 'api_key';

export interface SessionEntry {
    kind: SessionKind;
    id: string;
    tenant_id: string;
    user_id: string | null;
    user_label: string | null;
    created_at: string | null;
    last_seen_at: string | null;
    expires_at: string | null;
    ip: string | null;
    user_agent: string | null;
    extra: Record<string, unknown>;
}

export interface SessionListResponse {
    tenant_id: string;
    kind: SessionKind;
    generated_at: string;
    items: SessionEntry[];
}

export interface SessionRevokeRequest {
    kind: SessionKind;
    id: string;
    reason?: string;
}

export interface SessionRevokeResponse {
    status: 'queued';
    kind: SessionKind;
    id: string;
    tenant_id: string;
    channel: string;
    payload: string;
}


// ─── logs ───
// ─── Logs ───────────────────────────────────
//
// LogLevel is the canonical slider rung. The runtime accepts a few
// aliases (warning/critical/fatal) but the wire shape between admin
// UI and gateway is this 5-rung enum only. `trace` is accepted for
// parity with the Rust runtime; on Python it collapses to debug.

export type LogLevel = 'trace' | 'debug' | 'info' | 'warn' | 'error';

export interface LogLevelEntry {
    /** Python logger name (matches the BrutorLogger constructor arg). */
    module: string;
    /** Live effective level — what the next log call dispatches against. */
    level: LogLevel;
    /** Numeric effective level for clients that want exact comparisons. */
    effective_level_int: number;
    /** True when the module appears in the curated slider list. */
    is_curated: boolean;
    /** Persisted override level. NULL when no override row exists. */
    override_level: LogLevel | null;
    /** ISO-8601 timestamp the override was last written. */
    override_at: string | null;
    /** Admin id that applied the override. Currently always NULL. */
    override_by: string | null;
}

export interface LogLevelListResponse {
    items: LogLevelEntry[];
}

export interface SetLogLevelRequest {
    module: string;
    level: LogLevel;
    /** 'all' fans out via NOTIFY; 'self' mutates only the receiving replica. */
    scope?: 'all' | 'self';
    reason?: string;
}

export interface SetLogLevelResponse {
    module: string;
    level: LogLevel;
    effective_level_int: number;
    /** ISO-8601 timestamp the mutation landed. */
    applied_at: string;
    /** NOTIFY channel the change was published on. */
    fanout_channel: string;
}


// ─── maintenance ───
// ─── Maintenance ────────────────────────────

export interface MaintenanceStateResponse {
    tenant_id: string;
    mode: MaintenanceMode;
    maintenance_mode_at: string | null;
    maintenance_mode_by: string | null;
    drain_grace_secs: number | null;
}

export interface MaintenancePatchRequest {
    tenant_id?: string;
    mode: MaintenanceMode;
    drain_grace_secs?: number;
    reason?: string;
}

export interface RestartRollingRequest {
    tenant_id?: string;
    reason?: string;
}

export interface RestartRollingResponse {
    audit_id: string;
    recorded_at: string;
}

export interface DbVacuumRequest {
    table: string;
    tenant_id?: string;
    reason?: string;
}

export interface DbVacuumResponse {
    table: string;
    duration_ms: number;
    audit_id: string;
    completed_at: string;
}


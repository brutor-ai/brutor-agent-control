# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Pydantic models for the System Operations Console REST endpoints.

Three model families live here:

  1. **SystemOperationLog** — list / filter / response shapes for the
     Audit tab. The endpoints write rows from inside the audit-row
     helper, never from a client-supplied request body, so there's no
     `Create` model.

  2. **SchedulerRuntimeState** — read shape (per-scheduler status) and
     the small action bodies for pause / resume / trigger.

  3. **Maintenance mode** — patch body + response shape for the
     PATCH /v1/admin/system/maintenance endpoint.

Filter envelopes follow the proxy-logs faceted-search pattern:
multi-value arrays for OR-within-field, single string for free text.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Wire-format enums
# ---------------------------------------------------------------------------
# Kept Python-side as Literal types so the OpenAPI schema enumerates
# them explicitly. Drift between these and the model-level constants
# is caught by the regression test in test_system_ops_models.py.

# "system" is the platform acting on its own — the drift-response closed
# loop writes audit rows as `system:drift-response`. Pinned against the ORM
# by test_actor_type_literal_matches_orm.
ActorType = Literal["system_admin", "tenant_admin", "system"]

MaintenanceMode = Literal["off", "read_only", "drain", "paused"]


# ---------------------------------------------------------------------------
# system_operation_log — list / detail
# ---------------------------------------------------------------------------


class SystemOperationLogResponse(BaseModel):
    """One audit row, surfaced to the Audit tab."""

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Tenant this action targeted. NULL = global / cross-tenant "
            "action (only system admins emit these)."
        ),
    )
    actor_admin_id: str
    actor_type: ActorType
    action: str = Field(
        description=(
            "Action verb. See SystemOperationLog.ACTION_* constants for "
            "the canonical set; new actions are allowed without a "
            "schema change."
        ),
    )
    scope_kind: Optional[str] = None
    scope_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Sanitised request body — secrets stripped at write time.",
    )
    before_state: Optional[Dict[str, Any]] = None
    after_state: Optional[Dict[str, Any]] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: Optional[str] = None


class SystemOperationLogListRequest(BaseModel):
    """Query envelope for GET /v1/admin/system/audit.

    All filter fields are multi-value (OR-within-field, AND across
    fields) — same as the proxy-logs / admin-audit-logs viewer
    envelope so the FacetSidebar component can reuse its plural-key
    sync helper unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    # Multi-value facet filters. Empty list = no constraint.
    tenant_ids: List[str] = Field(default_factory=list)
    actor_admin_ids: List[str] = Field(default_factory=list)
    actor_types: List[ActorType] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)
    scope_kinds: List[str] = Field(default_factory=list)

    # Time range (ISO-8601 strings; UTC assumed when the offset is absent).
    start: Optional[str] = Field(
        default=None,
        description="Inclusive lower bound on created_at.",
    )
    end: Optional[str] = Field(
        default=None,
        description="Exclusive upper bound on created_at.",
    )

    # Free text against the JSONB payload (GIN-indexed).
    q: Optional[str] = Field(
        default=None,
        description="Free-text search against the payload JSONB tree.",
    )

    # Pagination — same shape as the rest of the admin API.
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class SystemOperationLogListResponse(BaseModel):
    """List wrapper with the audit rows + a total count for paging."""

    model_config = ConfigDict(extra="forbid")

    items: List[SystemOperationLogResponse]
    total: int = Field(
        description=(
            "Total rows matching the filter, regardless of limit/offset. "
            "Used by the Audit tab footer's page counter."
        ),
    )


# ---------------------------------------------------------------------------
# scheduler_runtime_state — read + control
# ---------------------------------------------------------------------------


class SchedulerStatusResponse(BaseModel):
    """One row in the Background Tasks page table.

    The status fields are derived at read time by the endpoint — they
    aren't persisted columns. `last_run_at` / `last_run_result` come
    from the scheduler's own log channel; `next_run_eta` is computed
    from the scheduler's interval. The PERSISTED state is just the
    pause/resume pair.
    """

    model_config = ConfigDict(extra="forbid")

    scheduler_name: str
    paused: bool
    paused_at: Optional[str] = None
    paused_by: Optional[str] = None
    resumed_at: Optional[str] = None
    resumed_by: Optional[str] = None

    # Derived status fields.
    last_run_at: Optional[str] = Field(
        default=None,
        description="Most recent tick start (from the scheduler's status report).",
    )
    last_run_result: Optional[str] = Field(
        default=None,
        description="'success', 'error: <message>', or null if never run.",
    )
    next_run_eta: Optional[str] = Field(
        default=None,
        description="Approximate next-tick timestamp, computed from interval.",
    )


class SchedulerListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[SchedulerStatusResponse]


class SchedulerActionRequest(BaseModel):
    """Body of `POST /v1/admin/system/tasks/pause`, `/tasks/resume` and
    `/tasks/trigger`: which scheduler, and optionally why."""

    model_config = ConfigDict(extra="forbid")

    scheduler_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "The scheduler to act on: a `scheduler_name` of "
            "`GET /v1/admin/system/tasks`. An unknown name is answered 404."
        ),
    )

    reason: Optional[str] = Field(
        default=None,
        max_length=4096,
        description=(
            "Why the scheduler is paused, resumed or triggered. Recorded in the "
            "system operation log; the reason of a pause is also shown as the "
            "scheduler's `pause_reason` while it is paused."
        ),
    )


# ---------------------------------------------------------------------------
# Maintenance mode
# ---------------------------------------------------------------------------


class MaintenanceStateResponse(BaseModel):
    """Current per-tenant maintenance state."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    mode: MaintenanceMode
    maintenance_mode_at: Optional[str] = None
    maintenance_mode_by: Optional[str] = None
    drain_grace_secs: Optional[int] = Field(
        default=None,
        ge=0,
        le=3600,
        description="Per-tenant override (seconds). NULL = use global default.",
    )


class MaintenancePatchRequest(BaseModel):
    """Body for PATCH /v1/admin/system/maintenance.

    Tenant id rides the auth context for tenant-admin callers; sysadmin
    callers may set it explicitly to act on a tenant they have access
    to. The endpoint enforces the rule.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Target tenant. NULL = use the caller's tenant context. "
            "Sysadmin only when set explicitly."
        ),
    )
    mode: MaintenanceMode
    drain_grace_secs: Optional[int] = Field(
        default=None,
        ge=0,
        le=3600,
        description=(
            "Override the global drain grace for this tenant. Only "
            "meaningful when mode = 'drain'."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=4096,
        description=(
            "Operator-supplied reason. Persisted into the "
            "system_operation_log payload so the Audit tab shows it."
        ),
    )


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


# Counter subject types. Matches the buckets the Rust proxy enforces.
# `mcp_server` was added when the MCP per-server rate-limit work
# (#801..#807) landed — the proxy already counts against it; this enum
# closes the gap so the deep-link contract can address MCP-server
# scoped counters from a Proxy Logs row or PagerDuty alert.
CounterScope = Literal[
    "tenant",
    "group",
    "user",
    "api_key",
    "model",
    "mcp_server",
]


class CounterResetRequest(BaseModel):
    """Body for POST /v1/admin/system/counters/reset."""

    model_config = ConfigDict(extra="forbid")

    scope: CounterScope = Field(
        description=(
            "What the counter is keyed on. The endpoint forwards this "
            "verbatim to the gateway-core NOTIFY consumer."
        ),
    )
    id: str = Field(
        min_length=1,
        max_length=255,
        description=(
            "Identifier of the scoped subject. Shape depends on scope: "
            "group_id for 'group', user_id for 'user', api_key id for "
            "'api_key', etc."
        ),
    )
    limit_types: Optional[List[str]] = Field(
        default=None,
        description=(
            "Subset of limit buckets to reset (e.g. ['rpm', 'itpm']). "
            "NULL = reset every bucket for the scope. Open enum — the "
            "Rust runtime decides what's valid."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=4096,
    )


class CounterReleaseCooldownRequest(BaseModel):
    """Body for POST /v1/admin/system/counters/release-cooldown."""

    model_config = ConfigDict(extra="forbid")

    scope: CounterScope
    id: str = Field(min_length=1, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=4096)


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


CacheLayer = Literal[
    "semantic",
    "policy",
    "group",
    "tool",
    "response",
]

CacheScope = Literal["tenant", "group", "model", "mcp_server", "config"]


class CacheFlushRequest(BaseModel):
    """Body for POST /v1/admin/system/caches/flush."""

    model_config = ConfigDict(extra="forbid")

    layer: CacheLayer = Field(
        description=(
            "Which cache to flush. Each layer's NOTIFY consumer in "
            "gateway-core handles its own dispatch."
        ),
    )
    scope: CacheScope = Field(
        description=(
            "What scope to flush. Shape pairs with layer — "
            "semantic supports tenant/group/model, policy and group "
            "support tenant only, tool supports mcp_server, response "
            "supports config."
        ),
    )
    id: str = Field(
        min_length=1,
        max_length=255,
        description="Identifier of the scoped subject.",
    )
    reason: Optional[str] = Field(default=None, max_length=4096)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
# The Overview tab is the at-a-glance health grid. The endpoint
# returns a list of `HealthCard` entries (one per dependency Brutor
# relies on at runtime) plus aggregate counters and the current
# maintenance state. The frontend renders one card per entry; status
# colour comes from `status`, the per-card "open details" deep-link
# from `target_route`.

HealthStatus = Literal["healthy", "degraded", "down", "unknown", "disabled"]
DependencyKind = Literal[
    "database",
    "redis",
    "qdrant",
    "llm_upstream",
    "mcp_server",
    "gateway_core",
    "skill_runner",
    "kb_connector_sync",
    "registry",
]


class HealthCard(BaseModel):
    """One row in the Overview health grid.

    Every dependency Brutor talks to at runtime gets a card. Card
    `status` is the result of a cheap probe — TCP ping, vendor `/v1/models`
    list, MCP `tools/list` — capped at 2s per card. The probes run in
    parallel inside the endpoint.

    `target_route` lets the UI deep-link into the right sub-section
    (e.g. an unhealthy LLM upstream links to LLM Configuration; a
    saturated Postgres pool links to System → Connections).
    """

    model_config = ConfigDict(extra="forbid")

    kind: DependencyKind
    id: str = Field(
        description=(
            "Stable identifier for the dependency. For DB / Redis / Qdrant "
            "this is the literal kind. For LLM upstreams it's the model "
            "id; for MCP servers, the server id."
        ),
    )
    label: str = Field(
        description="Human-readable card title shown in the UI.",
    )
    status: HealthStatus
    detail: Optional[str] = Field(
        default=None,
        description=(
            "Short status reason — e.g. 'connection refused', '12/20 "
            "pool slots free', 'last probe 4m ago'. Surface verbatim "
            "in the card body."
        ),
    )
    latency_ms: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Round-trip latency of the probe in milliseconds. NULL "
            "if the probe didn't complete (status=down/unknown)."
        ),
    )
    target_route: Optional[str] = Field(
        default=None,
        description=(
            "Admin-UI tab id the card deep-links to. Frontend opens "
            "that tab on click. NULL means no drill-down."
        ),
    )
    last_checked_at: Optional[str] = Field(
        default=None,
        description="ISO-8601 timestamp of the probe.",
    )


class OverviewMaintenance(BaseModel):
    """Maintenance state surface on the Overview tab.

    Duplicates a subset of `MaintenanceStateResponse` so the Overview
    page doesn't need a second round-trip just to colour the banner.
    """

    model_config = ConfigDict(extra="forbid")

    mode: MaintenanceMode
    since: Optional[str] = None
    drain_grace_secs: Optional[int] = None


class OverviewCounters(BaseModel):
    """Aggregate counts surfaced as a single header strip."""

    model_config = ConfigDict(extra="forbid")

    healthy: int = Field(ge=0)
    degraded: int = Field(ge=0)
    down: int = Field(ge=0)
    unknown: int = Field(ge=0)
    disabled: int = Field(ge=0)


class OverviewResponse(BaseModel):
    """GET /v1/admin/system/overview body."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(
        description=(
            "Tenant the cards were scoped to. Always equals the "
            "authenticated admin's tenant unless a system admin "
            "specified ?tenant_id="
        ),
    )
    generated_at: str = Field(
        description="ISO-8601 timestamp of the response.",
    )
    counters: OverviewCounters
    maintenance: OverviewMaintenance
    cards: List[HealthCard] = Field(
        description="One card per dependency, ordered by `kind` then `id`.",
    )


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
# A counter is one bucket — a rate limit's current state for a given
# (tenant, scope, subject, limit kind) combination. The proxy is the
# canonical owner; the control plane reads recent state from the
# `usage_logs` table for display, and writes RESET / RELEASE actions
# via NOTIFY so the proxy clears its in-memory state.

CounterKind = Literal[
    "llm_request_rate",
    "llm_input_tokens",
    "llm_output_tokens",
    "llm_concurrent",
    "llm_spend",
    "mcp_tool_rate",
    "mcp_session_rate",
]


class CounterEntry(BaseModel):
    """One row in the Counters table."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    group_id: Optional[str] = Field(
        default=None,
        description="Resource group scope, or NULL for tenant-wide.",
    )
    subject_id: Optional[str] = Field(
        default=None,
        description=(
            "What the bucket is keyed against — model id for LLM "
            "counters, MCP server id (and optionally tool name) "
            "for MCP counters."
        ),
    )
    subject_label: Optional[str] = Field(
        default=None,
        description="Human-readable label, e.g. 'gpt-5.2' or 'jira (issue.create)'.",
    )
    kind: CounterKind
    window: str = Field(
        description=(
            "Window descriptor of the rate limit, e.g. '60s', '1d'. "
            "Free-text — defined by the rate-limit config."
        ),
    )
    used: int = Field(ge=0, description="Current usage in the window.")
    limit: int = Field(ge=0, description="Configured limit.")
    cooldown_active: bool = Field(
        description=(
            "True if the bucket entered cooldown — proxy is rejecting "
            "requests until the cooldown clears."
        ),
    )
    cooldown_until: Optional[str] = Field(
        default=None,
        description="ISO-8601 timestamp the cooldown expires.",
    )
    last_updated_at: Optional[str] = None


class CounterListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    generated_at: str
    counters: List[CounterEntry]


class CounterFilterQuery(BaseModel):
    """Optional query params for the GET endpoint."""

    model_config = ConfigDict(extra="forbid")

    group_id: Optional[str] = None
    kind: Optional[CounterKind] = None
    subject_id: Optional[str] = None
    only_cooling_down: bool = Field(
        default=False,
        description="If true, return only buckets currently in cooldown.",
    )

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Router Pydantic Models for API Requests/Responses
For router status, routing groups, and testing
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from enum import Enum
from app.api.v1.models.admin.resource_fields import ResourceVersionField
from app.api.v1.models.admin.resource_fields import ResourceMetaFields
from app.services import release_scope

# Fields of a routing group the gateway does not read: alerting, the
# pre-call capability check, and the router's own retry and blocking
# tuning. Not available in this release: a value is refused (`False` is
# accepted for `alerting_enabled`), and no response shows them.
ROUTING_GROUP_UNAVAILABLE_FIELDS = (
    "alerting_enabled",
    "alerting_threshold",
    "alerting_webhook_url",
    "alerting_config",
    "required_capabilities",
    "routing_max_retries",
    "routing_retry_delay",
    "ttl_for_blocked_models",
    "router_settings",
)
_UNAVAILABLE = "Not available in this release: a value is refused (the gateway does not read it)."


def _refuse_unavailable_routing_fields(model):
    for field in ROUTING_GROUP_UNAVAILABLE_FIELDS:
        value = getattr(model, field, None)
        if value is None or (field == "alerting_enabled" and value is False):
            continue
        raise ValueError(
            release_scope.not_available(f"Routing group field {field}", why="the gateway does not read it")
        )
    return model


# =============================================================================
# Enums for Router Configuration
# =============================================================================

class RoutingStrategy(str, Enum):
    """Supported routing strategies"""
    SIMPLE_SHUFFLE = "simple-shuffle"
    LEAST_BUSY = "least-busy"
    USAGE_BASED = "usage-based-routing"
    USAGE_BASED_V2 = "usage-based-routing-v2"
    LATENCY_BASED = "latency-based-routing"
    COST_BASED = "cost-based-routing"
    COMPLEXITY_BASED = "complexity-based-routing"


class RoutingGroupMode(str, Enum):
    """Inbound surface a routing group serves. Mirrors
    `LLModelMode` minus the synthetic 'routing' value (which is
    reserved for the virtual model the control plane creates per
    group; admins don't address it directly).

    Members of a routing group must share this mode — enforced
    at member-assign time in `update_llm` / `create_llm`.
    """
    CHAT = "chat"
    COMPLETION = "completion"
    RESPONSES = "responses"
    EMBEDDING = "embedding"
    IMAGE_GENERATION = "image_generation"
    AUDIO_SPEECH = "audio_speech"
    AUDIO_TRANSCRIPTION = "audio_transcription"
    VIDEO_GENERATION = "video_generation"
    RERANK = "rerank"
    MODERATION = "moderation"


class HealthStatus(str, Enum):
    """Model health status — must stay in sync with the values
    actually written by the LLM-models manager + Rust proxy
    health probes. Missing a real status value here causes a
    `fastapi.exceptions.ResponseValidationError` 500 when an
    admin endpoint tries to serialize a model row whose
    `health_status` column carries that value (the user's
    "Failed to load deployments" with `degraded` was exactly
    this pattern).

    Source of truth in the LLM-models manager —
    `app/services/llm/model/llm_models_manager.py`:
      * `healthy`         last probe passed
      * `unhealthy`       last probe failed
      * `degraded`        partial failure / intermittent — set by
                          the probe path when retries succeed but
                          the failure count is above zero
      * `requires_tier`   tier-restricted model (Codex / Pro / o1-
                          pro) that the configured key isn't
                          eligible for; the badge is grey, not red
      * `unknown`         never probed (or older row pre-dating
                          the probe-type column)
    """
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    REQUIRES_TIER = "requires_tier"
    UNKNOWN = "unknown"


# =============================================================================
# Routing Group CRUD Models
# =============================================================================

class ComplexityTier(str, Enum):
    """Complexity tiers, cheapest -> most capable.

    WIRE CONTRACT: these strings are shared verbatim with the Rust scorer
    (`services/complexity.rs::ComplexityTier::as_str`) and stored in
    `llm_routing_group_tier.tier`.
    """
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"
    REASONING = "reasoning"


class RoutingGroupTierAssignment(BaseModel):
    """One tier -> model mapping."""
    tier: ComplexityTier = Field(..., description="Complexity tier")
    llm_model_id: str = Field(..., description="Model to serve this tier")
    position: int = Field(
        default=0, ge=0,
        description="Order within the tier; lower is preferred. Several models "
                    "may share a tier, in which case the group's ordinary "
                    "balancing picks between them.",
    )


class RoutingGroupTiersUpdate(BaseModel):
    """PUT-replace the whole tier map for a group."""

    model_config = ConfigDict(extra="forbid")
    tiers: List[RoutingGroupTierAssignment] = Field(
        default_factory=list,
        description="Full replacement set. An empty list clears the mapping, "
                    "which makes complexity routing inert for this group.",
    )


class RoutingGroupTierItem(BaseModel):
    id: str
    routing_group_id: str
    tier: str
    llm_model_id: str
    position: int


class RoutingGroupTiersResponse(BaseModel):
    routing_group_id: str
    tiers: List[RoutingGroupTierItem]
    total: int


# The ceiling of a routing group's `stream_timeout`: the ceiling of a model's
# whole-request timeout, which bounds the same call.
STREAM_TIMEOUT_MAX_SECONDS = 1800.0


class RoutingGroupCreate(ResourceVersionField):
    """
    Request model for creating a new routing group
    POST /v1/admin/llm-router/routing-groups
    """
    id: str = Field(..., min_length=1, max_length=100, description="Unique routing group ID")
    name: str = Field(..., min_length=1, max_length=200, description="Display name")
    description: Optional[str] = Field(None, description="Description of routing group purpose")

    # Mode — declared at creation time, immutable once members
    # are assigned. Members must share this mode.
    mode: RoutingGroupMode = Field(
        ...,
        description=(
            "Inbound surface this group serves (chat / responses / "
            "embedding / image_generation / …). Members must share "
            "this mode; the model-picker on the LLM Models page "
            "filters its routing-group dropdown by it. Cannot be "
            "changed once any model has been assigned to the group "
            "— delete or move the members first."
        ),
    )

    # Routing Strategy
    routing_strategy: RoutingStrategy = Field(
        default=RoutingStrategy.USAGE_BASED_V2,
        description="Routing strategy for load balancing"
    )

    # Complexity-based routing (only read when routing_strategy is
    # complexity-based-routing; harmless otherwise)
    complexity_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional scorer overrides (weights, tier boundaries, keyword "
                    "lists). Omit for the shipped defaults.",
    )
    complexity_floor_tier: Optional[ComplexityTier] = Field(
        default=None,
        description="Never route below this tier, whatever the prompt scores.",
    )

    # Failure Handling
    cooldown_time: int = Field(default=60, ge=0, description="Cooldown time in seconds")

    # Fallback Configuration
    fallback_models: Optional[List[str]] = Field(None, description="List of fallback routing group IDs")
    context_window_fallbacks: Optional[List[str]] = Field(None, description="Fallback routing group IDs for context window exceeded")
    content_policy_fallbacks: Optional[List[str]] = Field(None, description="Fallback routing group IDs for content policy violations")
    default_fallbacks: Optional[List[str]] = Field(None, description="Default fallback routing group IDs")
    max_fallbacks: Optional[int] = Field(None, ge=1, le=20, description="Maximum fallback attempts")

    # Not available in this release (ROUTING_GROUP_UNAVAILABLE_FIELDS)
    required_capabilities: Optional[List[str]] = Field(None, description=_UNAVAILABLE)

    # Retry Configuration
    num_retries: int = Field(default=3, ge=0, le=10, description="Number of retries")
    retry_after: int = Field(default=5, ge=0, description="Seconds to wait before retrying")
    stream_timeout: Optional[float] = Field(
        None, gt=0, le=STREAM_TIMEOUT_MAX_SECONDS,
        description=(
            "Seconds. The longest a streamed call waits for the first chunk of "
            "the member it was routed to; a member that stays silent past it "
            "is given up on and the call fails over to the next. Greater than "
            "0 and at most 1800. Not set: only the member's own request "
            "timeout applies."
        ),
    )

    # Not available in this release (ROUTING_GROUP_UNAVAILABLE_FIELDS)
    alerting_enabled: Optional[bool] = Field(None, description=_UNAVAILABLE + " `false` is accepted.")
    alerting_threshold: Optional[int] = Field(None, description=_UNAVAILABLE)
    alerting_webhook_url: Optional[str] = Field(None, description=_UNAVAILABLE)
    alerting_config: Optional[Dict[str, Any]] = Field(None, description=_UNAVAILABLE)
    routing_max_retries: Optional[int] = Field(None, description=_UNAVAILABLE)
    routing_retry_delay: Optional[float] = Field(None, description=_UNAVAILABLE)
    ttl_for_blocked_models: Optional[int] = Field(None, description=_UNAVAILABLE)

    # Rate Limiting
    max_parallel_requests: Optional[int] = Field(None, ge=1, description="Max parallel requests")
    rpm_limit: Optional[int] = Field(None, ge=1, description="Requests per minute limit")
    tpm_limit: Optional[int] = Field(None, ge=1, description="Tokens per minute limit")

    router_settings: Optional[Dict[str, Any]] = Field(None, description=_UNAVAILABLE)

    # Status
    enabled: bool = Field(default=True, description="Whether routing group is enabled")
    custom_metadata: Optional[Dict[str, Any]] = Field(None, description="Custom metadata")

    @model_validator(mode="after")
    def _in_this_release(self):
        return _refuse_unavailable_routing_fields(self)


class RoutingGroupUpdate(ResourceVersionField):
    """
    Request model for updating a routing group
    PUT /v1/admin/llm-router/routing-groups/{group_id}
    """

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    # Mode change is rejected by the service layer when any
    # members are already assigned. Surface it here so an admin
    # can fix the mode of a freshly-created (still-empty) group
    # without deleting + recreating.
    mode: Optional[RoutingGroupMode] = Field(
        None,
        description=(
            "New inbound surface. Rejected with 409 if the group "
            "already has members — move them out first, change "
            "the mode, then move them back in."
        ),
    )
    routing_strategy: Optional[RoutingStrategy] = None
    complexity_config: Optional[Dict[str, Any]] = None
    complexity_floor_tier: Optional[ComplexityTier] = None
    cooldown_time: Optional[int] = Field(None, ge=0)
    fallback_models: Optional[List[str]] = None
    context_window_fallbacks: Optional[List[str]] = None
    content_policy_fallbacks: Optional[List[str]] = None
    default_fallbacks: Optional[List[str]] = None
    max_fallbacks: Optional[int] = Field(None, ge=1, le=20)
    required_capabilities: Optional[List[str]] = Field(None, description=_UNAVAILABLE)
    num_retries: Optional[int] = Field(None, ge=0, le=10)
    retry_after: Optional[int] = Field(None, ge=0)
    stream_timeout: Optional[float] = Field(
        None, gt=0, le=STREAM_TIMEOUT_MAX_SECONDS,
        description=(
            "Seconds. The longest a streamed call waits for the first chunk of "
            "the member it was routed to; a member that stays silent past it "
            "is given up on and the call fails over to the next. Greater than "
            "0 and at most 1800. Not set: only the member's own request "
            "timeout applies. "
            "Send null to clear it."
        ),
    )
    alerting_enabled: Optional[bool] = Field(None, description=_UNAVAILABLE + " `false` is accepted.")
    alerting_threshold: Optional[int] = Field(None, description=_UNAVAILABLE)
    alerting_webhook_url: Optional[str] = Field(None, description=_UNAVAILABLE)
    alerting_config: Optional[Dict[str, Any]] = Field(None, description=_UNAVAILABLE)
    routing_max_retries: Optional[int] = Field(None, description=_UNAVAILABLE)
    routing_retry_delay: Optional[float] = Field(None, description=_UNAVAILABLE)
    ttl_for_blocked_models: Optional[int] = Field(None, description=_UNAVAILABLE)
    max_parallel_requests: Optional[int] = Field(None, ge=1)
    rpm_limit: Optional[int] = Field(None, ge=1)
    tpm_limit: Optional[int] = Field(None, ge=1)
    router_settings: Optional[Dict[str, Any]] = Field(None, description=_UNAVAILABLE)
    enabled: Optional[bool] = None
    custom_metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _in_this_release(self):
        return _refuse_unavailable_routing_fields(self)


class RoutingGroupResponse(ResourceMetaFields):
    """
    Response model for routing group details
    """
    id: str
    tenant_id: str
    name: str
    description: Optional[str]
    # Mode is part of the on-the-wire contract every consumer
    # reads — admin UI dropdowns filter by it, the Test Router
    # picks an endpoint by it, the validators on the LLM Models
    # page check it. Surface it here as a string (the model
    # column is varchar(20), and we don't want to constrain the
    # response to a frozen enum that might lag the DB during a
    # multi-stage rollout — same softness pattern we use for
    # `routing_strategy` above).
    mode: str
    routing_strategy: str

    # Complexity-based routing (opt-in; None for every other strategy)
    complexity_config: Optional[Dict[str, Any]] = None
    complexity_floor_tier: Optional[str] = None

    # Configuration
    cooldown_time: int
    fallback_models: Optional[List[str]]
    context_window_fallbacks: Optional[List[str]]
    content_policy_fallbacks: Optional[List[str]] = None
    default_fallbacks: Optional[List[str]] = None
    max_fallbacks: Optional[int] = None

    # Retry Configuration
    num_retries: int
    retry_after: int
    stream_timeout: Optional[float] = Field(
        None,
        description=(
            "Seconds. The longest a streamed call waits for the first chunk of "
            "the member it was routed to; a member that stays silent past it "
            "is given up on and the call fails over to the next. Greater than "
            "0 and at most 1800. Not set: only the member's own request "
            "timeout applies."
        ),
    )

    # Rate Limiting
    max_parallel_requests: Optional[int]
    rpm_limit: Optional[int]
    tpm_limit: Optional[int]

    # Status
    enabled: bool
    custom_metadata: Optional[Dict[str, Any]]

    # Statistics
    num_models: int = Field(default=0, description="Number of models in this routing group")

    # Timestamps
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class RoutingGroupListItem(BaseModel):
    """
    Routing group info for list view — includes configuration indicators
    """
    id: str
    name: str
    description: Optional[str]
    # Inbound surface this group serves — surfaced on the list
    # row so admins can see at a glance "this is the image
    # group, that one's responses-only" without expanding each
    # row. The Routing Groups admin table renders it as a pill
    # next to the strategy badge.
    mode: str
    routing_strategy: str
    enabled: bool
    num_models: int

    # Configuration indicators
    num_retries: int = 3
    cooldown_time: int = 60

    # Fallback indicators
    has_fallbacks: bool = False
    fallback_count: int = 0
    fallback_models: Optional[List[str]] = None
    context_window_fallbacks: Optional[List[str]] = None
    content_policy_fallbacks: Optional[List[str]] = None
    default_fallbacks: Optional[List[str]] = None

    # Rate limits
    rpm_limit: Optional[int] = None
    tpm_limit: Optional[int] = None
    max_parallel_requests: Optional[int] = None

    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class RoutingGroupListResponse(BaseModel):
    """
    Response for listing routing groups
    GET /v1/admin/llm-router/routing-groups
    """
    routing_groups: List[RoutingGroupListItem]
    total: int


class RoutingGroupDeleteResponse(BaseModel):
    """
    Response after deleting routing group
    DELETE /v1/admin/llm-router/routing-groups/{group_id}
    """
    success: bool
    message: str
    group_id: str


# =============================================================================
# Model Deployment Information
# =============================================================================

class ModelDeployment(BaseModel):
    """
    Information about a deployed model in the router
    Represents a model that can be routed to
    """
    model_id: str = Field(..., description="Unique model ID")
    model_name: str = Field(..., description="Model display name")
    provider_model_name: str = Field(..., description="The provider's model identifier (e.g., gpt-5.2)")
    provider: str = Field(..., description="Provider (openai, anthropic, etc.)")
    enabled: bool = Field(..., description="Whether model is enabled")
    health_status: HealthStatus = Field(..., description="Current health status")
    routing_weight: int = Field(default=1, description="Weight for routing decisions")
    routing_group_id: Optional[str] = Field(None, description="Routing group ID if part of a group")
    routing_group_name: Optional[str] = Field(None, description="Routing group name if part of a group")
    avg_latency_ms: Optional[float] = Field(None, description="Average latency in milliseconds")
    total_requests: int = Field(default=0, description="Total requests served")
    error_rate: float = Field(default=0.0, description="Error rate percentage (0-100)")
    cost_per_1k_input: Optional[float] = Field(None, description="Cost per 1K input tokens (USD)")
    cost_per_1k_output: Optional[float] = Field(None, description="Cost per 1K output tokens (USD)")

    class Config:
        from_attributes = True


# =============================================================================
# Router Status Models (Enhanced)
# =============================================================================

class RoutingGroupModelSummary(BaseModel):
    """Summary of a single enabled model within a routing group status"""
    model_id: str
    model_name: str
    provider: str
    health_status: str
    routing_weight: int = 1
    avg_latency_ms: Optional[float] = None
    total_requests: int = 0
    failed_requests: int = 0
    error_rate: float = 0.0


class RoutingGroupStatusSummary(BaseModel):
    """Per-routing-group status summary (enabled groups only)"""
    id: str = Field(..., description="Routing group ID")
    name: str = Field(..., description="Display name")
    routing_strategy: str = Field(..., description="Active routing strategy")

    # Model health breakdown (enabled models only)
    total_models: int = Field(default=0, description="Total enabled models in group")
    healthy_models: int = Field(default=0, description="Healthy model count")
    unhealthy_models: int = Field(default=0, description="Unhealthy model count")

    # Aggregate performance
    total_requests: int = Field(default=0, description="Sum of requests across all models")
    total_failed: int = Field(default=0, description="Sum of failed requests")
    error_rate: float = Field(default=0.0, description="Aggregate error rate %")
    avg_latency_ms: Optional[float] = Field(None, description="Weighted average latency")

    # Configuration flags
    has_fallbacks: bool = Field(default=False, description="Whether fallbacks are configured")

    # Models detail
    models: List[RoutingGroupModelSummary] = Field(default=[], description="Per-model breakdown")


class RouterCacheStatus(BaseModel):
    """Router instance cache statistics"""
    cached_routers: int = Field(default=0, description="Currently cached router instances")
    max_cached_routers: int = Field(default=100, description="Max cache capacity")
    cache_utilization: float = Field(default=0.0, description="Cache utilization 0-1")
    cache_hits: int = Field(default=0, description="Total cache hits")
    cache_misses: int = Field(default=0, description="Total cache misses")
    hit_rate: float = Field(default=0.0, description="Hit rate 0-1")
    total_rebuilds: int = Field(default=0, description="Total router rebuilds")
    router_ttl_seconds: int = Field(default=3600, description="TTL for cached routers")


class RouterStatusResponse(BaseModel):
    """
    Router status information
    GET /v1/admin/llm-router/status

    Now includes per-routing-group breakdowns, router cache stats,
    and aggregate model health.
    """
    # Router State
    enabled: bool = Field(..., description="Whether router is enabled")
    routing_strategy: str = Field(..., description="Default routing strategy")

    # Deployment Statistics (enabled models only, excl. virtual)
    total_deployments: int = Field(..., description="Total enabled model deployments")
    healthy_deployments: int = Field(..., description="Healthy deployments")
    unhealthy_deployments: int = Field(..., description="Unhealthy deployments")
    unassigned_deployments: int = Field(default=0, description="Enabled models not in any routing group")

    # Routing Group Overview (enabled only)
    total_routing_groups: int = Field(default=0)

    # Per-routing-group detail
    routing_groups: List[RoutingGroupStatusSummary] = Field(
        default=[],
        description="Per-routing-group status summaries"
    )

    # Router instance cache
    router_cache: RouterCacheStatus = Field(
        default_factory=RouterCacheStatus,
        description="Router instance cache statistics"
    )

    # Reliability Features
    fallback_enabled: bool = Field(..., description="Whether fallback is enabled")
    health_check_enabled: bool = Field(..., description="Whether health checks are enabled")

    # Aggregate Performance Metrics
    total_requests: int = Field(default=0, description="Total requests across all models")
    total_errors: int = Field(default=0, description="Total failed requests")
    error_rate: float = Field(default=0.0, description="Overall error rate %")
    avg_latency_ms: float = Field(default=0.0, description="Weighted average latency (ms)")

    # Runtime Information
    uptime_seconds: int = Field(default=0, description="Router uptime in seconds")

    # Timestamp
    timestamp: str = Field(..., description="Status timestamp (ISO 8601)")

    class Config:
        from_attributes = True


# =============================================================================
# Router Testing Models
# =============================================================================

class RouterTestRequest(BaseModel):
    """
    Request body for `POST /v1/admin/llm-router/test`.

    The endpoint sends a real chat-completion through the proxy
    against the specified routing group and reports back which
    member model the router actually selected. There's no
    `model_id` here — that path used to mock the routing
    decision DB-side, which gave operators no real signal about
    whether their routing config worked. Targeting a specific
    model directly is what the LLM Models page already does.
    """
    routing_group_id: str = Field(
        ...,
        min_length=1,
        description="Routing group to dispatch the test through.",
    )
    message: str = Field(
        default="Hello! Reply with a single short sentence.",
        min_length=1,
        max_length=1000,
        description=(
            "Test message body. Sent as the user turn. Default "
            "is a small idempotent prompt that costs ~$0.0001 "
            "across most catalogue models — operators can leave "
            "it blank for a quick check or override it to test "
            "specific routing scenarios."
        ),
    )


class RouterTestResponse(BaseModel):
    """
    Result of a single router test. Returned from
    `POST /v1/admin/llm-router/test` and surfaced as a list by
    `GET /v1/admin/llm-router/test-history`.
    """
    timestamp: str = Field(..., description="ISO 8601 timestamp of the test.")
    routing_group_id: str = Field(..., description="Group dispatched against.")
    routing_group_name: str = Field(..., description="Group display name.")
    routing_strategy: str = Field(..., description="Strategy in effect at test time.")
    message: str = Field(..., description="Test prompt body sent.")
    selected_model: Optional[str] = Field(
        None,
        description=(
            "Member model the router actually chose. Read from "
            "the upstream's `model` field in the response. None "
            "if the proxy returned an error before dispatching."
        ),
    )
    selected_provider: Optional[str] = Field(
        None,
        description="Provider slug for the selected model (best-effort).",
    )
    response_text: Optional[str] = Field(
        None,
        description="First completion message content. None on error.",
    )
    latency_ms: Optional[float] = Field(
        None,
        description="End-to-end latency from control plane to upstream and back, ms.",
    )
    status_code: Optional[int] = Field(
        None,
        description="HTTP status the proxy returned (2xx on success).",
    )
    error: Optional[str] = Field(
        None,
        description="Human-readable error message when the test failed.",
    )


class RouterTestHistoryResponse(BaseModel):
    """
    Wrapped list payload for `GET /v1/admin/llm-router/test-
    history`. Empty list when the tenant has no recorded tests.
    """
    items: List[RouterTestResponse]
    total: int


# =============================================================================
# Deployment List Models
# =============================================================================

class DeploymentListRequest(BaseModel):
    """
    Optional filters for deployment list
    """
    filter_status: Optional[HealthStatus] = Field(
        None,
        description="Filter by health status"
    )
    routing_group_id: Optional[str] = Field(
        None,
        description="Filter by routing group"
    )
    enabled_only: bool = Field(
        default=False,
        description="Only return enabled deployments"
    )
    sort_by: Optional[str] = Field(
        None,
        description="Sort field (name, latency, requests, error_rate)"
    )


class DeploymentListResponse(BaseModel):
    """
    Response containing list of model deployments
    """
    deployments: List[ModelDeployment] = Field(..., description="List of model deployments")
    total: int = Field(..., description="Total number of deployments")
    healthy: int = Field(..., description="Number of healthy deployments")
    unhealthy: int = Field(..., description="Number of unhealthy deployments")


# =============================================================================
# Router Health Check Models
# =============================================================================

class RouterHealthCheckResponse(BaseModel):
    """
    Quick health check response for router
    """
    status: str = Field(..., description="Overall router status (healthy/degraded/unhealthy)")
    enabled: bool = Field(..., description="Whether router is enabled")
    deployments_available: int = Field(..., description="Number of available deployments")
    routing_groups_available: int = Field(default=0, description="Number of available routing groups")
    cache_available: bool = Field(..., description="Whether cache is available")
    timestamp: str = Field(..., description="Health check timestamp")


# =============================================================================
# Router Statistics Models
# =============================================================================

class RouterStatistics(BaseModel):
    """
    Aggregated router statistics
    """
    # Request Statistics
    total_requests: int = Field(default=0, description="Total requests processed")
    successful_requests: int = Field(default=0, description="Successfully processed requests")
    failed_requests: int = Field(default=0, description="Failed requests")
    error_rate: float = Field(default=0.0, description="Error rate percentage")

    # Performance Statistics
    avg_latency_ms: float = Field(default=0.0, description="Average latency")
    p50_latency_ms: Optional[float] = Field(None, description="50th percentile latency")
    p95_latency_ms: Optional[float] = Field(None, description="95th percentile latency")
    p99_latency_ms: Optional[float] = Field(None, description="99th percentile latency")

    # Model Distribution
    requests_by_model: Dict[str, int] = Field(
        default={},
        description="Request count by model name"
    )

    # Routing Group Distribution
    requests_by_routing_group: Dict[str, int] = Field(
        default={},
        description="Request count by routing group"
    )

    # Cache Statistics
    cache_hits: int = Field(default=0, description="Cache hits")
    cache_misses: int = Field(default=0, description="Cache misses")
    cache_hit_rate: float = Field(default=0.0, description="Cache hit rate percentage")

    # Time Period
    period_start: str = Field(..., description="Statistics period start (ISO 8601)")
    period_end: str = Field(..., description="Statistics period end (ISO 8601)")


class RouterStatisticsRequest(BaseModel):
    """
    Request for router statistics
    Optional time range
    """
    start_time: Optional[str] = Field(
        None,
        description="Start time (ISO 8601, default: last 24 hours)"
    )
    end_time: Optional[str] = Field(
        None,
        description="End time (ISO 8601, default: now)"
    )
    group_by_model: bool = Field(
        default=True,
        description="Include per-model breakdown"
    )
    group_by_routing_group: bool = Field(
        default=True,
        description="Include per-routing-group breakdown"
    )


# =============================================================================
# Error Response Models
# =============================================================================

class RouterErrorResponse(BaseModel):
    """
    Standard error response for router operations
    """
    error: str = Field(..., description="Error message")
    error_code: str = Field(..., description="Error code")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional error details")
    timestamp: str = Field(..., description="Error timestamp")

# =============================================================================
# Routing-group in-process micro-cache config (per tenant)
#
# Backs the small exact-match cache inside the Rust proxy's
# `dispatch_routing_group_request` — distinct from the platform-
# wide `semantic_cache_configs` (Redis exact + Qdrant similarity).
# See `app/database/models/router_cache_config.py` for the full
# distinction.
# =============================================================================

class RouterCacheConfigResponse(BaseModel):
    """GET /v1/admin/llm-router/cache-config response shape."""

    enabled: bool = Field(
        ...,
        description=(
            "Whether the routing-group in-process exact-match "
            "cache is active for this tenant. When disabled, "
            "every routing-group dispatch hits the upstream "
            "directly."
        ),
    )
    ttl_secs: int = Field(
        ...,
        ge=0,
        le=86400,
        description=(
            "Cached-response freshness window in seconds. Default "
            "300. The router serves a cached body for any "
            "identical request within this window."
        ),
    )
    updated_at: Optional[str] = Field(
        None,
        description="ISO timestamp of the last config change.",
    )

    class Config:
        from_attributes = True


class RouterCacheConfigUpdate(BaseModel):
    """PATCH body — both fields optional, only provided fields update."""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = Field(
        None,
        description="Enable / disable the routing micro-cache.",
    )
    ttl_secs: Optional[int] = Field(
        None,
        ge=0,
        le=86400,
        description="New TTL in seconds (0–86400).",
    )

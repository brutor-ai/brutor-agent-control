# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Routing Group Model
Manages load balancing and routing configuration for groups of LLM models
Configures the gateway's router for advanced routing strategies
"""

from sqlalchemy import Column, String, Boolean, Integer, Float, DateTime, Text, ForeignKey, Index, JSON
from sqlalchemy.sql import func
from sqlalchemy.orm import validates, relationship
from datetime import datetime, timezone
from typing import Dict, Any, List

from app.database.models.resource_mixins import ResourceAuthorshipMixin, ResourceVersionMixin
from app.database import Base

# Complexity tiers, cheapest → most capable. WIRE CONTRACT: these strings are
# shared verbatim with the Rust scorer (`services/complexity.rs`,
# `ComplexityTier::as_str`) and are stored in `llm_routing_group_tier.tier`.
# Changing one silently repoints an operator's traffic.
COMPLEXITY_TIERS = ("simple", "medium", "complex", "reasoning")


class LLMRoutingGroup(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """
    LLM Routing Group configuration for load balancing multiple models

    A routing group defines how multiple LLM models work together with:
    - Load balancing strategies
    - Fallback configuration
    - Failure handling and cooldown
    - Caching configuration
    - Pre-call checks and filtering
    - Alerting configuration

    Models reference a routing_group_id to be part of load balancing.
    When calling the router with model="my-gpt-4-group", the router will:
    1. Load balance across all models in that group
    2. Apply fallback strategies
    3. Handle failures with cooldown
    4. Cache responses if enabled
    """

    __tablename__ = "llm_routing_group"

    # Basic Information
    id = Column(String(100), primary_key=True, comment="Routing group ID - the model name callers address the group by")

    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this routing group belongs to"
    )

    name = Column(String(200), nullable=False, comment="Display name for this routing group")
    description = Column(Text, nullable=True, comment="Description of the routing group and its purpose")

    # =========================================================================
    # ROUTING STRATEGY
    # =========================================================================

    routing_strategy = Column(
        String(50),
        default="usage-based-routing-v2",
        comment="""
        Routing strategy for load balancing:
        - 'simple-shuffle': Random selection across all models
        - 'least-busy': Route to model with least pending requests
        - 'usage-based-routing': Basic usage-based routing
        - 'usage-based-routing-v2': Advanced usage-based routing (recommended)
        - 'latency-based-routing': Route to fastest model
        - 'cost-based-routing': Cheapest healthy model by blended input+output
          price. Deterministic, so across models of differing capability it is
          a static pin rather than a per-request optimisation.
        - 'complexity-based-routing': Score the prompt locally (no model call)
          and pick the model mapped to the resulting tier. Opt-in; inert unless
          tiers are configured.
        """
    )

    # =========================================================================
    # COMPLEXITY-BASED ROUTING (opt-in; ignored by every other strategy)
    # =========================================================================

    complexity_config = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment=(
            "Optional overrides for the complexity scorer: dimension weights, "
            "tier boundaries and keyword lists. NULL means the shipped "
            "defaults, which is the expected setting for almost everyone."
        ),
    )

    complexity_floor_tier = Column(
        String(20),
        nullable=True,
        comment=(
            "Governance floor: never route below this tier, whatever the "
            "prompt scores. The quality control for a deterministic scorer — "
            "a rule-based score has no confidence to threshold, so the "
            "enforceable knob is 'this group never goes cheaper than X'. "
            "One of simple|medium|complex|reasoning; NULL means no floor."
        ),
    )

    # =========================================================================
    # MODE — inbound surface this group routes
    # =========================================================================
    #
    # A routing group dispatches one inbound request to one
    # member, so members MUST share the inbound surface. The
    # mode is declared at creation time and enforced at every
    # member-assign site (`update_llm`, `create_llm`). Mixing
    # modes — e.g. one chat-completions member and one
    # responses-API member — would 400 at dispatch time on the
    # mismatched member because the request body shape only
    # fits one surface.
    #
    # Valid values mirror `LLModelMode`. The `mode='routing'`
    # value is reserved for the synthetic virtual model the
    # control plane creates per group (`mode == "routing"` is
    # what `redirect_if_routing` keys on in the proxy) — admins
    # don't pick it from the create form.
    mode = Column(
        String(20),
        nullable=False,
        default="chat",
        server_default="chat",
        comment=(
            "Inbound surface this routing group serves. Members "
            "must share this mode. Same valid set as `LLModel.mode` "
            "minus the synthetic 'routing' value."
        ),
    )

    # =========================================================================
    # FAILURE HANDLING & COOLDOWN
    # =========================================================================

    cooldown_time = Column(
        Integer,
        default=60,
        comment="Cooldown time in seconds if num_fails > allowed_fails. Model is excluded from rotation during cooldown."
    )

    disable_cooldowns = Column(
        Boolean,
        default=False,
        comment="If True, disables cooldowns for all models in this routing group"
    )

    # =========================================================================
    # FALLBACK CONFIGURATION
    # =========================================================================

    fallback_models = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""
        List of fallback routing group IDs to try if all models in this group fail.
        Example: ["backup-gpt-4-group", "emergency-claude-group"]
        The router tries these in order if the primary group is exhausted.
        """
    )

    context_window_fallbacks = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""
        Fallback routing group IDs for context window exceeded errors.
        Example: ["gpt-4-128k-group", "claude-group"]
        Format: [{self.id: [...]}] - built automatically by to_router_settings().
        """
    )

    content_policy_fallbacks = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""
        Fallback routing group IDs for content policy violation errors.
        Example: ["anthropic-group", "backup-group"]
        Format: [{self.id: [...]}] - built automatically by to_router_settings().
        """
    )

    default_fallbacks = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""
        Default fallback routing group IDs used if no specific fallbacks match.
        Example: ["general-fallback-group"]
        Applied when a model group has no configured fallbacks.
        """
    )

    # =========================================================================
    # PRE-CALL CHECKS & FILTERING
    # =========================================================================
    #
    # Note — there used to be three response-caching columns here
    # (`cache_responses`, `cache_kwargs`, `caching_groups`) carried
    # over from an earlier router. They were redundant and
    # unimplemented: the platform already has a first-class
    # response-cache component (exact + semantic) configured
    # per-tenant via `semantic_cache_configs`, which is what
    # actually serves cache reads/writes today. The columns were
    # dropped in migration `a037_drop_rg_cache_cost`.

    required_capabilities = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""
        Required model capabilities for pre-call filtering.
        Example: ["function_calling", "vision", "streaming"]
        Only models with ALL listed capabilities will be used.
        """
    )

    # =========================================================================
    # RETRY CONFIGURATION
    # =========================================================================

    num_retries = Column(
        Integer,
        default=3,
        comment="Number of retries for failed requests across the routing group"
    )

    retry_after = Column(
        Integer,
        default=5,
        comment="Seconds to wait before retrying after rate limit or failure"
    )

    stream_timeout = Column(
        Float,
        nullable=True,
        comment="""
        Timeout for streaming responses (seconds). Maximum time to wait for the first
        chunk (first token). Use this to abort 'hanging' providers and retry another model.
        If not set, the 'timeout' value is used.
        """
    )

    retry_policy = Column(
        JSON,
        nullable=True,
        comment="""
        Per-error-type retry counts. Overrides num_retries for specific error types.
        Example: {
            "AuthenticationErrorRetries": 0,
            "TimeoutErrorRetries": 3,
            "RateLimitErrorRetries": 3,
            "ContentPolicyViolationErrorRetries": 0,
            "InternalServerErrorRetries": 4
        }
        """
    )

    allowed_fails_policy = Column(
        JSON,
        nullable=True,
        comment="""
        Per-error-type allowed fails before cooldown. Overrides allowed_fails for specific errors.
        Example: {
            "BadRequestErrorAllowedFails": 1000,
            "AuthenticationErrorAllowedFails": 10,
            "TimeoutErrorAllowedFails": 12,
            "RateLimitErrorAllowedFails": 10000,
            "ContentPolicyViolationErrorAllowedFails": 15,
            "InternalServerErrorAllowedFails": 20
        }
        """
    )

    max_fallbacks = Column(
        Integer,
        nullable=True,
        comment="Maximum number of fallback attempts before giving up. Default: 5"
    )

    routing_strategy_args = Column(
        JSON,
        nullable=True,
        comment="""
        Strategy-specific arguments for fine-tuning routing behavior.
        For latency-based-routing:
            {"ttl": 10, "lowest_latency_buffer": 0.5}
        ttl = time window (seconds) for averaging latency.
        lowest_latency_buffer = % buffer for considering multiple deployments.
        """
    )

    # =========================================================================
    # ALERTING CONFIGURATION
    # =========================================================================

    alerting_enabled = Column(
        Boolean,
        default=False,
        comment="Enable alerting for slow/hanging responses"
    )

    alerting_threshold = Column(
        Integer,
        default=300,
        comment="Threshold for slow/hanging LLM responses (in seconds). Alerts sent if exceeded."
    )

    alerting_webhook_url = Column(
        String(500),
        nullable=True,
        comment="Webhook URL for alerts (Slack, Teams, etc.). Alerts sent here when threshold exceeded."
    )

    alerting_config = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""
        Additional alerting configuration.
        Example: {
            "alert_types": ["timeout", "failure", "rate_limit"],
            "email_recipients": ["ops@example.com"],
            "slack_channel": "#llm-alerts",
            "alert_interval": 300  # Min seconds between duplicate alerts
        }
        """
    )

    # =========================================================================
    # LOAD BALANCING TUNING
    # =========================================================================

    routing_max_retries = Column(
        Integer,
        default=3,
        comment="Max retries across different models in the group before giving up"
    )

    routing_retry_delay = Column(
        Float,
        default=0.1,
        comment="Delay between routing retries (seconds)"
    )

    ttl_for_blocked_models = Column(
        Integer,
        default=300,
        comment="How long to keep a model blocked (seconds) after max failures. Overrides cooldown_time if set."
    )

    # =========================================================================
    # RATE LIMITING (Group-level)
    # =========================================================================

    max_parallel_requests = Column(
        Integer,
        nullable=True,
        comment="Maximum parallel requests allowed across all models in this group"
    )

    rpm_limit = Column(
        Integer,
        nullable=True,
        comment="Requests per minute limit for entire routing group"
    )

    tpm_limit = Column(
        Integer,
        nullable=True,
        comment="Tokens per minute limit for entire routing group"
    )

    # =========================================================================
    # ADVANCED ROUTER SETTINGS
    # =========================================================================
    #
    # Note — there used to be three cost-tracking columns here
    # (`budget_limit_usd`, `current_month_cost_usd`,
    # `last_cost_reset`) carried over from an earlier router.
    # They were redundant: the platform already enforces
    # spend caps via the resource-group `usage_limits` system,
    # which has tenant + group + user scopes and integrates with
    # the proxy's per-request cost accounting. The columns were
    # dropped in migration `a037_drop_rg_cache_cost`.

    router_settings = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="""
        Advanced router settings.
        Example: {
            "redis_host": "localhost",
            "redis_port": 6379,
            "redis_password": "...",
            "routing_strategy_args": {...},
            "enable_loadbalancing_on_batch_endpoints": true,
            "enable_pre_call_checks": true,
            "model_group_alias": {"my-alias": "my-group-id"}
        }
        """
    )

    # =========================================================================
    # STATUS & METADATA
    # =========================================================================

    enabled = Column(
        Boolean,
        default=True,
        comment="Whether this routing group is enabled"
    )

    custom_metadata = Column(
        JSON(none_as_null=True),
        nullable=True,
        comment="Custom metadata for logging and tracking"
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    # One-to-many: A routing group has many models
    # Note: No delete cascade — models are disconnected (not deleted) when a routing group is removed.
    # The virtual model (mode="routing") is deleted explicitly in the service layer.
    models = relationship(
        "LLModel",
        back_populates="routing_group",
        foreign_keys="LLModel.routing_group_id",
        lazy="selectin",
        passive_deletes=True
    )

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        Index('ix_llm_routing_group_tenant', 'tenant_id'),
        Index('ix_llm_routing_group_tenant_enabled', 'tenant_id', 'enabled'),
        Index('ix_llm_routing_group_enabled', 'enabled'),
        {'comment': 'LLM routing groups for load balancing and failover configuration'}
    )

    # =========================================================================
    # VALIDATION
    # =========================================================================

    @validates('routing_strategy')
    def validate_routing_strategy(self, key, value):
        """Validate routing strategy"""
        valid_strategies = [
            'simple-shuffle',
            'least-busy',
            'usage-based-routing',
            'usage-based-routing-v2',
            'latency-based-routing',
            'cost-based-routing',
            'complexity-based-routing'
        ]
        if value and value not in valid_strategies:
            raise ValueError(f"routing_strategy must be one of: {', '.join(valid_strategies)}")
        return value

    @validates('complexity_floor_tier')
    def validate_complexity_floor_tier(self, key, value):
        """Validate the complexity floor tier against the scorer's vocabulary.

        These strings are the wire contract with the Rust scorer
        (`services/complexity.rs::ComplexityTier::as_str`). A typo here would
        silently disable the floor, which is the one control that stops a bad
        score sending traffic to a weak model.
        """
        if value is None or value == "":
            return None
        valid_tiers = COMPLEXITY_TIERS
        if value not in valid_tiers:
            raise ValueError(
                f"complexity_floor_tier must be one of: {', '.join(valid_tiers)}"
            )
        return value

    @validates('cooldown_time')
    def validate_cooldown_time(self, key, value):
        """Validate cooldown_time is positive"""
        if value is not None and value < 0:
            raise ValueError("cooldown_time must be >= 0")
        return value

    # =========================================================================
    # ROUTER CONFIGURATION
    # =========================================================================

    def to_router_settings(self) -> Dict[str, Any]:
        """
        Generate router settings for this routing group.

        Returns settings dict that can be passed to:
        Router(model_list=model_list, **settings)

        Fallback format: List[Dict[str, List[str]]]
            e.g. [{"openai-group": ["claude-group", "backup-group"]}]
        """
        settings = {
            "routing_strategy": self.routing_strategy or "usage-based-routing-v2",
        }

        # =====================================================================
        # FAILURE HANDLING & COOLDOWN
        # =====================================================================
        if self.cooldown_time is not None:
            settings["cooldown_time"] = self.cooldown_time

        if self.disable_cooldowns:
            settings["disable_cooldowns"] = True

        # =====================================================================
        # RETRY CONFIGURATION
        # =====================================================================
        if self.num_retries is not None:
            settings["num_retries"] = self.num_retries

        if self.retry_after is not None:
            settings["retry_after"] = self.retry_after

        if self.stream_timeout:
            settings["stream_timeout"] = self.stream_timeout

        if self.retry_policy:
            settings["retry_policy"] = self.retry_policy

        if self.allowed_fails_policy:
            settings["allowed_fails_policy"] = self.allowed_fails_policy

        if self.max_fallbacks is not None:
            settings["max_fallbacks"] = self.max_fallbacks

        # =====================================================================
        # FALLBACKS — format: List[Dict[str, List[str]]]
        # =====================================================================
        if self.fallback_models:
            settings["fallbacks"] = [{self.id: self.fallback_models}]

        if self.context_window_fallbacks:
            settings["context_window_fallbacks"] = [{self.id: self.context_window_fallbacks}]

        if self.content_policy_fallbacks:
            settings["content_policy_fallbacks"] = [{self.id: self.content_policy_fallbacks}]

        if self.default_fallbacks:
            settings["default_fallbacks"] = self.default_fallbacks

        # =====================================================================
        # ALERTING
        # =====================================================================
        if self.alerting_enabled:
            alerting_config = {
                "alerting_threshold": self.alerting_threshold or 300
            }

            if self.alerting_webhook_url:
                alerting_config["webhook_url"] = self.alerting_webhook_url

            if self.alerting_config:
                alerting_config.update(self.alerting_config)

            settings["alerting_config"] = alerting_config

        # =====================================================================
        # RATE LIMITING
        # =====================================================================
        if self.max_parallel_requests:
            settings["max_parallel_requests"] = self.max_parallel_requests

        if self.rpm_limit:
            settings["rpm"] = self.rpm_limit

        if self.tpm_limit:
            settings["tpm"] = self.tpm_limit

        # =====================================================================
        # ROUTING STRATEGY ARGS
        # =====================================================================
        if self.routing_strategy_args:
            settings["routing_strategy_args"] = self.routing_strategy_args

        # =====================================================================
        # ADVANCED — raw router_settings override (applied last)
        # =====================================================================
        if self.router_settings:
            settings.update(self.router_settings)

        return settings


    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            # Inbound surface this group serves; enforced at member-
            # assign time so all members share the mode.
            "mode": self.mode or "chat",
            # Routing strategy
            "routing_strategy": self.routing_strategy,
            "routing_strategy_args": self.routing_strategy_args,
            # Complexity-based routing (opt-in)
            "complexity_config": self.complexity_config,
            "complexity_floor_tier": self.complexity_floor_tier,
            # Failure handling
            "cooldown_time": self.cooldown_time,
            "disable_cooldowns": self.disable_cooldowns,
            # Retries
            "num_retries": self.num_retries,
            "retry_after": self.retry_after,
            "stream_timeout": self.stream_timeout,
            "retry_policy": self.retry_policy,
            "allowed_fails_policy": self.allowed_fails_policy,
            # Fallbacks
            "fallback_models": self.fallback_models,
            "context_window_fallbacks": self.context_window_fallbacks,
            "content_policy_fallbacks": self.content_policy_fallbacks,
            "default_fallbacks": self.default_fallbacks,
            "max_fallbacks": self.max_fallbacks,
            # Alerting
            "alerting_enabled": self.alerting_enabled,
            "alerting_threshold": self.alerting_threshold,
            # Rate limiting
            "max_parallel_requests": self.max_parallel_requests,
            "rpm_limit": self.rpm_limit,
            "tpm_limit": self.tpm_limit,
            # Status
            "enabled": self.enabled,
            "num_models": len([m for m in (self.models or []) if not m.is_routing]),
            # Audit info
            "version": self.version,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            # Timestamps
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<LLMRoutingGroup(id={self.id}, name={self.name}, strategy={self.routing_strategy}, enabled={self.enabled})>"


class LLMRoutingGroupTier(Base):
    """One complexity tier → model mapping inside a routing group.

    Only consulted by the `complexity-based-routing` strategy; a group using
    any other strategy simply has no rows here, which is why the whole feature
    is inert until an operator configures it.

    A tier may map to several models (ordered by `position`) so the group's
    existing selector — weighted, least-busy, latency — still chooses *within*
    the tier. The tier picks the pool; the established machinery picks the
    member.
    """

    __tablename__ = "llm_routing_group_tier"

    id = Column(String(36), primary_key=True, comment="UUID primary key")
    tenant_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Owning tenant — every query is tenant-scoped",
    )
    routing_group_id = Column(
        String(36),
        ForeignKey("llm_routing_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Routing group this tier mapping belongs to",
    )
    tier = Column(
        String(20),
        nullable=False,
        comment="One of simple|medium|complex|reasoning (see COMPLEXITY_TIERS)",
    )
    llm_model_id = Column(
        String(36),
        ForeignKey("llm_model.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Model to use for this tier",
    )
    position = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Order within the tier; lower is preferred",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_llm_rg_tier_group_tier", "routing_group_id", "tier"),
        Index("ix_llm_rg_tier_tenant_group", "tenant_id", "routing_group_id"),
    )

    @validates("tier")
    def validate_tier(self, key, value):
        if value not in COMPLEXITY_TIERS:
            raise ValueError(f"tier must be one of: {', '.join(COMPLEXITY_TIERS)}")
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "routing_group_id": self.routing_group_id,
            "tier": self.tier,
            "llm_model_id": self.llm_model_id,
            "position": self.position,
        }

    def __repr__(self):
        return (
            f"<LLMRoutingGroupTier(group={self.routing_group_id}, "
            f"tier={self.tier}, model={self.llm_model_id})>"
        )
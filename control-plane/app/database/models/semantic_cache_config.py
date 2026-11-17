# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Semantic Cache Configuration Database Model

Controls the two-layer semantic cache for LLM responses:
- Layer 1: Exact-match via Redis (SHA-256 hash of normalised prompt + model)
- Layer 2: Semantic similarity via Qdrant vector database

Multiple configs per tenant are supported. Each config can be
associated with one or more Resource Groups — exactly like
GuardrailConfig and LLM models.

- A config associated with specific groups ONLY applies to requests
  from users in those groups.
- When a user belongs to multiple groups, the first matching config
  is used (priority-based).

Association table:
    resource_group_cache_configs  (resource_group_id, cache_config_id)
"""

from sqlalchemy import (
    Column, String, Boolean, DateTime, JSON, Float, Table,
    ForeignKey, Index, Integer
)
from sqlalchemy.sql import func
from sqlalchemy.orm import validates, relationship
from typing import Dict, Any, List

from app.database import Base


# =============================================================================
# Many-to-many association table (matches resource_group_guardrails pattern)
# =============================================================================

resource_group_cache_configs = Table(
    "resource_group_cache_configs",
    Base.metadata,
    Column(
        "resource_group_id",
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "cache_config_id",
        String(255),
        ForeignKey("semantic_cache_configs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    comment="Associates semantic cache configs with resource groups",
)


class SemanticCacheConfig(Base):
    """
    Semantic cache configuration — assignable to Resource Groups.

    Multiple configs per tenant. Each controls how LLM responses are
    cached for the assigned groups: exact-match TTL, semantic similarity
    threshold, time bucketing, exclusion rules, and shared pool settings.

    Cache isolation: each resource group has its own cache namespace.
    The optional shared pool allows cross-group cache sharing for
    common queries (e.g., FAQs, general knowledge).
    """

    __tablename__ = "semantic_cache_configs"

    # =========================================================================
    # IDENTITY
    # =========================================================================

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant",
    )

    name = Column(
        String(255), nullable=False, default="Default Cache",
        comment="Human-readable name (unique within tenant)",
    )

    description = Column(
        String(1024), nullable=True,
        comment="Admin-facing description of this cache config's purpose",
    )

    # =========================================================================
    # RESOURCE GROUP ASSOCIATION
    # =========================================================================

    groups = relationship(
        "ResourceGroup",
        secondary=resource_group_cache_configs,
        back_populates="cache_configs",
        lazy="selectin",
    )

    # =========================================================================
    # GLOBAL TOGGLE
    # =========================================================================

    enabled = Column(
        Boolean, default=False,
        comment="Master switch — False disables ALL caching for this config",
    )

    # =========================================================================
    # EXACT MATCH SETTINGS (Layer 1 — Redis)
    # =========================================================================

    exact_match_enabled = Column(
        Boolean, default=True,
        comment="Enable exact-match caching via Redis (SHA-256 hash lookup)",
    )

    # Streamable-cache key composition.
    #
    # 'last_user_turn' (default): the Layer 1 (exact-match)
    # cache key hashes only system messages + the most recent
    # user turn (plus model, temperature, tools,
    # response_format). Multi-turn chat history is excluded
    # from the key, so the same question asked in two
    # different conversations collides cleanly — that's what
    # gives the exact layer real-world hit rate on the chat
    # surface.
    #
    # 'full': hash the entire messages array. Strictest
    # correctness (the answer can legitimately depend on
    # prior turns), but exact-match hit rate on organic
    # multi-turn chat is effectively zero. Use for groups
    # where context-dependent prompts dominate.
    #
    # Layer 2 (semantic) is unaffected — it always embeds
    # only system + most-recent-user content anyway.
    cache_key_mode = Column(
        String(20),
        default="last_user_turn",
        server_default="last_user_turn",
        nullable=False,
        comment=(
            "Cache key composition mode: 'last_user_turn' (default; "
            "high chat hit rate) or 'full' (strict context-aware "
            "correctness)."
        ),
    )

    exact_match_ttl_seconds = Column(
        Integer, default=3600,
        comment="Time-to-live for exact-match cache entries (seconds)",
    )

    # =========================================================================
    # SEMANTIC MATCH SETTINGS (Layer 2 — Qdrant)
    # =========================================================================

    semantic_match_enabled = Column(
        Boolean, default=True,
        comment="Enable semantic similarity caching via Qdrant",
    )

    similarity_threshold = Column(
        Float, default=0.95,
        comment="Minimum cosine similarity score to consider a cache hit (0.0-1.0)",
    )

    semantic_match_ttl_seconds = Column(
        Integer, default=86400,
        comment="Time-to-live for semantic cache entries (seconds)",
    )

    max_entries = Column(
        Integer, default=10000,
        comment="Maximum number of cached entries per resource group in Qdrant",
    )

    # =========================================================================
    # EMBEDDING MODEL
    # =========================================================================
    # NOTE: embedding_model_id was removed in migration
    # a001_primary_group_tenant_embedding. The tenant-wide default on
    # Tenant.default_embedding_model_id applies here too, so cache keys
    # live in the same vector space as KB chunks within a tenant.

    # =========================================================================
    # TIME BUCKETING
    # =========================================================================

    time_bucketing = Column(
        String(10), default="daily",
        comment=(
            "Controls cache key time scoping to prevent stale responses. "
            "'daily' = same prompt on different days is a cache miss (default, safest). "
            "'hourly' = same prompt in different hours is a cache miss. "
            "'none' = pure content match, for FAQs / static knowledge."
        ),
    )

    # =========================================================================
    # EXCLUSION RULES
    # =========================================================================

    exclude_tool_calls = Column(
        Boolean, default=True,
        comment="Exclude requests with tools/functions (responses depend on real-time data)",
    )

    exclude_temperature_above = Column(
        Float, default=1.0,
        comment="Exclude requests with temperature above this threshold (creative outputs shouldn't be cached)",
    )

    exclude_guardrail_flagged = Column(
        Boolean, default=True,
        comment="Exclude requests that triggered guardrail checks (even if they passed)",
    )

    # =========================================================================
    # SHARED POOL
    # =========================================================================

    shared_pool_enabled = Column(
        Boolean, default=False,
        comment="Enable shared cache pool for cross-group caching (opt-in per group)",
    )

    shared_pool_contribute = Column(
        Boolean, default=False,
        comment="Allow this group's cache entries to be written to the shared pool",
    )

    shared_pool_consume = Column(
        Boolean, default=False,
        comment="Allow this group to read from the shared pool on cache miss",
    )

    shared_pool_model_filter = Column(
        JSON, nullable=True,
        comment="List of model IDs eligible for shared pool (null = all authorised models)",
    )

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_by = Column(String(255), nullable=True, comment="Admin who created this config")

    __table_args__ = (
        Index("ix_scc_tenant", "tenant_id"),
        Index("ix_scc_tenant_name", "tenant_id", "name", unique=True),
        {"comment": "Semantic cache configurations assignable to resource groups"},
    )

    # =========================================================================
    # VALIDATION
    # =========================================================================

    VALID_TIME_BUCKETS = {"daily", "hourly", "none"}
    VALID_CACHE_KEY_MODES = {"last_user_turn", "full"}

    @validates("time_bucketing")
    def validate_time_bucketing(self, key, value):
        if value and value not in self.VALID_TIME_BUCKETS:
            raise ValueError(f"{key} must be one of: {', '.join(self.VALID_TIME_BUCKETS)}")
        return value

    @validates("cache_key_mode")
    def validate_cache_key_mode(self, key, value):
        if value and value not in self.VALID_CACHE_KEY_MODES:
            raise ValueError(
                f"{key} must be one of: {', '.join(self.VALID_CACHE_KEY_MODES)}"
            )
        return value

    @validates("similarity_threshold")
    def validate_similarity_threshold(self, key, value):
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError(f"{key} must be between 0.0 and 1.0")
        return value

    @validates("exclude_temperature_above")
    def validate_exclude_temperature(self, key, value):
        if value is not None and not (0.0 <= value <= 2.0):
            raise ValueError(f"{key} must be between 0.0 and 2.0")
        return value

    # =========================================================================
    # HELPERS
    # =========================================================================

    @property
    def group_ids(self) -> List[str]:
        """Return list of associated resource group IDs."""
        return [g.id for g in (self.groups or [])]

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for API responses."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "group_ids": self.group_ids,
            "exact_match": {
                "enabled": self.exact_match_enabled,
                "ttl_seconds": self.exact_match_ttl_seconds,
                "key_mode": self.cache_key_mode or "last_user_turn",
            },
            "semantic_match": {
                "enabled": self.semantic_match_enabled,
                "similarity_threshold": self.similarity_threshold,
                "ttl_seconds": self.semantic_match_ttl_seconds,
            },
            "time_bucketing": self.time_bucketing,
            "exclusions": {
                "tool_calls": self.exclude_tool_calls,
                "temperature_above": self.exclude_temperature_above,
                "guardrail_flagged": self.exclude_guardrail_flagged,
            },
            "shared_pool": {
                "enabled": self.shared_pool_enabled,
                "contribute": self.shared_pool_contribute,
                "consume": self.shared_pool_consume,
            },
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "created_by": self.created_by,
        }

    def __repr__(self) -> str:
        return (
            f"<SemanticCacheConfig(id={self.id!r}, name={self.name!r}, "
            f"tenant={self.tenant_id!r}, enabled={self.enabled})>"
        )

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Prompt Cache Policy — provider-side prompt caching configuration.

This is distinct from SemanticCacheConfig (the gateway's own exact +
semantic response cache). A prompt-cache policy controls how the Rust
proxy injects provider-specific prompt-cache markers (Anthropic's
`cache_control`, OpenAI's implicit prefix caching, Google Gemini's
`cachedContents`) on its way out of the gateway.

Key separation:
  * Response cache = whether we call the provider at all.
  * Prompt cache   = how cheap the provider call is when we do make it.

Policy lives on a per-resource-group binding (same pattern as
`semantic_cache_configs`). Model capabilities (mechanism, max
breakpoints, supported TTLs, pricing multipliers) live on the
`llm_model` catalog row, not here — admins read capabilities, they
don't configure them.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    text,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# =============================================================================
# Binding table — one policy per resource group at a time.
#
# The UNIQUE(resource_group_id) constraint matches the runtime loader's
# assumption that `group_id → policy` is a function, not a relation.
# Flipping a group from policy A to policy B is DELETE + INSERT, not an
# UPSERT, so the stored key never collides.
# =============================================================================

resource_group_prompt_cache_policies = Table(
    "resource_group_prompt_cache_policies",
    Base.metadata,
    Column(
        "resource_group_id",
        String(50),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "prompt_cache_policy_id",
        String(50),
        ForeignKey("prompt_cache_policies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # Per-model override (Gap #10). NULL = the group's default policy
    # (one allowed per group). NOT NULL = an override that wins for
    # that specific model. Uniqueness is enforced via partial indexes
    # built in migration a029_prompt_cache_per_model_override —
    # model_id is deliberately NOT in the PK because Postgres requires
    # PK columns to be NOT NULL, which would break the NULL=default
    # row contract.
    Column(
        "model_id",
        String(50),
        ForeignKey("llm_model.id", ondelete="CASCADE"),
        nullable=True,
    ),
    Index(
        "ix_resource_group_prompt_cache_policies_policy",
        "prompt_cache_policy_id",
    ),
    comment=(
        "Binds prompt-cache policies to resource groups. NULL model_id "
        "is the group default; non-NULL is a per-model override."
    ),
)


class PromptCachePolicy(Base):
    """
    Provider-side prompt-cache policy — assignable to Resource Groups.
    """

    __tablename__ = "prompt_cache_policies"

    id = Column(String(50), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    # Strategy controls which breakpoints we inject on Anthropic-style
    # providers. OpenAI / automatic-prefix providers ignore the value
    # (caching is implicit), but we keep the field so the policy row
    # is mechanism-agnostic and tenants can run the same policy across
    # mixed catalogs.
    #
    # Values:
    #   * tools_only        — cache tool definitions only. Safest,
    #                         cheapest writes. Use when the system
    #                         prompt is volatile.
    #   * tools_and_system  — cache tools + system prompt. Default.
    #                         Matches Anthropic's documented ordering.
    #   * aggressive        — tools + system + stable history prefix.
    #                         Higher write premium; wins big on
    #                         long-running conversations.
    strategy = Column(String(40), nullable=False, default="tools_and_system", server_default=text("'tools_and_system'"))

    # 300 = 5 min (every provider supports). 3600 = 1 hour (Anthropic
    # extended-TTL beta; guarded per-model by catalog flags).
    ttl_seconds = Column(Integer, nullable=False, default=300, server_default=text("300"))

    # Don't bother with prompt caching below this size — the write
    # premium (~1.25× on Anthropic) doesn't pay off on short prompts
    # and OpenAI only auto-caches ≥ 1024 tokens anyway.
    min_request_tokens = Column(Integer, nullable=False, default=1024, server_default=text("1024"))

    # Pin routing to the provider that first warmed the cache within
    # TTL. Without this, round-robin routing groups destroy hit rate.
    sticky_routing = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    # Regex patterns the admin UI lints system prompts against —
    # anything matching gets a "this will invalidate prompt cache on
    # every call" warning. Not enforced at runtime; operator lint
    # only. Examples: `\\d{4}-\\d{2}-\\d{2}T`, `request_id=`,
    # `\\b[0-9a-f]{8}-[0-9a-f]{4}-`.
    volatile_patterns = Column(JSON, nullable=False, default=list, server_default=text("'[]'"))

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    resource_groups = relationship(
        "ResourceGroup",
        secondary=resource_group_prompt_cache_policies,
        back_populates="prompt_cache_policies",
        lazy="dynamic",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_prompt_cache_policies_tenant_name"),
        Index("ix_prompt_cache_policies_tenant", "tenant_id"),
    )

    def to_dict(self, include_groups: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "enabled": bool(self.enabled),
            "strategy": self.strategy,
            "ttl_seconds": int(self.ttl_seconds),
            "min_request_tokens": int(self.min_request_tokens),
            "sticky_routing": bool(self.sticky_routing),
            "volatile_patterns": self.volatile_patterns or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_groups:
            # `resource_groups` is a dynamic loader; `.all()` forces it.
            data["assigned_group_ids"] = [g.id for g in self.resource_groups.all()]
        return data

    @staticmethod
    def allowed_strategies() -> List[str]:
        return ["tools_only", "tools_and_system", "aggressive"]

    @staticmethod
    def allowed_ttls() -> List[int]:
        # 5-minute and 1-hour. Anything else is a typo, not a feature
        # — provider APIs only honour these two values today.
        return [300, 3600]

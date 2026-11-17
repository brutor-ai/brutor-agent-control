# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Semantic Cache Tool Rules — per-capability caching control.

When exclude_tool_calls=false on a SemanticCacheConfig, these rules
determine which specific MCP capabilities produce cacheable responses.

Rules are evaluated most-specific-first:
1. Exact (server_config_id, capability_name) match
2. Server-level (server_config_id, *) match
3. Default: not cacheable (opt-in model)
"""

from sqlalchemy import (
    Column, String, Boolean, Integer, DateTime, ForeignKey, Index,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class SemanticCacheToolRule(Base):
    """
    Per-capability cache rule for a semantic cache configuration.

    When a cache config has exclude_tool_calls=false, the proxy consults
    these rules to decide whether responses involving specific tool calls
    should be cached.
    """
    __tablename__ = "semantic_cache_tool_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)

    cache_config_id = Column(
        String(255),
        ForeignKey("semantic_cache_configs.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent cache configuration",
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Owning tenant",
    )

    # ── What this rule applies to ────────────────────────────────────────

    server_config_id = Column(
        String(255),
        nullable=True,
        comment="Server config ID. NULL = applies to all servers.",
    )

    capability_type = Column(
        String(20),
        nullable=True,
        comment="Capability type: 'tool', 'resource', 'prompt'. NULL = all types.",
    )

    capability_name = Column(
        String(500),
        nullable=True,
        comment="Capability name (e.g., 'skill__hello-world'). NULL = all capabilities on server.",
    )

    # ── Cache decision ───────────────────────────────────────────────────

    cacheable = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether matching responses are cacheable",
    )

    # ── Priority (higher = checked first) ────────────────────────────────

    priority = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Rule evaluation priority. Higher = checked first. Use for specific overrides.",
    )

    # ── Audit ────────────────────────────────────────────────────────────

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # ── Relationships ────────────────────────────────────────────────────

    cache_config = relationship(
        "SemanticCacheConfig",
        backref="tool_rules",
    )

    # ── Constraints & Indexes ────────────────────────────────────────────

    __table_args__ = (
        UniqueConstraint(
            'cache_config_id', 'server_config_id', 'capability_type', 'capability_name',
            name='uix_cache_tool_rule',
        ),
        Index('ix_ctr_config', 'cache_config_id'),
        Index('ix_ctr_tenant', 'tenant_id'),
        {'comment': 'Per-capability caching rules for semantic cache configurations'},
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cache_config_id": self.cache_config_id,
            "server_config_id": self.server_config_id,
            "capability_type": self.capability_type,
            "capability_name": self.capability_name,
            "cacheable": self.cacheable,
            "priority": self.priority,
        }

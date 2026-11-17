# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Router Cache Configuration — per-tenant control for the
routing-group in-process exact-match micro-cache that lives
inside the Rust core proxy.

This is a DIFFERENT cache from the platform-wide response cache:

  * `SemanticCacheConfig` (semantic_cache_configs) — Redis exact +
    Qdrant similarity, configured per tenant, drives ALL model
    traffic (direct calls and routing-group dispatches alike). The
    enterprise-grade response cache.

  * `RouterCacheConfig` (this model) — A tiny in-memory map inside
    the routing dispatcher in `brutor-gateway-core/src/services/
    llm_router.rs`. Short-TTL, exact-key only, NOT shared across
    proxy nodes (process-local). Its only job is to defang dumb
    retry storms — when an SDK retries an identical request after
    its own timeout, the router doesn't re-pay the upstream cost.

The Rust proxy reads this row at the cache read/write sites in
`dispatch_routing_group_request` and gates on `enabled` + uses
`ttl_secs` for the cache-write TTL. One row per tenant; auto-
created with sensible defaults (enabled=true, ttl_secs=300) if
the row doesn't exist yet.
"""

from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey, Integer, CheckConstraint
)
from sqlalchemy.sql import func
from typing import Dict, Any

from app.database import Base


class RouterCacheConfig(Base):
    """One row per tenant; defaults match pre-config behaviour."""

    __tablename__ = "router_cache_configs"

    tenant_id = Column(
        String(64),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
        comment=(
            "Per-tenant config for the routing-group in-process "
            "micro-cache. The cache itself is per-process inside "
            "the Rust proxy; this row is the per-tenant policy "
            "that gates it."
        ),
    )

    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment=(
            "Whether the routing micro-cache is active. When "
            "disabled, every routing-group dispatch hits the "
            "upstream directly — the safety-net retry-defang is "
            "off."
        ),
    )

    ttl_secs = Column(
        Integer,
        nullable=False,
        default=300,
        server_default="300",
        comment=(
            "Cached-response freshness window in seconds. Default "
            "300 (5 min) matches the legacy hard-coded value. Range "
            "0–86400 (one day) — anything beyond a few minutes "
            "increases the chance of serving a stale tool/RAG-"
            "informed answer; the router cache is content-addressed "
            "by request hash and is not aware of upstream tool/"
            "knowledge-base state changes."
        ),
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "ttl_secs >= 0 AND ttl_secs <= 86400",
            name="router_cache_configs_ttl_range",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "enabled": bool(self.enabled),
            "ttl_secs": int(self.ttl_secs),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        # Safe-repr pattern (same lesson as LlmProvider):
        # never trigger lazy-load from inside `__repr__` because
        # exception formatters call repr() on detached instances
        # and the resulting AttributeError masquerades as a
        # different bug (CORS, validation, etc.).
        from sqlalchemy import inspect as sa_inspect
        try:
            state = sa_inspect(self).dict
        except Exception:
            state = {}
        tid = state.get("tenant_id", "<unloaded>")
        en = state.get("enabled", "<unloaded>")
        ttl = state.get("ttl_secs", "<unloaded>")
        return f"<RouterCacheConfig tenant={tid!r} enabled={en!r} ttl_secs={ttl!r}>"

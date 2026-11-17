# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Agent implementation releases

One row per ``(tenant, agent identity, name, version, build)`` the platform
has observed. WRITTEN by the Rust core's run finaliser (an upsert per
finalised run whose actor is an agent identity and which carries a name and
version); READ by the control plane (the identity's release timeline, the
composition's current release, the agent checks, the assurance report).

``build`` is NOT NULL with ``''`` meaning "no build": it is part of the
unique key, and NULLs never collide in a unique constraint (the same reason
``actor_id='all'`` is a sentinel on baselines).

``approved`` is deliberately NOT a column: it is derived at read time from
the identity's ``approved_versions`` (``app.services.agent_implementation``),
so approving a version takes effect for every release at once and can never
disagree with the declaration.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class AgentImplementationRelease(Base):
    """An observed release of an agent identity's software."""

    __tablename__ = "agent_implementation_releases"

    id = Column(String(64), primary_key=True)  # rel-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    agent_id = Column(
        String(255), ForeignKey("agent_identities.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(64), nullable=False)
    version = Column(String(64), nullable=False)
    build = Column(String(128), nullable=False, default="", server_default="")
    trust = Column(String(16), nullable=False)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    run_count = Column(Integer, nullable=False, default=0, server_default="0")
    #: Distinct implementation fingerprints seen for this release (≤ 16).
    fingerprints = Column(
        JSONB, nullable=False, default=list, server_default=text("'[]'")
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "agent_id", "name", "version", "build",
            name="uq_agent_implementation_releases_key",
        ),
        CheckConstraint(
            "trust IN ('verified', 'declared')",
            name="ck_agent_implementation_releases_trust",
        ),
        Index(
            "ix_agent_implementation_releases_last_seen",
            "tenant_id", "agent_id", "last_seen_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        fps = list(self.fingerprints or [])
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "name": self.name,
            "version": self.version,
            "build": self.build or None,
            "trust": self.trust,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "run_count": int(self.run_count or 0),
            "fingerprints": fps,
            "fingerprint_count": len(fps),
        }


__all__ = ["AgentImplementationRelease"]

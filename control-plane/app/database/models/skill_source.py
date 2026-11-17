# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""SkillSource — git-sourced Agent Skill repository registration.

One row per registered repo. The Rust scheduler in gateway-core
polls these rows, walks the GitHub tree, and POSTs discovered
SKILL.md files back to the Python ingest endpoint, which materialises
them into `agent_skills` rows tagged with `source_repo_id` +
`source_commit_sha` + `source_path`.

Operators register a source once, then push updates to the repo and
the scheduler picks them up on the next tick. Same mental model as
Obot's `SkillRepository` CRD.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    text,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# Sync status string constants. Kept as strings (not an SQLEnum) because
# the Rust scheduler reads/writes the same column and we don't want to
# proliferate PG-specific enum types we have to keep in lockstep across
# two languages.
SYNC_STATUS_IDLE = "idle"
SYNC_STATUS_SYNCING = "syncing"
SYNC_STATUS_FAILED = "failed"

ALLOWED_SYNC_STATUSES = {SYNC_STATUS_IDLE, SYNC_STATUS_SYNCING, SYNC_STATUS_FAILED}

# Hard floors on the sync cadence — protect GitHub from us (their
# unauthenticated REST limit is 60/h) and protect us from a buggy
# operator who sets 1s and DoS's their own gateway.
MIN_SYNC_INTERVAL_SECONDS = 300       # 5 min
MAX_SYNC_INTERVAL_SECONDS = 86400     # 24 h
DEFAULT_SYNC_INTERVAL_SECONDS = 3600  # 1 h — Obot's default


class SkillSource(Base):
    """Registered git source for Agent Skill discovery.

    Lifecycle: created via admin API → scheduler picks it up → walks
    the repo → POSTs findings to the Python ingest endpoint → status
    transitions through `syncing` → (`idle` | `failed`).
    """

    __tablename__ = "skill_sources"

    id = Column(String(255), primary_key=True, comment="ULID format: sks-01J...")
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    name = Column(String(255), nullable=False)
    repo_url = Column(String(1024), nullable=False)
    git_ref = Column(String(255), nullable=False, default="main", server_default="main")
    subpath = Column(String(512), nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    sync_interval_seconds = Column(
        Integer, nullable=False, default=DEFAULT_SYNC_INTERVAL_SECONDS,
        server_default="3600",
    )
    auto_publish = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # Lifecycle state — the Rust scheduler CAS's this from idle→syncing
    # at the start of a tick and back to idle/failed at the end. If a
    # tick crashes mid-sync the row is left in `syncing` until the next
    # operator-triggered refresh or a stale-state sweep — see the
    # scheduler's recover_stuck_syncs() for the rescue path.
    status = Column(String(50), nullable=False, default=SYNC_STATUS_IDLE, server_default="idle")
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_attempted_at = Column(DateTime(timezone=True), nullable=True)
    last_commit_sha = Column(String(64), nullable=True)
    sync_error = Column(Text, nullable=True)
    discovered_skill_count = Column(Integer, nullable=False, default=0, server_default="0")

    github_token_secret_ref = Column(String(512), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_by = Column(String(255), nullable=True)

    # The reverse side of the agent_skills.source_repo_id FK is declared
    # via primaryjoin to avoid a circular import between the two model
    # modules. The FK + ondelete='SET NULL' is on the agent_skills side.
    skills = relationship(
        "AgentSkill",
        back_populates="source_repo",
        foreign_keys="AgentSkill.source_repo_id",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_url",
            "git_ref",
            name="uq_skill_source_tenant_repo_ref",
        ),
        Index(
            "ix_skill_sources_tenant_status",
            "tenant_id",
            "enabled",
            "status",
        ),
        Index("ix_skill_sources_last_synced_at", "last_synced_at"),
    )

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def begin_sync(self) -> None:
        """Mark the row as actively syncing. Caller commits."""
        self.status = SYNC_STATUS_SYNCING
        self.last_attempted_at = datetime.now(timezone.utc)
        self.sync_error = None

    def finish_sync(
        self,
        *,
        commit_sha: Optional[str],
        discovered_count: int,
    ) -> None:
        """Mark a successful completion."""
        self.status = SYNC_STATUS_IDLE
        self.last_synced_at = datetime.now(timezone.utc)
        self.last_commit_sha = commit_sha
        self.discovered_skill_count = discovered_count
        self.sync_error = None

    def record_error(self, message: str) -> None:
        """Mark a sync attempt as failed. Caller commits.

        `last_attempted_at` is updated so the scheduler's
        next-due ordering keeps moving forward — otherwise a
        consistently-failing source would starve other sources.
        """
        self.status = SYNC_STATUS_FAILED
        self.last_attempted_at = datetime.now(timezone.utc)
        self.sync_error = (message or "unknown error")[:8000]

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """Has enough time passed since the last attempt?"""
        if not self.enabled:
            return False
        if self.status == SYNC_STATUS_SYNCING:
            return False  # another worker (or stuck) has it
        if self.last_attempted_at is None:
            return True
        anchor = now or datetime.now(timezone.utc)
        elapsed = (anchor - self.last_attempted_at).total_seconds()
        return elapsed >= self.sync_interval_seconds

    def to_dict(self) -> Dict[str, Any]:
        """Serialisation matches the Pydantic response model. Keep in
        sync — both must list the same keys."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "repo_url": self.repo_url,
            "git_ref": self.git_ref,
            "subpath": self.subpath,
            "enabled": self.enabled,
            "sync_interval_seconds": self.sync_interval_seconds,
            "auto_publish": self.auto_publish,
            "status": self.status,
            "last_synced_at": (
                self.last_synced_at.isoformat() if self.last_synced_at else None
            ),
            "last_attempted_at": (
                self.last_attempted_at.isoformat()
                if self.last_attempted_at
                else None
            ),
            "last_commit_sha": self.last_commit_sha,
            "sync_error": self.sync_error,
            "discovered_skill_count": self.discovered_skill_count,
            "github_token_secret_ref": self.github_token_secret_ref,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "created_by": self.created_by,
        }

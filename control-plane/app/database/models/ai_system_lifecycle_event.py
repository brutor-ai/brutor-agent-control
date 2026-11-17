# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The lifecycle transition record.

The gate demands a named approver, and the console must answer "who moved
this system to active, and when?". Every transition writes a row that
carries two people on purpose:

* ``approved_by`` — the accountable approver the transition *declares*. It
  defaults to the admin who performed it and can name someone else when a
  person who is not logged in signed off (a risk owner on paper).
* ``recorded_by`` — the admin who actually clicked, taken from the session,
  never from the request body. An approver name is a declaration; the
  recorder is a fact.

Append-only: rows are never updated or deleted by the application, so the
history a lifecycle gate produced is the history an auditor reads.

The record also carries events that move no stage:
``agent.new_release`` is raised on every AI System an agent identity is a
member of, once per observed release (``subject_id`` = the release id; the
partial unique index makes the emitter idempotent). ``to_stage`` is NULL for
those, and a CHECK keeps it required for stage transitions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, String, Text, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

EVENT_STAGE_TRANSITION = "stage_transition"
#: Mirrors app.constants.agent_implementation.LIFECYCLE_AGENT_NEW_RELEASE.
EVENT_AGENT_NEW_RELEASE = "agent.new_release"
LIFECYCLE_EVENT_TYPES = (EVENT_STAGE_TRANSITION, EVENT_AGENT_NEW_RELEASE)


class AISystemLifecycleEvent(Base):
    __tablename__ = "ai_system_lifecycle_events"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    ai_system_id = Column(
        String(255), ForeignKey("resource_groups.id", ondelete="CASCADE"), nullable=False
    )
    event_type = Column(
        String(32),
        nullable=False,
        default=EVENT_STAGE_TRANSITION,
        server_default=EVENT_STAGE_TRANSITION,
        comment="stage_transition | agent.new_release",
    )
    from_stage = Column(String(32), nullable=True)
    #: NULL only for events that move no stage (CHECK below).
    to_stage = Column(String(32), nullable=True)
    subject_id = Column(
        String(64),
        nullable=True,
        comment="What the event is about (agent.new_release: the release id)",
    )
    details = Column(
        JSONB,
        nullable=True,
        comment="Event facts (agent.new_release: identity, name, version, build, trust)",
    )
    #: The accountable approver as declared; defaults to the recorder.
    approved_by = Column(String(255), nullable=True)
    #: The admin who performed the transition — from the session, unforgeable.
    recorded_by = Column(String(255), nullable=False)
    recorded_by_id = Column(String(255), nullable=True)
    successor_system_id = Column(String(255), nullable=True)
    no_successor = Column(Boolean, nullable=False, default=False, server_default="false")
    note = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("ix_ai_system_lifecycle_events_system", "tenant_id", "ai_system_id", "created_at"),
        CheckConstraint(
            "event_type IN ('stage_transition', 'agent.new_release')",
            name="ck_ai_system_lifecycle_events_type",
        ),
        CheckConstraint(
            "event_type <> 'stage_transition' OR to_stage IS NOT NULL",
            name="ck_ai_system_lifecycle_events_stage",
        ),
        Index(
            "uq_ai_system_lifecycle_events_subject",
            "tenant_id", "ai_system_id", "event_type", "subject_id",
            unique=True,
            postgresql_where=text("subject_id IS NOT NULL"),
            sqlite_where=text("subject_id IS NOT NULL"),
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ai_system_id": self.ai_system_id,
            "event_type": self.event_type or EVENT_STAGE_TRANSITION,
            "subject_id": self.subject_id,
            "details": self.details,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "approved_by": self.approved_by,
            "recorded_by": self.recorded_by,
            "successor_system_id": self.successor_system_id,
            "no_successor": bool(self.no_successor),
            "note": self.note,
            "at": self.created_at.isoformat() if self.created_at else None,
        }


def new_event_id() -> str:
    from ulid import ULID

    return f"lce-{ULID()}"


__all__ = [
    "AISystemLifecycleEvent",
    "EVENT_AGENT_NEW_RELEASE",
    "EVENT_STAGE_TRANSITION",
    "LIFECYCLE_EVENT_TYPES",
    "new_event_id",
]

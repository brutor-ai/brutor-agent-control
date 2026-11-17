# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""SchedulerRuntimeState — persisted pause/resume state per scheduler.

Schedulers are singletons named by code constant (`audit_export`,
`llm_retirement_warner`, …). Each background loop reads its own row
at the top of every tick:

  * Row absent     → run normally (the default).
  * paused = TRUE  → exit the tick before doing work.
  * paused = FALSE → run normally.

Pause / resume happens from the System → Background Tasks page. The
state transition writes the row AND fires a
`brutor:scheduler:pause` / `brutor:scheduler:resume` NOTIFY so
schedulers that LISTEN can react faster than their tick interval.

Insert-or-upsert on first interaction. Schedulers don't need to seed
their own rows at boot — the row appears the first time an operator
pauses them. A scheduler that's never been paused has no row, and
its loop falls through the `WHERE scheduler_name = ?` query without
short-circuiting.

See migration `a081_system_ops_console` for the design rationale.
"""

from sqlalchemy import Boolean, Column, DateTime, Index, String, Text
from sqlalchemy.sql import func

from app.database import Base


# Code-side scheduler names. Schedulers register themselves against
# this enum so the Background Tasks page has a fixed dropdown and the
# audit log carries a known set of values for the scheduler scope.
# Adding a new scheduler is a code change + one constant here.
SCHEDULER_AUDIT_EXPORT = "audit_export"
SCHEDULER_LLM_RETIREMENT_WARNER = "llm_retirement_warner"
SCHEDULER_SYSTEM_ALERTS_RESOLVER = "system_alerts_resolver"
SCHEDULER_BATCH_SUBMITTER = "batch_submitter"
SCHEDULER_BATCH_POLLER = "batch_poller"
SCHEDULER_KB_CONNECTOR_SYNC = "kb_connector_sync"
SCHEDULER_KB_UPLOADER = "kb_uploader"
SCHEDULER_SKILL_SOURCE_SYNC = "skill_source_sync"
SCHEDULER_USAGE_IMPORT = "usage_import"

KNOWN_SCHEDULERS = (
    SCHEDULER_AUDIT_EXPORT,
    SCHEDULER_LLM_RETIREMENT_WARNER,
    SCHEDULER_SYSTEM_ALERTS_RESOLVER,
    SCHEDULER_BATCH_SUBMITTER,
    SCHEDULER_BATCH_POLLER,
    SCHEDULER_KB_CONNECTOR_SYNC,
    SCHEDULER_KB_UPLOADER,
    SCHEDULER_SKILL_SOURCE_SYNC,
    SCHEDULER_USAGE_IMPORT,
)


class SchedulerRuntimeState(Base):
    """One row per scheduler that has been paused at least once."""

    __tablename__ = "scheduler_runtime_state"

    scheduler_name = Column(
        String(64),
        primary_key=True,
        comment=(
            "Code-side scheduler name. Must match one of the "
            "KNOWN_SCHEDULERS module constants — the Background "
            "Tasks UI dropdown reads from that list."
        ),
    )

    paused = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="When TRUE, the scheduler's next tick exits before doing work.",
    )

    paused_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When paused last transitioned to TRUE.",
    )

    paused_by = Column(
        String(255),
        nullable=True,
        comment="Admin id that paused.",
    )

    pause_reason = Column(
        Text,
        nullable=True,
        comment="Why the scheduler is paused, as the administrator who paused it said; NULL when not paused or no reason was given.",
    )

    resumed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When paused last transitioned to FALSE.",
    )

    resumed_by = Column(
        String(255),
        nullable=True,
        comment="Admin id that resumed.",
    )

    # Per-tick beacon. Each scheduler stamps this
    # column on every tick via `system_ops.beacon.write_beacon`. The
    # Background Tasks page derives liveness from this column: a
    # value within the past few minutes proves the loop is alive even
    # when the operator hasn't touched it via Trigger / Pause /
    # Resume. NULL = never observed; the state derivation falls back
    # to 'unknown'.
    last_beacon_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Wall-clock timestamp of the scheduler's most recent tick.",
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Beacon-staleness scan, the same index the migration creates.
        Index("ix_srs_last_beacon_at", "last_beacon_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<SchedulerRuntimeState(name={self.scheduler_name!r}, "
            f"paused={self.paused}, by={self.paused_by!r})>"
        )

    def to_dict(self) -> dict:
        return {
            "scheduler_name": self.scheduler_name,
            "paused": bool(self.paused),
            "paused_at": (
                self.paused_at.isoformat() if self.paused_at else None
            ),
            "paused_by": self.paused_by,
            "pause_reason": self.pause_reason,
            "resumed_at": (
                self.resumed_at.isoformat() if self.resumed_at else None
            ),
            "resumed_by": self.resumed_by,
            "last_beacon_at": (
                self.last_beacon_at.isoformat()
                if self.last_beacon_at
                else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

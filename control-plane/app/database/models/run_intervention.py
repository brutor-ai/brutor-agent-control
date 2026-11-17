# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Operator interventions on a single in-flight run.

Autonomy (`autonomy.rs`) is a real kill switch, but its unit is the agent or
the AI System, not the run. An operator watching one run misbehave could only
let it finish or suspend the whole system and take down every other run
inside it. On a shared copilot serving forty people that is not a control
anyone will use, so the honest answer to "can you stop it?" was "yes, by
stopping everything".

**Abort is autonomy scoped to a run.** Same verdict, same five enforcement
sites, same audit shape — the only new thing is knowing which run.

Why a table rather than only a Redis flag: an operator intervention **is
evidence**. It has to survive a Redis loss, appear in the run's audit trail
and in the Assurance Report, and be answerable months later. Same reasoning
as the fail-closed audit work.

Why it is written by the control plane and only read by the core: the CP owns
the schema, and the core is read-only with respect to configuration. The core
stamps two columns as it enforces (`first_blocked_at`, `steps_blocked`), which
is telemetry about the intervention rather than configuration of it.

The shape is deliberately general — `action` is a column, not a boolean — so
a future `throttle` or `require_approval_for_remainder` fits the same record
and the same propagation. Only `abort` is implemented; building the second
action before anyone asks for it costs a lot and modelling for it costs
nothing.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.resource_mixins import ResourceAuthorshipMixin

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

ACTION_ABORT = "abort"
ALLOWED_ACTIONS = frozenset({ACTION_ABORT})

#: `requested` — written, not yet observed by any gateway replica.
#: `effective` — a replica has refused at least one action on the run.
#: `closed`    — the run reached a terminal state; the record is history.
#:
#: The distinction matters to an operator: "I clicked stop" and "the run
#: actually stopped" are different claims, and a control that conflates them
#: is one you cannot trust in an incident.
STATUS_REQUESTED = "requested"
STATUS_EFFECTIVE = "effective"
STATUS_CLOSED = "closed"
ALLOWED_STATUSES = frozenset({STATUS_REQUESTED, STATUS_EFFECTIVE, STATUS_CLOSED})


class RunIntervention(Base, ResourceAuthorshipMixin):
    """One operator intervention on one run."""

    __tablename__ = "run_interventions"

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    root_task_id = Column(
        String(255),
        nullable=False,
        comment="The run this intervention acts on (proxy_logs chain root)",
    )

    action = Column(
        String(16),
        nullable=False,
        default=ACTION_ABORT,
        server_default=ACTION_ABORT,
        comment="abort — the only action today; the shape allows others",
    )

    reason = Column(
        Text,
        nullable=False,
        comment=(
            "Required. An intervention with no stated reason is not evidence, "
            "and 'why did this run stop?' is the first question asked afterwards."
        ),
    )

    requested_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    first_blocked_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "Stamped by the gateway the first time it actually refused an "
            "action. NULL means requested-but-never-bit: either the run had "
            "already finished, or the abort has not reached a replica yet."
        ),
    )

    steps_blocked = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="How many actions this intervention has refused",
    )

    status = Column(
        String(16),
        nullable=False,
        default=STATUS_REQUESTED,
        server_default=STATUS_REQUESTED,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        # One open intervention per run. A second abort on the same run is
        # the same instruction, not a new one.
        UniqueConstraint("tenant_id", "root_task_id", name="uq_run_interventions_run"),
        Index("ix_run_interventions_tenant_status", "tenant_id", "status"),
    )

    @validates("action")
    def _validate_action(self, key, value):
        if value not in ALLOWED_ACTIONS:
            raise ValueError(
                f"action must be one of {sorted(ALLOWED_ACTIONS)}, got {value!r}"
            )
        return value

    @validates("status")
    def _validate_status(self, key, value):
        if value not in ALLOWED_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(ALLOWED_STATUSES)}, got {value!r}"
            )
        return value

    @validates("reason")
    def _validate_reason(self, key, value):
        if not value or not str(value).strip():
            raise ValueError("reason is required — an intervention with no reason is not evidence")
        return value

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "root_task_id": self.root_task_id,
            "action": self.action,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "first_blocked_at": (
                self.first_blocked_at.isoformat() if self.first_blocked_at else None
            ),
            "steps_blocked": self.steps_blocked or 0,
            "status": self.status,
            # "Did it actually bite?" is the question an operator asks next,
            # so answer it rather than making them infer it.
            "took_effect": self.first_blocked_at is not None,
        }
        data.update(self.authorship_dict())
        return data


__all__ = [
    "RunIntervention",
    "ACTION_ABORT",
    "ALLOWED_ACTIONS",
    "STATUS_REQUESTED",
    "STATUS_EFFECTIVE",
    "STATUS_CLOSED",
    "ALLOWED_STATUSES",
]

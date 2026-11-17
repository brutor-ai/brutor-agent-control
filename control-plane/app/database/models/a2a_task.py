# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
SQLAlchemy models for the A2A v1.0 stateful task surface.

The tables are declared in Alembic migration `a013_a2a_tasks_push_configs` — see its docstring
for the full schema + rationale. These models exist so Python callers
(tests, admin tools, Mission Control queries) can read task rows
through the ORM without re-declaring the shape.

The Rust proxy writes rows directly via sqlx (no ORM), so the models
here are read-mostly on the Python side. The denormalised `state` +
`status_timestamp` columns are the only fields an ORM writer would
need to keep in sync with `task_json`; see the helper on
[`A2aTask.sync_from_task_json`].
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base


class A2aTask(Base):
    """One row per A2A v1.0 Task."""

    __tablename__ = "a2a_tasks"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    context_id = Column(String(64), nullable=True)
    state = Column(String(32), nullable=False)
    status_timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    task_json = Column(JSONB, nullable=False)
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
        Index("ix_a2a_tasks_tenant_status_ts", "tenant_id", "status_timestamp"),
        Index("ix_a2a_tasks_tenant_context", "tenant_id", "context_id", "status_timestamp"),
        Index("ix_a2a_tasks_tenant_state", "tenant_id", "state", "status_timestamp"),
    )

    def sync_from_task_json(self) -> None:
        """Keep `state` + `status_timestamp` + `context_id` in lockstep
        with `task_json`. Call after mutating the JSON blob.
        """
        tj = self.task_json or {}
        status = tj.get("status") or {}
        new_state = status.get("state")
        if isinstance(new_state, str):
            self.state = new_state
        # v1.0 spec mandates millisecond-precision ISO 8601 UTC.
        ts_raw = status.get("timestamp")
        if isinstance(ts_raw, str):
            try:
                # Parse and normalise — strip trailing Z, Python's
                # fromisoformat accepts "+00:00" not "Z".
                normalised = ts_raw.replace("Z", "+00:00")
                self.status_timestamp = datetime.fromisoformat(normalised)
            except ValueError:
                # Don't fail the whole write if a partner emits a
                # malformed timestamp — the DB timestamptz default
                # handles the absence case.
                pass
        ctx = tj.get("contextId") or tj.get("context_id")
        if ctx is not None:
            self.context_id = ctx

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "resource_group_id": self.resource_group_id,
            "context_id": self.context_id,
            "state": self.state,
            "status_timestamp": (
                self.status_timestamp.isoformat() if self.status_timestamp else None
            ),
            "task_json": self.task_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class A2aPushNotificationConfig(Base):
    """One row per (task, push-notification-config) pair."""

    __tablename__ = "a2a_push_notification_configs"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id = Column(
        String(64),
        ForeignKey("a2a_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    config_json = Column(JSON, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_a2a_push_configs_task", "task_id"),
        Index("ix_a2a_push_configs_tenant", "tenant_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "config_json": self.config_json,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

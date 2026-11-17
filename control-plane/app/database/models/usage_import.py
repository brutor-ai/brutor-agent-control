# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Traffic Data Import — operational child tables.

The import *configuration* lives on the provider catalog/instance
(``llm_provider_catalog.import_*`` + ``llm_providers.import_*``). These two
tables hold only the operational state that doesn't belong on a config row:

  * ``usage_import_checkpoints`` — incremental cursor per provider so re-syncs
    are incremental, not full-history.
  * ``usage_import_run_logs`` — per-run audit (window, counts, $, errors),
    surfaced on the System → Background Tasks page and the Traffic Data Import
    UI's run-history table.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    text,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from app.database import Base

# Run trigger + status vocabularies (kept here so the API/UI agree).
IMPORT_TRIGGER_SCHEDULED = "scheduled"
IMPORT_TRIGGER_MANUAL = "manual"
IMPORT_TRIGGER_BACKFILL = "backfill"
IMPORT_TRIGGERS = (
    IMPORT_TRIGGER_SCHEDULED,
    IMPORT_TRIGGER_MANUAL,
    IMPORT_TRIGGER_BACKFILL,
)

IMPORT_STATUS_RUNNING = "running"
IMPORT_STATUS_SUCCESS = "success"
IMPORT_STATUS_FAILED = "failed"
IMPORT_STATUS_PARTIAL = "partial"
IMPORT_STATUSES = (
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SUCCESS,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PARTIAL,
)


class UsageImportCheckpoint(Base):
    """One incremental cursor per (configured) provider."""

    __tablename__ = "usage_import_checkpoints"

    provider_id = Column(
        String(64),
        ForeignKey("llm_providers.id", ondelete="CASCADE"),
        primary_key=True,
        comment="The configured provider this cursor belongs to",
    )
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        # NB: no `index=True` — that would auto-name the index
        # `ix_usage_import_checkpoints_tenant_id`, but migration `a095_traffic_data_import` named
        # it `ix_usage_import_checkpoints_tenant`. Declared explicitly in
        # __table_args__ below so create_all and the migration agree.
    )
    last_imported_through = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Most recent fully-imported bucket end (the resume point)",
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_usage_import_checkpoints_tenant", "tenant_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "tenant_id": self.tenant_id,
            "last_imported_through": (
                self.last_imported_through.isoformat()
                if self.last_imported_through
                else None
            ),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UsageImportRunLog(Base):
    """Per-run history for a provider's traffic-data-import."""

    __tablename__ = "usage_import_run_logs"

    id = Column(String(64), primary_key=True)  # "impru-<ULID>"
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_id = Column(
        String(64),
        ForeignKey("llm_providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    import_api = Column(String(64), nullable=True)
    trigger = Column(String(16), nullable=False, default=IMPORT_TRIGGER_SCHEDULED)
    status = Column(String(16), nullable=False, default=IMPORT_STATUS_RUNNING)

    window_start = Column(DateTime(timezone=True), nullable=True)
    window_end = Column(DateTime(timezone=True), nullable=True)

    rows_ingested = Column(Integer, nullable=False, default=0, server_default="0")
    users_created = Column(Integer, nullable=False, default=0, server_default="0")
    groups_created = Column(Integer, nullable=False, default=0, server_default="0")
    models_created = Column(Integer, nullable=False, default=0, server_default="0")
    memberships_created = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    model_links_created = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_imported = Column(Float, nullable=False, default=0.0, server_default="0")
    currency = Column(String(10), nullable=False, default="USD", server_default="USD")

    error_message = Column(Text, nullable=True)

    started_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_usage_import_runs_provider",
            "tenant_id",
            "provider_id",
            "started_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "provider_id": self.provider_id,
            "import_api": self.import_api,
            "trigger": self.trigger,
            "status": self.status,
            "window_start": (
                self.window_start.isoformat() if self.window_start else None
            ),
            "window_end": (
                self.window_end.isoformat() if self.window_end else None
            ),
            "rows_ingested": self.rows_ingested,
            "users_created": self.users_created,
            "groups_created": self.groups_created,
            "models_created": self.models_created,
            "memberships_created": self.memberships_created,
            "model_links_created": self.model_links_created,
            "cost_imported": self.cost_imported,
            "currency": self.currency,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
        }

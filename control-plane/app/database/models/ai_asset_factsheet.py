# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI Asset Factsheet snapshot

A point-in-time snapshot of an AI asset's auto-assembled factsheet (the
per-asset governance dossier built from runtime evidence in proxy_logs +
config + the ai_assets row). The live factsheet is computed on demand by
`FactsheetService.build_factsheet`; this table persists immutable
snapshots so an operator can capture "what the dossier said on
a given day" for an audit, a board deck, or an EU AI Act technical-
documentation pack.

Append-only — like `mcp_capability_snapshots`. One row per
capture; the whole dossier lives in `snapshot_json`. Snapshots are never
mutated; a re-capture writes a new row.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base


# Trigger values stored in the `trigger` column. String literals so an
# operator reading the row sees the exact reason without a code lookup.
TRIGGER_MANUAL = "manual"          # operator clicked "capture snapshot"
TRIGGER_SCHEDULED = "scheduled"    # periodic capture job
TRIGGER_EXPORT = "export"          # captured as part of an export run


class AiAssetFactsheet(Base):
    """One immutable factsheet snapshot for an AI asset."""

    __tablename__ = "ai_asset_factsheets"

    id = Column(String(255), primary_key=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_id = Column(
        String(255),
        ForeignKey("ai_assets.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The evidence window the dossier was computed over (hours; 0 =
    # all-time). Stored so a snapshot is self-describing.
    period_hours = Column(Integer, nullable=False)

    # The full assembled dossier (see FactsheetService.build_factsheet).
    snapshot_json = Column(JSONB, nullable=False)

    # Why this snapshot was taken.
    trigger = Column(String(32), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_ai_asset_factsheets_tenant_asset_created",
            "tenant_id", "asset_id", "created_at",
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Dictionary for API responses. Every column appears here — the
        Pydantic response model declares the same keys (the strip-
        undeclared-fields gotcha)."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "asset_id": self.asset_id,
            "period_hours": self.period_hours,
            "snapshot_json": self.snapshot_json or {},
            "trigger": self.trigger,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<AiAssetFactsheet(id={self.id!r}, asset={self.asset_id!r}, "
            f"period_hours={self.period_hours!r}, trigger={self.trigger!r})>"
        )


__all__ = [
    "AiAssetFactsheet",
    "TRIGGER_MANUAL",
    "TRIGGER_SCHEDULED",
    "TRIGGER_EXPORT",
]

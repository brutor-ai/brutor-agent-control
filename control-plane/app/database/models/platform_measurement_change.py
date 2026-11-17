# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Platform measurement changes applied on this installation

One row per entry of the versioned registry
(`app/constants/measurement_changes.py`) that the control plane has applied:
when (the moment the changed measurement is taken to begin here), by which
release, and how many `measurement_change` baseline epochs it wrote. The row
is also the claim — an entry with a row never applies again, whatever
restarts, workers or instances follow. Installation-wide, not per tenant: a
measurement is how the platform code measures, for everyone.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base


class PlatformMeasurementChange(Base):
    __tablename__ = "platform_measurement_changes"

    id = Column(
        String(64),
        primary_key=True,
        comment="Registry entry id (app/constants/measurement_changes.py)",
    )
    metric_keys = Column(JSONB, nullable=False)
    bases = Column(JSONB, nullable=False)
    reason = Column(Text, nullable=False)
    introduced_in_version = Column(String(32), nullable=False)
    platform_version = Column(
        String(64),
        nullable=True,
        comment="BRUTOR_VERSION of the control plane that applied it",
    )
    applied_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    epochs_written = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        {"comment": "Measurement changes applied on this installation (once each, ever)"},
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "metric_keys": list(self.metric_keys or []),
            "bases": list(self.bases or []),
            "reason": self.reason,
            "introduced_in_version": self.introduced_in_version,
            "platform_version": self.platform_version,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "epochs_written": self.epochs_written,
        }


__all__ = ["PlatformMeasurementChange"]

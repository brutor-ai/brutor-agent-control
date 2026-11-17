# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Compliance reports — periodic per-framework evidence reports

One row per (tenant, framework, system-or-estate, cadence, period). `rows`
holds the evaluated obligation statements, `summary` the counts; the report
is sealed as an `ai.brutor.report` record (`record_id`) and packaged as an
evidence bundle (`bundle_id`) whose digest a reviewer can countersign
(`countersignatures`). Free text never enters the record — only the
payload digest does.

Uniqueness is nulls-not-distinct on `ai_system_id` (COALESCE expression
index): an estate-wide report (NULL system) for a period exists once.

Written by the control plane.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Column, Date, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.evidence_columns import check_vocab, created_at_column, iso

REPORT_CADENCES = frozenset({"daily", "weekly", "monthly"})

REPORT_STATUS_GENERATING = "generating"
REPORT_STATUS_READY = "ready"
REPORT_STATUS_FAILED = "failed"
REPORT_STATUSES = frozenset({REPORT_STATUS_GENERATING, REPORT_STATUS_READY, REPORT_STATUS_FAILED})


class ComplianceReport(Base):
    """One generated compliance report (CP-written)."""

    __tablename__ = "compliance_reports"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'rpt-'")
    tenant_id = Column(String(255), nullable=False)
    framework_id = Column(String(64), nullable=False)
    ai_system_id = Column(String(255), nullable=True, comment="NULL = estate-wide")
    cadence = Column(String(16), nullable=False, comment="daily | weekly | monthly")
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    catalog_version = Column(String(32), nullable=False)
    status = Column(
        String(16),
        nullable=False,
        default=REPORT_STATUS_GENERATING,
        server_default="generating",
    )
    summary = Column(JSONB, nullable=False, default=dict, server_default="{}")
    rows = Column(JSONB, nullable=False, default=list, server_default="[]")
    error = Column(Text, nullable=True)
    record_id = Column(String(64), nullable=True, comment="ai.brutor.report record; NULL = not sealed")
    bundle_id = Column(String(64), nullable=True)
    countersignatures = Column(JSONB, nullable=False, default=list, server_default="[]")
    generated_by = Column(String(255), nullable=True)
    created_at = created_at_column()

    __table_args__ = (
        Index(
            "uq_compliance_reports_period",
            "tenant_id",
            "framework_id",
            text("COALESCE(ai_system_id, '')"),
            "cadence",
            "period_start",
            unique=True,
        ),
        Index("ix_compliance_reports_tenant", "tenant_id", "created_at"),
    )

    @validates("cadence")
    def _validate_cadence(self, _key, value):
        return check_vocab("cadence", value, REPORT_CADENCES)

    @validates("status")
    def _validate_status(self, _key, value):
        return check_vocab("status", value, REPORT_STATUSES)

    def summary_dict(self) -> Dict[str, Any]:
        """Everything but `rows` — for lists."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "framework_id": self.framework_id,
            "ai_system_id": self.ai_system_id,
            "cadence": self.cadence,
            "period_start": iso(self.period_start),
            "period_end": iso(self.period_end),
            "catalog_version": self.catalog_version,
            "status": self.status,
            "summary": self.summary or {},
            "error": self.error,
            "record_id": self.record_id,
            "bundle_id": self.bundle_id,
            "countersignatures": self.countersignatures or [],
            "generated_by": self.generated_by,
            "created_at": iso(self.created_at),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary_dict()
        data["rows"] = self.rows or []
        return data


__all__ = [
    "ComplianceReport",
    "REPORT_CADENCES",
    "REPORT_STATUSES",
    "REPORT_STATUS_GENERATING",
    "REPORT_STATUS_READY",
    "REPORT_STATUS_FAILED",
]

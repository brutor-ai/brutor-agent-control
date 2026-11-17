# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
ISO/IEC 42001:2023 — AI Management System (AIMS) support.

Two tables:
  * `iso42001_controls`          — the Annex A reference control catalog. STATIC
                                    reference data, identical for every tenant,
                                    seeded from `seed_iso42001_controls.py`.
  * `iso42001_control_coverage`  — per-tenant implementation status for each
                                    control (the "Check" evidence for Clause 9):
                                    status, accountable owner, evidence refs,
                                    review dates.

Note on terminology: control titles/summaries stored here are SHORT paraphrased
navigational labels — the authoritative normative text is the copyrighted
ISO/IEC 42001:2023 standard and is NOT reproduced. `brutor_note` is Brutor's own
implementation guidance, not ISO text.
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
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.database import Base

# --- Coverage status vocabulary -------------------------------------------
# Audit/conformity-recognised implementation states (NOT an ISO-defined term
# set — ISO speaks of conformity/nonconformity; these are the widely used
# control-implementation states auditors expect on a Statement of Applicability).
COVERAGE_IMPLEMENTED = "implemented"
COVERAGE_PARTIALLY_IMPLEMENTED = "partially_implemented"
COVERAGE_NOT_IMPLEMENTED = "not_implemented"
COVERAGE_NOT_APPLICABLE = "not_applicable"

COVERAGE_STATUSES = frozenset(
    {
        COVERAGE_IMPLEMENTED,
        COVERAGE_PARTIALLY_IMPLEMENTED,
        COVERAGE_NOT_IMPLEMENTED,
        COVERAGE_NOT_APPLICABLE,
    }
)

# Statuses that count toward the readiness percentage (numerator).
COVERAGE_SATISFIED = frozenset({COVERAGE_IMPLEMENTED, COVERAGE_NOT_APPLICABLE})


class Iso42001Control(Base):
    """One ISO/IEC 42001:2023 Annex A reference control (or management-system
    clause reference). Static, seeded reference data shared by all tenants."""

    __tablename__ = "iso42001_controls"

    # e.g. "A.5.2", "A.6.2.6". Stable identifier used everywhere it's referenced.
    control_id = Column(String(16), primary_key=True)
    # Parent clause/group, e.g. "A.5" ("Assessing impacts of AI systems").
    clause = Column(String(16), nullable=False)
    category = Column(String(255), nullable=False)
    # Short paraphrased title (NOT verbatim ISO text).
    title = Column(String(255), nullable=False)
    # Short paraphrased summary of the control's intent (NOT verbatim ISO text).
    summary = Column(Text, nullable=True)
    # Brutor's own note on how the platform helps implement this control —
    # this is what powers the "talk track" in the coverage dashboard.
    brutor_note = Column(Text, nullable=True)
    # Whether Brutor can supply evidence for this control out of the box
    # (drives the pre-seeded coverage defaults): "covered" | "partial" | "org".
    # "org" = a purely organisational control Brutor can't implement for you.
    brutor_capability = Column(String(16), nullable=False, server_default="org")
    sort_order = Column(Integer, nullable=False, server_default="0")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "control_id": self.control_id,
            "clause": self.clause,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "brutor_note": self.brutor_note,
            "brutor_capability": self.brutor_capability,
            "sort_order": self.sort_order,
        }


class Iso42001ControlCoverage(Base):
    """Per-tenant implementation status + evidence for one Annex A control."""

    __tablename__ = "iso42001_control_coverage"

    id = Column(String(255), primary_key=True)  # iso42cov-<ULID>
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    control_id = Column(
        String(16),
        ForeignKey("iso42001_controls.control_id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(
        String(32), nullable=False, server_default=COVERAGE_NOT_IMPLEMENTED
    )
    # Accountable owner (Annex A.3.2). Free-text / user ref.
    owner = Column(String(255), nullable=True)
    # Evidence references: list of {kind, ref, label} pointing at registry rows,
    # policies, audit-log queries, guardrail configs, etc.
    evidence = Column(JSONB, nullable=True)
    notes = Column(Text, nullable=True)
    last_reviewed = Column(DateTime(timezone=True), nullable=True)
    next_review = Column(DateTime(timezone=True), nullable=True)
    updated_by = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "control_id", name="uix_iso42001_coverage_tenant_control"
        ),
        # The same index the migration creates, so create_all builds it too.
        Index("ix_iso42001_coverage_tenant", "tenant_id"),
    )

    @validates("status")
    def _validate_status(self, _key: str, value: str) -> str:
        if value not in COVERAGE_STATUSES:
            raise ValueError(
                f"Invalid ISO 42001 coverage status {value!r}; expected one of "
                f"{sorted(COVERAGE_STATUSES)}"
            )
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "control_id": self.control_id,
            "status": self.status,
            "owner": self.owner,
            "evidence": self.evidence,
            "notes": self.notes,
            "last_reviewed": self.last_reviewed.isoformat()
            if self.last_reviewed
            else None,
            "next_review": self.next_review.isoformat() if self.next_review else None,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

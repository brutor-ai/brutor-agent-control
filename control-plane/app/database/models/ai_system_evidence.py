# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System evidence attachments

An AI System's assurance evidence is mostly **platform-generated**: runs,
drift findings, contracts, replay results. This table holds the other
kind — evidence produced *outside* the platform that belongs on the same
record: an impact assessment (EU AI Act Article 27 FRIA, a DPIA), an
evaluation report, a red-team or penetration-test report.

## A registry of artifacts, not a document store

A row records that an artifact exists, where it lives, and how to verify
it (`sha256`) — the document itself stays wherever the organisation keeps
documents (SharePoint, the GRC tool, a vendor portal). Compliance teams
already have a system of record for documents; what they lack is the link
between *that* document and *this* AI System's lifecycle gate and
assurance report. Duplicating the bytes here would create a second copy
to govern; recording the hash makes the pointer verifiable without one.

## Why `independent` is a first-class field

Evidence produced by a party separate from the team that built or
operates the system carries more weight as the stakes rise — that
distinction is exactly what an auditor asks about first, so it is a
column, not a convention buried in `notes`. The assurance report states
plainly when everything attached is self-produced.

## Expiry is a statement, not housekeeping

`valid_until` lets an assessor bound their own claim ("this assessment
holds for twelve months"). An expired impact assessment does not satisfy
the lifecycle gate: evidence that has aged out is a different fact from
evidence that exists.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.resource_mixins import ResourceAuthorshipMixin

# ---------------------------------------------------------------------------
# Evidence kinds
# ---------------------------------------------------------------------------

EVIDENCE_IMPACT_ASSESSMENT = "impact_assessment"
EVIDENCE_EVAL_REPORT = "eval_report"
EVIDENCE_RED_TEAM_REPORT = "red_team_report"
EVIDENCE_PEN_TEST_REPORT = "pen_test_report"
#: Pointers the obligation catalogs cite (EU AI Act Art 9
#: risk management, Art 10 data governance; GDPR Art 35 DPIA).
EVIDENCE_RISK_ASSESSMENT = "risk_assessment"
EVIDENCE_DATA_GOVERNANCE = "data_governance"
EVIDENCE_DPIA = "dpia"
#: EU AI Act Art 25(4) — the written agreement a system
#: relies on from the supplier of one of its dependencies (an external
#: agent, or a shared service owned by another legal entity). Attached to
#: the dependency edge (`dependency_id`), pinned by hash like every pointer.
EVIDENCE_SUPPLIER_INFORMATION = "supplier_information"
EVIDENCE_OTHER = "other"

EVIDENCE_KINDS = frozenset({
    EVIDENCE_IMPACT_ASSESSMENT,
    EVIDENCE_EVAL_REPORT,
    EVIDENCE_RED_TEAM_REPORT,
    EVIDENCE_PEN_TEST_REPORT,
    EVIDENCE_RISK_ASSESSMENT,
    EVIDENCE_DATA_GOVERNANCE,
    EVIDENCE_DPIA,
    EVIDENCE_SUPPLIER_INFORMATION,
    EVIDENCE_OTHER,
})

#: Kinds that are about ONE dependency edge and must name it.
EDGE_EVIDENCE_KINDS = frozenset({EVIDENCE_SUPPLIER_INFORMATION})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AISystemEvidence(Base, ResourceAuthorshipMixin):
    """One external evidence artifact attached to an AI System."""

    __tablename__ = "ai_system_evidence"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    ai_system_id = Column(String(255), nullable=False, index=True)

    kind = Column(String(40), nullable=False)
    title = Column(String(500), nullable=False)
    notes = Column(Text, nullable=True)

    #: Where the artifact lives — a URL into the org's document system.
    uri = Column(Text, nullable=True)
    #: SHA-256 of the artifact bytes, lowercase hex. Lets a reviewer verify
    #: that the document at `uri` is the one this row vouches for.
    sha256 = Column(String(64), nullable=True)

    #: Who produced the artifact — a person, team or firm. Free text: the
    #: assessor is frequently not a platform user.
    assessor = Column(String(255), nullable=True)
    #: Produced by a party separate from the team that built or operates
    #: the system. Nullable on purpose: unknown is not the same claim as
    #: "self-assessed", and defaulting either way would put words in the
    #: uploader's mouth.
    independent = Column(Boolean, nullable=True)

    #: When the artifact itself was produced (not when it was attached).
    produced_at = Column(DateTime(timezone=True), nullable=True)
    #: The assessor's own bound on how long the claim holds. Null means
    #: no expiry was stated.
    valid_until = Column(DateTime(timezone=True), nullable=True)

    #: The dependency edge this pointer is about;
    #: set for `supplier_information` (required), NULL for every other kind.
    #: SET NULL when the edge is removed: the agreement existed, it just no
    #: longer backs a declared dependency.
    dependency_id = Column(
        String(64),
        ForeignKey(
            "ai_system_dependencies.id",
            name="fk_ai_system_evidence_dependency",
            ondelete="SET NULL",
        ),
        nullable=True,
        comment="The dependency edge a supplier_information pointer is "
        "attached to (EU AI Act Art 25(4) supplier agreement)",
    )

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_ai_system_evidence_system", "tenant_id", "ai_system_id"),
        Index("ix_ai_system_evidence_kind", "tenant_id", "ai_system_id", "kind"),
        Index("ix_ai_system_evidence_dependency", "dependency_id"),
    )

    @validates("kind")
    def _validate_kind(self, _key: str, value: str) -> str:
        if value not in EVIDENCE_KINDS:
            raise ValueError(
                f"kind must be one of {sorted(EVIDENCE_KINDS)}, got {value!r}"
            )
        return value

    @validates("sha256")
    def _validate_sha256(self, _key: str, value):
        if value is None:
            return None
        normalized = value.strip().lower()
        if not _SHA256_RE.match(normalized):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return normalized

    @property
    def is_expired(self) -> bool:
        if self.valid_until is None:
            return False
        expiry = self.valid_until
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry < datetime.now(timezone.utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "kind": self.kind,
            "title": self.title,
            "notes": self.notes,
            "uri": self.uri,
            "sha256": self.sha256,
            "assessor": self.assessor,
            "independent": self.independent,
            "produced_at": self.produced_at.isoformat() if self.produced_at else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "dependency_id": self.dependency_id,
            "is_expired": self.is_expired,
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "AISystemEvidence",
    "EVIDENCE_KINDS",
    "EVIDENCE_IMPACT_ASSESSMENT",
    "EVIDENCE_EVAL_REPORT",
    "EVIDENCE_RED_TEAM_REPORT",
    "EVIDENCE_PEN_TEST_REPORT",
    "EVIDENCE_RISK_ASSESSMENT",
    "EVIDENCE_DATA_GOVERNANCE",
    "EVIDENCE_DPIA",
    "EVIDENCE_SUPPLIER_INFORMATION",
    "EDGE_EVIDENCE_KINDS",
    "EVIDENCE_OTHER",
]

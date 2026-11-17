# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Disclosure records — what was handed to whom

Every bundle handed to an auditor, a regulator or a counterparty leaves a
record: the audience, the selector that chose the records, whether the
selection was `contiguous` (every record in a window — a verifier can
check nothing was left out) or `producer_selected` (a hand-picked set — the
verifier is told so), which fields were suppressed, and the bundle digest.
The record is itself sealed into the tenant log (`seq`), so the act of
disclosing is as verifiable as what was disclosed.

The control plane reads these rows; the core writes them with bundles.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import BigInteger, Column, DateTime, Index, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base

COMPLETENESS_CONTIGUOUS = "contiguous"
COMPLETENESS_PRODUCER_SELECTED = "producer_selected"
COMPLETENESS_VALUES = frozenset({COMPLETENESS_CONTIGUOUS, COMPLETENESS_PRODUCER_SELECTED})


class DisclosureRecord(Base):
    """One disclosure of evidence to one audience."""

    __tablename__ = "disclosure_records"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    log_id = Column(String(64), nullable=False)
    seq = Column(BigInteger, nullable=True, comment="Leaf index once sealed into the tenant log")
    audience = Column(String(255), nullable=False, comment="Who received it — a label, never an identity")
    selector = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="The query that chose the records (system, window, verdicts, run …)",
    )
    completeness = Column(String(24), nullable=False, comment="contiguous | producer_selected")
    disclosed_record_ids = Column(JSONB, nullable=False, default=list, server_default="[]")
    suppressed_fields = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="Record members redacted from the bundle, by path",
    )
    bundle_digest = Column(String(64), nullable=True, comment="SHA-256 of the bundle bytes (hex)")
    statement = Column(LargeBinary, nullable=True, comment="Signed Statement over this record")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    created_by = Column(String(255), nullable=True)

    __table_args__ = (Index("ix_disclosure_records_tenant", "tenant_id", "created_at"),)

    @validates("completeness")
    def _validate_completeness(self, _key, value):
        if value not in COMPLETENESS_VALUES:
            raise ValueError(
                f"completeness must be one of {sorted(COMPLETENESS_VALUES)}, got {value!r}"
            )
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "log_id": self.log_id,
            "seq": self.seq,
            "audience": self.audience,
            "selector": self.selector or {},
            "completeness": self.completeness,
            "disclosed_record_ids": self.disclosed_record_ids or [],
            "disclosed_count": len(self.disclosed_record_ids or []),
            "suppressed_fields": self.suppressed_fields or [],
            "bundle_digest": self.bundle_digest,
            "statement_b64": (
                base64.b64encode(self.statement).decode("ascii") if self.statement else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
        }


__all__ = [
    "DisclosureRecord",
    "COMPLETENESS_VALUES",
    "COMPLETENESS_CONTIGUOUS",
    "COMPLETENESS_PRODUCER_SELECTED",
]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Data-subject erasure events.

A GDPR erasure request is itself something an auditor asks about: who
asked, when, what was removed. The request is recorded here as an
append-only row carrying a fingerprint of the subject (never the
identifiers themselves — that would re-create what was just erased) and
the counts of what each table gave up.

What erasure removes and what it keeps is a deliberate line, documented in
the data-handling page (https://docs.brutor.ai/governance/data-handling/): content and direct identifiers go; pseudonymous keys
that the audit hash chain covers (`subject_id`, `actor_id`) stay, and the
record that linked them to a person is deleted, so the key no longer
identifies anyone.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class DataErasureEvent(Base):
    __tablename__ = "data_erasure_events"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False)
    subject_fingerprint = Column(
        String(64),
        nullable=False,
        comment="SHA-256 over the sorted identifiers the request named; never the identifiers",
    )
    subject_kinds = Column(
        JSONB,
        nullable=False,
        comment='Which identifier kinds were supplied, e.g. ["end_user_id", "email"]',
    )
    requested_by = Column(String(255), nullable=False)
    requested_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    dry_run = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    counts = Column(JSONB, nullable=False, comment="Per-table counts of rows scrubbed or deleted")

    __table_args__ = (
        Index("ix_data_erasure_events_tenant", "tenant_id", "requested_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "subject_fingerprint": self.subject_fingerprint,
            "subject_kinds": self.subject_kinds,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at.isoformat() if self.requested_at else None,
            "dry_run": self.dry_run,
            "counts": self.counts,
        }

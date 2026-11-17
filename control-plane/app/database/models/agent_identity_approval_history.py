# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Approval history of an agent identity

`agent_identities.approved_versions` is the approver's CURRENT declaration.
`agent.unapproved_release` must judge a run by the declaration in effect
when the run STARTED, not by today's: replacing ['0.1.0'] with ['0.2.0']
does not make 0.1.0 — approved the whole time it ran — an unapproved
release. This table keeps every declared value with its validity period
[valid_from, valid_to) (half-open; `valid_to` NULL = the current one).

Written ONLY by `app.services.agent_implementation.record_approved_versions`
(static guard: `tests/test_agent_approval_history.py`), which closes the
current row and opens a new one when — and only when — the value changes.
The `source` values `backfill_contract` (from the contract closures) and
`backfill_live` (from the live declaration) mark rows that were derived
rather than recorded when the value changed.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base

SOURCE_DECLARATION = "declaration"
SOURCE_BACKFILL_CONTRACT = "backfill_contract"
SOURCE_BACKFILL_LIVE = "backfill_live"
APPROVAL_HISTORY_SOURCES = (SOURCE_DECLARATION, SOURCE_BACKFILL_CONTRACT, SOURCE_BACKFILL_LIVE)


class AgentIdentityApprovalHistory(Base):
    """One declared `approved_versions` value and the period it was in effect."""

    __tablename__ = "agent_identity_approval_history"

    id = Column(String(64), primary_key=True)  # aah-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    agent_id = Column(
        String(255), ForeignKey("agent_identities.id", ondelete="CASCADE"), nullable=False
    )
    approved_versions = Column(
        JSONB,
        nullable=False,
        comment="Sorted approved version patterns in effect; [] = observe only",
    )
    valid_from = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    valid_to = Column(DateTime(timezone=True), nullable=True, comment="NULL = the current declaration")
    changed_by = Column(String(255), nullable=True)
    source = Column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "source IN ('declaration', 'backfill_contract', 'backfill_live')",
            name="ck_agent_identity_approval_history_source",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_agent_identity_approval_history_period",
        ),
        Index("ix_agent_identity_approval_history_agent", "tenant_id", "agent_id", "valid_from"),
        Index(
            "uq_agent_identity_approval_history_current",
            "agent_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
        {"comment": "approved_versions with their validity periods"},
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "approved_versions": list(self.approved_versions or []),
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "changed_by": self.changed_by,
            "source": self.source,
        }


__all__ = [
    "APPROVAL_HISTORY_SOURCES",
    "AgentIdentityApprovalHistory",
    "SOURCE_BACKFILL_CONTRACT",
    "SOURCE_BACKFILL_LIVE",
    "SOURCE_DECLARATION",
]

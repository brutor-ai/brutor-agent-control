# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Declared deletions from the audit hash chain.

`proxy_logs` rows carry a dense per-writer sequence. Deleting a row
leaves a hole, and the verifier reports a hole as `gap` — "rows were
deleted" — because it cannot tell a retention prune from tampering. This
table is how a prune declares itself: the exact sequence numbers removed,
and for every contiguous run the `row_hash` of its last row, which the next
surviving row's `prev_hash` must still equal. With that on record the
verifier can return `pruned` — rows are missing, every one of them was
declared, and the survivors still link to what was declared — and keep
`gap` for the deletions nobody owned up to.

A chain interleaves every tenant the writer process served, so a
tenant-scoped prune produces many short runs; `runs` stores them
compactly. Rows are append-only: a prune record is itself evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

REASON_RETENTION = "retention"
REASON_ERASURE = "erasure"
REASON_MANUAL = "manual"
REASONS = (REASON_RETENTION, REASON_ERASURE, REASON_MANUAL)


class AuditChainPrune(Base):
    __tablename__ = "audit_chain_prunes"

    id = Column(String(64), primary_key=True)
    chain_id = Column(String(64), nullable=False)
    tenant_id = Column(String(255), nullable=True, comment="Tenant whose rows were removed; NULL for a system-wide prune")
    seq_min = Column(BigInteger, nullable=False)
    seq_max = Column(BigInteger, nullable=False)
    row_count = Column(Integer, nullable=False)
    # [{"from": 120, "to": 124, "tail_hash": "<row_hash of seq 124>"}, ...]
    # Contiguous removed runs in ascending order. The verifier checks the
    # survivor after each run links to tail_hash.
    runs = Column(JSONB, nullable=False)
    reason = Column(String(16), nullable=False)
    actor = Column(String(255), nullable=True, comment="Admin id for manual/erasure; 'retention-sweeper' otherwise")
    pruned_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_audit_chain_prunes_chain_range", "chain_id", "seq_min", "seq_max"),
        Index("ix_audit_chain_prunes_tenant", "tenant_id", "pruned_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chain_id": self.chain_id,
            "tenant_id": self.tenant_id,
            "seq_min": self.seq_min,
            "seq_max": self.seq_max,
            "row_count": self.row_count,
            "runs": self.runs,
            "reason": self.reason,
            "actor": self.actor,
            "pruned_at": self.pruned_at.isoformat() if self.pruned_at else None,
        }

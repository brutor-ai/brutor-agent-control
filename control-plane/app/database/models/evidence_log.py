# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Tenant evidence logs — RFC 6962 trees, heads and receipts

One append-only Merkle tree per tenant. Leaves are `SHA-256(0x00 ‖
statement bytes)`, nodes `SHA-256(0x01 ‖ left ‖ right)`; the stored node
set is every complete aligned subtree, which is what an inclusion proof
for the current tree size is served from (`app/services/evidence/merkle.py`).

Every 100 entries or 15 minutes the core issues a **tree head** — a Signed
Statement over `{log_id, tree_size, root, prev_size, prev_root, issued_at,
consistency_proof}` — registers it with each enabled witness, and appends
the head to its own log so head N is covered by head N+1. A **receipt** is
what one witness handed back for one head; its `verdict` is the core's
offline verification of that receipt against the pinned key, and the head's
`grade` folds the receipts: `standalone` (none) → `witnessed` (one) →
`plural` (two operators); `self_witnessed` marks the trial bundle's local
witness, which proves ordering but not independence.

All of these are written by the Rust core; the control plane reads them.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

ENTRY_RECORD = "record"
ENTRY_TREE_HEAD = "tree_head"
ENTRY_DISCLOSURE = "disclosure"
ENTRY_WITNESS_BACKFILL = "witness_backfill"
ENTRY_KINDS = frozenset({ENTRY_RECORD, ENTRY_TREE_HEAD, ENTRY_DISCLOSURE, ENTRY_WITNESS_BACKFILL})

GRADE_STANDALONE = "standalone"
GRADE_WITNESSED = "witnessed"
GRADE_PLURAL = "plural"
GRADE_SELF_WITNESSED = "self_witnessed"
HEAD_GRADES = frozenset({GRADE_STANDALONE, GRADE_WITNESSED, GRADE_PLURAL, GRADE_SELF_WITNESSED})

#: Worst-to-best order for folding grades across heads.
GRADE_RANK = {
    GRADE_STANDALONE: 0,
    GRADE_SELF_WITNESSED: 1,
    GRADE_WITNESSED: 2,
    GRADE_PLURAL: 3,
}

WITNESS_KIND_SCITT = "scitt"
WITNESS_KIND_RFC3161 = "rfc3161"
WITNESS_KINDS = frozenset({WITNESS_KIND_SCITT, WITNESS_KIND_RFC3161})

RECEIPT_WITNESSED = "witnessed"
RECEIPT_TIMESTAMPED = "timestamped"
RECEIPT_UNVERIFIED = "unverified"
RECEIPT_INVALID = "invalid"
RECEIPT_VERDICTS = frozenset({RECEIPT_WITNESSED, RECEIPT_TIMESTAMPED, RECEIPT_UNVERIFIED, RECEIPT_INVALID})


def _b64(data) -> Any:
    return base64.b64encode(data).decode("ascii") if data else None


def _iso(value) -> Any:
    return value.isoformat() if value else None


class EvidenceLog(Base):
    """One log per tenant. `log_id = sha256(deployment_id ‖ tenant_id)`."""

    __tablename__ = "evidence_logs"

    log_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, unique=True)
    next_seq = Column(BigInteger, nullable=False, default=0, server_default="0")
    tree_size = Column(BigInteger, nullable=False, default=0, server_default="0")
    root = Column(String(64), nullable=True, comment="Current Merkle root (hex)")
    kid = Column(String(64), nullable=True, comment="Key id heads are signed with")
    last_tree_head_id = Column(String(64), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "log_id": self.log_id,
            "tenant_id": self.tenant_id,
            "next_seq": self.next_seq or 0,
            "tree_size": self.tree_size or 0,
            "root": self.root,
            "kid": self.kid,
            "last_tree_head_id": self.last_tree_head_id,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class EvidenceLogNode(Base):
    """A stored Merkle node: the hash of the complete subtree at
    (`level`, `index`). Level 0 is the leaf row. Append-only."""

    __tablename__ = "evidence_log_nodes"

    log_id = Column(String(64), nullable=False)
    level = Column(Integer, nullable=False)
    index = Column(BigInteger, nullable=False)
    hash = Column(String(64), nullable=False, comment="Subtree hash (hex)")

    __table_args__ = (
        PrimaryKeyConstraint("log_id", "level", "index", name="pk_evidence_log_nodes"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "log_id": self.log_id,
            "level": self.level,
            "index": self.index,
            "hash": self.hash,
        }


class EvidenceLogEntry(Base):
    """What leaf `seq` of a log is — a record, a head, a disclosure or a
    late witness receipt — and where to find it."""

    __tablename__ = "evidence_log_entries"

    log_id = Column(String(64), nullable=False)
    seq = Column(BigInteger, nullable=False)
    kind = Column(String(24), nullable=False, comment="record | tree_head | disclosure | witness_backfill")
    leaf_hash = Column(String(64), nullable=False, comment="SHA-256(0x00 ‖ statement) (hex)")
    ref_id = Column(String(64), nullable=True, comment="record_id / tree head id / disclosure id")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("log_id", "seq", name="pk_evidence_log_entries"),
    )

    @validates("kind")
    def _validate_kind(self, _key, value):
        if value not in ENTRY_KINDS:
            raise ValueError(f"kind must be one of {sorted(ENTRY_KINDS)}, got {value!r}")
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "log_id": self.log_id,
            "seq": self.seq,
            "kind": self.kind,
            "leaf_hash": self.leaf_hash,
            "ref_id": self.ref_id,
            "created_at": _iso(self.created_at),
        }


class EvidenceTreeHead(Base):
    """A signed checkpoint over the log at `tree_size`."""

    __tablename__ = "evidence_tree_heads"

    id = Column(String(64), primary_key=True)
    log_id = Column(String(64), nullable=False)
    tenant_id = Column(String(255), nullable=False)
    tree_size = Column(BigInteger, nullable=False)
    root = Column(String(64), nullable=False)
    prev_size = Column(BigInteger, nullable=False, default=0, server_default="0")
    prev_root = Column(String(64), nullable=True)
    statement = Column(
        LargeBinary, nullable=False, comment="Signed Statement over the head (full-content)"
    )
    transparent_statement = Column(
        LargeBinary,
        nullable=True,
        comment="The statement with every receipt attached at unprotected[394]",
    )
    issued_at = Column(DateTime(timezone=True), nullable=False)
    stamp_seq = Column(
        BigInteger, nullable=True, comment="Leaf index of this head's own tree_head entry"
    )
    grade = Column(
        String(16),
        nullable=False,
        default=GRADE_STANDALONE,
        server_default=GRADE_STANDALONE,
        comment="standalone | witnessed | plural | self_witnessed",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("uq_evidence_tree_heads_log_size", "log_id", "tree_size", unique=True),
        Index("ix_evidence_tree_heads_tenant", "tenant_id", "issued_at"),
    )

    @validates("grade")
    def _validate_grade(self, _key, value):
        if value not in HEAD_GRADES:
            raise ValueError(f"grade must be one of {sorted(HEAD_GRADES)}, got {value!r}")
        return value

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "log_id": self.log_id,
            "tenant_id": self.tenant_id,
            "tree_size": self.tree_size,
            "root": self.root,
            "prev_size": self.prev_size or 0,
            "prev_root": self.prev_root,
            "issued_at": _iso(self.issued_at),
            "stamp_seq": self.stamp_seq,
            "grade": self.grade,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary_dict()
        data["statement_b64"] = _b64(self.statement)
        data["transparent_statement_b64"] = _b64(self.transparent_statement)
        data["created_at"] = _iso(self.created_at)
        return data


class EvidenceReceipt(Base):
    """What one witness handed back for one head."""

    __tablename__ = "evidence_receipts"

    id = Column(String(64), primary_key=True)
    tree_head_id = Column(
        String(64),
        ForeignKey("evidence_tree_heads.id", ondelete="CASCADE"),
        nullable=False,
    )
    witness_id = Column(String(64), nullable=True)
    kind = Column(String(16), nullable=False, comment="scitt | rfc3161")
    receipt = Column(LargeBinary, nullable=False)
    leaf_index = Column(BigInteger, nullable=True, comment="Index in the WITNESS's log")
    tree_size = Column(BigInteger, nullable=True, comment="Size of the WITNESS's log")
    witness_iat = Column(BigInteger, nullable=True, comment="Witness-asserted issue time (epoch s)")
    verdict = Column(
        String(16),
        nullable=False,
        default=RECEIPT_UNVERIFIED,
        server_default=RECEIPT_UNVERIFIED,
        comment="witnessed | timestamped | unverified | invalid",
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)
    vendor_ext = Column(JSONB, nullable=True, comment="Vendor-private labels — surfaced, never trusted")
    error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (Index("ix_evidence_receipts_head", "tree_head_id"),)

    @validates("kind")
    def _validate_kind(self, _key, value):
        if value not in WITNESS_KINDS:
            raise ValueError(f"kind must be one of {sorted(WITNESS_KINDS)}, got {value!r}")
        return value

    @validates("verdict")
    def _validate_verdict(self, _key, value):
        if value not in RECEIPT_VERDICTS:
            raise ValueError(
                f"verdict must be one of {sorted(RECEIPT_VERDICTS)}, got {value!r}"
            )
        return value

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tree_head_id": self.tree_head_id,
            "witness_id": self.witness_id,
            "kind": self.kind,
            "verdict": self.verdict,
            "verified_at": _iso(self.verified_at),
            "leaf_index": self.leaf_index,
            "tree_size": self.tree_size,
            "witness_iat": self.witness_iat,
            "error": self.error,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary_dict()
        data["receipt_b64"] = _b64(self.receipt)
        data["vendor_ext"] = self.vendor_ext
        data["created_at"] = _iso(self.created_at)
        return data


__all__ = [
    "EvidenceLog",
    "EvidenceLogNode",
    "EvidenceLogEntry",
    "EvidenceTreeHead",
    "EvidenceReceipt",
    "ENTRY_KINDS",
    "ENTRY_RECORD",
    "ENTRY_TREE_HEAD",
    "ENTRY_DISCLOSURE",
    "ENTRY_WITNESS_BACKFILL",
    "HEAD_GRADES",
    "GRADE_RANK",
    "GRADE_STANDALONE",
    "GRADE_WITNESSED",
    "GRADE_PLURAL",
    "GRADE_SELF_WITNESSED",
    "WITNESS_KINDS",
    "WITNESS_KIND_SCITT",
    "WITNESS_KIND_RFC3161",
    "RECEIPT_VERDICTS",
    "RECEIPT_WITNESSED",
    "RECEIPT_TIMESTAMPED",
    "RECEIPT_UNVERIFIED",
    "RECEIPT_INVALID",
]

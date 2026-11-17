# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence bundles — what the core packaged for a verifier

A bundle (`ai.brutor/evidence-bundle/v1`) is a self-contained, offline
verifiable package: records, inclusion proofs against a signed tree head,
the heads and their receipts, consistency proofs, prunes and disclosures.
The Rust core builds it (`POST /v1/evidence/bundles`) and persists every
one here; the control plane only reads these rows (download, report links).

`selection` is `contiguous` (a window or run selector — nothing in range
left out) or `producer-selected` (an explicit id list — the verifier is
told so). `payloads_mode` is always `none` today: bodies never enter
records. `requested_by` is the admin who asked — stored here only, never
in a record.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Column, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base
from app.database.models.evidence_columns import created_at_column, iso

SELECTION_CONTIGUOUS = "contiguous"
SELECTION_PRODUCER_SELECTED = "producer-selected"
BUNDLE_SELECTIONS = frozenset({SELECTION_CONTIGUOUS, SELECTION_PRODUCER_SELECTED})

PAYLOADS_NONE = "none"
PAYLOADS_ALL = "all"
PAYLOADS_MODES = frozenset({PAYLOADS_NONE, PAYLOADS_ALL})


class EvidenceBundle(Base):
    """One bundle the core built (core-written; CP reads)."""

    __tablename__ = "evidence_bundles"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'bdl-'")
    tenant_id = Column(String(255), nullable=False)
    log_id = Column(String(64), nullable=False)
    selector = Column(JSONB, nullable=False, comment="record_ids | root_task_id | system+window")
    audience = Column(String(255), nullable=True, comment="Non-null ⇒ a disclosure record was sealed")
    payloads_mode = Column(String(16), nullable=False, comment="none | all")
    selection = Column(String(24), nullable=False, comment="contiguous | producer-selected")
    framework_id = Column(String(64), nullable=True)
    catalog_version = Column(String(32), nullable=True)
    obligation_key = Column(String(128), nullable=True)
    root_record_id = Column(String(64), nullable=True)
    record_count = Column(Integer, nullable=False)
    bundle_digest = Column(String(64), nullable=False, comment="hex SHA-256 of JCS(bundle − countersignatures)")
    record_id = Column(String(64), nullable=True, comment="The ai.brutor.bundle record sealing the digest")
    disclosure_record_id = Column(String(64), nullable=True)
    bundle = Column(JSONB, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    requested_by = Column(String(255), nullable=True)
    created_at = created_at_column()

    __table_args__ = (Index("ix_evidence_bundles_tenant", "tenant_id", "created_at"),)

    def summary_dict(self) -> Dict[str, Any]:
        """Everything but the bundle body — for lists."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "log_id": self.log_id,
            "selector": self.selector or {},
            "audience": self.audience,
            "payloads_mode": self.payloads_mode,
            "selection": self.selection,
            "framework_id": self.framework_id,
            "catalog_version": self.catalog_version,
            "obligation_key": self.obligation_key,
            "root_record_id": self.root_record_id,
            "record_count": self.record_count,
            "bundle_digest": self.bundle_digest,
            "record_id": self.record_id,
            "disclosure_record_id": self.disclosure_record_id,
            "size_bytes": self.size_bytes,
            "requested_by": self.requested_by,
            "created_at": iso(self.created_at),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary_dict()
        data["bundle"] = self.bundle
        return data


__all__ = [
    "EvidenceBundle",
    "BUNDLE_SELECTIONS",
    "SELECTION_CONTIGUOUS",
    "SELECTION_PRODUCER_SELECTED",
    "PAYLOADS_MODES",
    "PAYLOADS_NONE",
    "PAYLOADS_ALL",
]

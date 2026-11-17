# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Documentation exports — generated technical documentation

A framework template (e.g. EU AI Act Annex IV) filled from an AI system's
contract (`contract_version`), compliance profile and evidence. `content`
is the generated document, `digest` its hex SHA-256 (JCS); generation is
sealed as an `ai.brutor.documentation` record (`record_id`) referencing
that digest, so a later copy can be shown to be the one generated here.

Written by the control plane; immutable once written.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Column, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base
from app.database.models.evidence_columns import created_at_column, iso


class ComplianceDocumentationExport(Base):
    """One generated documentation export (CP-written)."""

    __tablename__ = "compliance_documentation_exports"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'doc-'")
    tenant_id = Column(String(255), nullable=False)
    ai_system_id = Column(String(255), nullable=False)
    framework_id = Column(String(64), nullable=False)
    template = Column(String(64), nullable=False)
    contract_version = Column(Integer, nullable=True)
    content = Column(JSONB, nullable=False)
    digest = Column(String(64), nullable=False)
    record_id = Column(String(64), nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = created_at_column()

    __table_args__ = (
        Index("ix_compliance_doc_exports_system", "tenant_id", "ai_system_id", "created_at"),
    )

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "framework_id": self.framework_id,
            "template": self.template,
            "contract_version": self.contract_version,
            "digest": self.digest,
            "record_id": self.record_id,
            "created_by": self.created_by,
            "created_at": iso(self.created_at),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary_dict()
        data["content"] = self.content
        return data


__all__ = ["ComplianceDocumentationExport"]

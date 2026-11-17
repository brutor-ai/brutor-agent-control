# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI-interaction notice configuration (EU AI Act Art. 50(1))

Where and how the portal tells a person they are interacting with an AI
system. One row per scope: `ai_system_id` NULL is the tenant default, a set
`ai_system_id` overrides it for that system (unique nulls-not-distinct via
a COALESCE expression index). `texts` = `{locale: text}`; `text_digests` =
`{locale: hex SHA-256}` — the digest is what the rendered-notice record
references, so the text itself never enters a record. `exemption` =
`{asserted, basis, rationale}` when the operator claims the notice is not
required ("obvious from the circumstances").

Each save seals an `ai.brutor.notice-config` record (`record_id`).
Written by the control plane; the core reads it for
`GET /v1/portal/evidence/notice-config`.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Boolean, Column, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.evidence_columns import (
    check_vocab,
    created_at_column,
    iso,
    updated_at_column,
)

NOTICE_SURFACES = frozenset({"banner", "inline", "both"})


class ComplianceNoticeConfig(Base):
    """Notice configuration for a tenant or one AI system (CP-written; core reads)."""

    __tablename__ = "compliance_notice_configs"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'ntc-'")
    tenant_id = Column(String(255), nullable=False)
    ai_system_id = Column(String(255), nullable=True, comment="NULL = tenant default")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    surface = Column(String(16), nullable=False, default="both", server_default="both")
    texts = Column(JSONB, nullable=False, default=dict, server_default="{}")
    text_digests = Column(JSONB, nullable=False, default=dict, server_default="{}")
    exemption = Column(JSONB, nullable=True)
    record_id = Column(String(64), nullable=True)
    updated_by = Column(String(255), nullable=True)
    created_at = created_at_column()
    updated_at = updated_at_column()

    __table_args__ = (
        Index(
            "uq_compliance_notice_configs_scope",
            "tenant_id",
            text("COALESCE(ai_system_id, '')"),
            unique=True,
        ),
    )

    @validates("surface")
    def _validate_surface(self, _key, value):
        return check_vocab("surface", value, NOTICE_SURFACES)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "enabled": bool(self.enabled),
            "surface": self.surface,
            "texts": self.texts or {},
            "text_digests": self.text_digests or {},
            "exemption": self.exemption,
            "record_id": self.record_id,
            "updated_by": self.updated_by,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


__all__ = ["ComplianceNoticeConfig", "NOTICE_SURFACES"]

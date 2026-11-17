# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence settings — per-tenant evidence switches

* `per_tenant_key` — sign this tenant's records under its own key (created
  by the core on first rotation) instead of the deployment key.
* `salt_digests_default` — the default for a system profile's
  `core.salt_digests`: salt payload digests so a low-entropy body cannot be
  confirmed by guessing.

No row means both are false. Written by the control plane; read by the core.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import Boolean, Column, String

from app.database import Base
from app.database.models.evidence_columns import iso, updated_at_column


class EvidenceSettings(Base):
    """One row per tenant (CP-written)."""

    __tablename__ = "evidence_settings"

    tenant_id = Column(String(255), primary_key=True)
    per_tenant_key = Column(Boolean, nullable=False, default=False, server_default="false")
    salt_digests_default = Column(Boolean, nullable=False, default=False, server_default="false")
    updated_at = updated_at_column()
    updated_by = Column(String(255), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "per_tenant_key": bool(self.per_tenant_key),
            "salt_digests_default": bool(self.salt_digests_default),
            "updated_at": iso(self.updated_at),
            "updated_by": self.updated_by,
        }

    @staticmethod
    def defaults_dict(tenant_id: str) -> Dict[str, Optional[Any]]:
        """What a tenant with no row effectively has."""
        return {
            "tenant_id": tenant_id,
            "per_tenant_key": False,
            "salt_digests_default": False,
            "updated_at": None,
            "updated_by": None,
        }


__all__ = ["EvidenceSettings"]

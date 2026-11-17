# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Platform settings: what the system administrator sets for the whole
# installation (Enterprise Edition)

One row, id `platform`. Written by the Control Plane
(`/v1/admin/platform/settings`, system administrator only, every change in
the system administrator's audit log); read by the Core Gateway.

* `global_key_allowed_target_tenants`: the tenants a shared (global-scope)
  API key may act for besides its own. NULL: no list, and only the platform
  service key acts for other tenants. A JSON array of tenant ids: such a key
  acts for a tenant on it and is refused for any other, the platform service
  key included. Never an empty array. It replaced the gateway's
  `BRUTOR_GLOBAL_KEY_ALLOWED_TARGET_TENANTS`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Column, String

from app.database import Base
from app.database.models.evidence_columns import iso, updated_at_column


class PlatformSettings(Base):
    """The installation's settings (one row)."""

    __tablename__ = "platform_settings"

    SINGLETON_ID = "platform"

    id = Column(String(32), primary_key=True, default=SINGLETON_ID)
    global_key_allowed_target_tenants = Column(
        JSON,
        nullable=True,
        comment=(
            "Tenant ids a global-scope API key may act for besides its own (a JSON array); "
            "NULL: no list, only the platform service key acts for other tenants."
        ),
    )
    updated_at = updated_at_column()
    updated_by = Column(String(255), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "global_key_allowed_target_tenants": self.global_key_allowed_target_tenants,
            "updated_at": iso(self.updated_at),
            "updated_by": self.updated_by,
        }

    @staticmethod
    def defaults_dict() -> Dict[str, Optional[Any]]:
        """What an installation with no row has."""
        return {"global_key_allowed_target_tenants": None, "updated_at": None, "updated_by": None}


__all__ = ["PlatformSettings"]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Compliance profile per AI System

One row per AI System: a framework-neutral **core** (who operates it, in
which jurisdictions, whether it talks to natural persons, makes automated
decisions about them, generates synthetic content …) plus a **frameworks**
block keyed by registry id with that framework's own facts (`eu-ai-act`:
role and risk tier; `hipaa`: covered entity or business associate; …).

The existing `resource_groups.eu_ai_act_role` / `eu_ai_act_risk_tier`
columns stay the source of truth for the EU AI Act board: the profile
endpoint reads them into the `eu-ai-act` block when no row exists and
writes them back on every save, so nothing that already reads the group
breaks.

`declaration_record_id` points at the sealed `fyi` declaration record the
core produces for each save — a DECLARED evidence statement then
cites a sealed declaration rather than a mutable row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.resource_mixins import ResourceAuthorshipMixin

# --------------------------------------------------------------------------
# Core vocabulary
# --------------------------------------------------------------------------

OPERATOR_ROLES = frozenset({"builder", "operator", "vendor_of", "reseller"})

#: Boolean facts in `core`. Anything else under these keys is rejected.
CORE_BOOLEAN_FIELDS = (
    "interacts_with_natural_persons",
    "generates_synthetic_content",
    "automated_decisions_about_persons",
    "two_phase_actions",
    "inference_records",
    "salt_digests",
)

#: Every key `core` may carry.
CORE_FIELDS = frozenset({
    "operator_role",
    "jurisdictions",
    "entities",
    "dates",
    "external_registrations",
    "sensitivity",
    "notes",
    # Declared facts the resolvers cite:
    # `oversight.override_available` (who is assigned human oversight) and
    # `synthetic.marking_declared` (how synthetic content is marked).
    "oversight_assignment",
    "synthetic_marking",
    *CORE_BOOLEAN_FIELDS,
})


class AISystemComplianceProfile(Base, ResourceAuthorshipMixin):
    """The declared compliance facts for one AI System."""

    __tablename__ = "ai_system_compliance_profiles"

    ai_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id = Column(String(255), nullable=False)
    core = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Framework-neutral facts: operator_role, jurisdictions, sensitivity, flags …",
    )
    frameworks = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Per-framework facts keyed by registry id (eu-ai-act, hipaa …)",
    )
    declaration_record_id = Column(
        String(64),
        nullable=True,
        comment="action_records.record_id of the sealed declaration for the current save",
    )

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

    __table_args__ = (Index("ix_ai_sys_compliance_profiles_tenant", "tenant_id"),)

    @validates("core")
    def _validate_core(self, _key, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("core must be an object")
        role = value.get("operator_role")
        if role is not None and role not in OPERATOR_ROLES:
            raise ValueError(
                f"core.operator_role must be one of {sorted(OPERATOR_ROLES)}, got {role!r}"
            )
        return value

    @validates("frameworks")
    def _validate_frameworks(self, _key, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("frameworks must be an object keyed by framework id")
        return value

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "ai_system_id": self.ai_system_id,
            "tenant_id": self.tenant_id,
            "core": self.core or {},
            "frameworks": self.frameworks or {},
            "declaration_record_id": self.declaration_record_id,
        }
        data.update(self.authorship_dict())
        return data


__all__ = [
    "AISystemComplianceProfile",
    "OPERATOR_ROLES",
    "CORE_BOOLEAN_FIELDS",
    "CORE_FIELDS",
]

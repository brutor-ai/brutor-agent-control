# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Retention floors are framework data

The effective retention of ledger rows and runs for an AI System is

    max(tenant policy, max over the system's active frameworks of floor)

where a framework is *active* for a system when the tenant has it enabled
and the system's profile does not declare it out of scope, and the floor
is the catalog's floor for the system's classification in that framework
(EU AI Act: high-risk 183 d). A framework in the registry without a
catalog contributes its registry `retention_floor_days`.

`None` for a policy window means "keep" — which already satisfies any
floor. The sweeper runs per tenant, so the tenant-level effective policy
raises `log_days` / `run_days` to the largest floor of any of its systems
(keeping more than one system needs is the only safe direction). Body
scrubbing (`body_days`) is not a record floor — record bodies and the row
hashes the records cite are untouched by it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.api.v1.models.admin.retention import DataRetentionPolicy
from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.compliance_framework import ComplianceFramework
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services import compliance_registry
from app.services.compliance import offering
from app.services.data_retention.policy import effective_policy


@dataclass
class FloorSource:
    framework_id: str
    classification: Optional[str]
    floor_days: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "framework_id": self.framework_id,
            "classification": self.classification,
            "floor_days": self.floor_days,
        }


@dataclass
class SystemFloor:
    ai_system_id: str
    name: str
    floor_days: Optional[int]
    sources: List[FloorSource] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ai_system_id": self.ai_system_id,
            "name": self.name,
            "floor_days": self.floor_days,
            "sources": [s.to_dict() for s in self.sources],
        }


def _classification(framework_id: str, system: ResourceGroup, blocks: Dict[str, Any],
                    tier_facts: Any = None) -> Optional[str]:
    """The classification a framework's floor is looked up with — under a
    composite-classification catalog the EFFECTIVE tier,
    the same rule the obligations use (`ai_obligations.evaluated_tier`)."""
    catalog = offering.catalog(framework_id)
    field_name = catalog.classification_field if catalog else "risk_tier"
    if framework_id == "eu-ai-act" and system.eu_ai_act_risk_tier:
        declared: Optional[str] = system.eu_ai_act_risk_tier
    else:
        block = blocks.get(framework_id) or {}
        value = block.get(field_name) if isinstance(block, dict) else None
        declared = value if isinstance(value, str) else None
    if catalog is None:
        return declared
    from app.services.ai_obligations import evaluated_tier

    return evaluated_tier(catalog, declared, system.id, tier_facts)[0]


def floor_sources(
    system: ResourceGroup,
    profile: Optional[AISystemComplianceProfile],
    active: List[ComplianceFramework],
    tier_facts: Any = None,
) -> List[FloorSource]:
    """Every framework floor that applies to `system` (pure over its inputs;
    `tier_facts` = the tenant's `load_tier_facts`, None = declared tiers)."""
    blocks = dict(profile.frameworks or {}) if profile is not None else {}
    out: List[FloorSource] = []
    for fw in active:
        block = blocks.get(fw.id)
        if isinstance(block, dict) and block.get("in_scope") is False:
            continue
        classification = _classification(fw.id, system, blocks, tier_facts)
        catalog = offering.catalog(fw.id)
        if catalog is not None:
            days = catalog.retention_floor_for(classification)
        else:
            days = fw.retention_floor_days
        if days:
            out.append(FloorSource(fw.id, classification, int(days)))
    return out


def active_frameworks(db: Session, tenant: Tenant) -> List[ComplianceFramework]:
    rows = db.query(ComplianceFramework).order_by(ComplianceFramework.sort_order).all()
    return [r for r in rows if compliance_registry.is_enabled(tenant, r)]


def system_floors(db: Session, tenant: Tenant) -> List[SystemFloor]:
    active = active_frameworks(db, tenant)
    systems = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.tenant_id == tenant.id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
        .order_by(ResourceGroup.name)
        .all()
    )
    profiles = {
        p.ai_system_id: p
        for p in db.query(AISystemComplianceProfile)
        .filter(AISystemComplianceProfile.tenant_id == tenant.id)
        .all()
    }
    from app.services.ai_system_composition import load_tier_facts

    tier_facts = load_tier_facts(db, tenant.id)
    out = []
    for s in systems:
        sources = floor_sources(s, profiles.get(s.id), active, tier_facts)
        out.append(
            SystemFloor(
                ai_system_id=s.id,
                name=s.display_name or s.name,
                floor_days=max((x.floor_days for x in sources), default=None),
                sources=sources,
            )
        )
    return out


def system_floor(db: Session, tenant: Tenant, system: ResourceGroup,
                 profile: Optional[AISystemComplianceProfile] = None) -> SystemFloor:
    from app.services.ai_system_composition import load_tier_facts

    if profile is None:
        profile = db.get(AISystemComplianceProfile, system.id)
    sources = floor_sources(system, profile, active_frameworks(db, tenant),
                            load_tier_facts(db, tenant.id))
    return SystemFloor(system.id, system.display_name or system.name,
                       max((x.floor_days for x in sources), default=None), sources)


def _raise(days: Optional[int], floor: Optional[int]) -> Optional[int]:
    if days is None or floor is None:
        return days
    return max(days, floor)


def compose(policy: Optional[DataRetentionPolicy], floor_days: Optional[int]) -> Optional[DataRetentionPolicy]:
    """The policy with `log_days` / `run_days` raised to `floor_days`.
    None stays None (keep); `body_days` is untouched."""
    if policy is None or floor_days is None:
        return policy
    return policy.model_copy(update={
        "log_days": _raise(policy.log_days, floor_days),
        "run_days": _raise(policy.run_days, floor_days),
    })


def effective_tenant_policy(db: Session, tenant: Tenant) -> Optional[DataRetentionPolicy]:
    """What the sweeper enforces: the tenant policy composed with the
    largest floor of any of the tenant's systems."""
    policy = effective_policy(tenant)
    if policy is None:
        return None
    floor = max((f.floor_days for f in system_floors(db, tenant) if f.floor_days), default=None)
    return compose(policy, floor)


def retention_view(db: Session, tenant: Tenant) -> Dict[str, Any]:
    """The Data Retention tab's composition block."""
    policy = effective_policy(tenant)
    floors = system_floors(db, tenant)
    tenant_floor = max((f.floor_days for f in floors if f.floor_days), default=None)
    effective = compose(policy, tenant_floor)
    return {
        "policy": policy.model_dump() if policy else None,
        "floor_days": tenant_floor,
        "effective": effective.model_dump() if effective else None,
        "raised": bool(
            policy is not None
            and effective is not None
            and (policy.log_days != effective.log_days or policy.run_days != effective.run_days)
        ),
        "systems": [
            {
                **f.to_dict(),
                "effective_log_days": _raise(policy.log_days, f.floor_days) if policy else None,
                "effective_run_days": _raise(policy.run_days, f.floor_days) if policy else None,
            }
            for f in floors
            if f.floor_days
        ],
    }


__all__ = [
    "FloorSource",
    "SystemFloor",
    "active_frameworks",
    "compose",
    "effective_tenant_policy",
    "floor_sources",
    "retention_view",
    "system_floor",
    "system_floors",
]

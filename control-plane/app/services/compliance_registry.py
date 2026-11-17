# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The framework registry, joined to a tenant's declarations

`compliance_frameworks` is platform-wide reference data; which of those a
tenant has declared lives on the tenant. Two places, deliberately:

* Frameworks with a `tag_key` (the five tagging frameworks) are enabled by their key
  appearing in `tenants.compliance_profile["frameworks"]` — the same list
  the Python and Rust tagging engines read, so enabling "GDPR" here is
  exactly what turns `gdpr_art_30` tags on, with no second switch to
  drift.
* Frameworks with no tagging (DORA, NIS2, NIST AI RMF) are enabled by
  their registry id appearing in `compliance_profile["registry_enabled"]`.

`obligation_count` comes from the obligation catalog in
`app/services/ai_obligations.py` when one exists for the id (only the EU
AI Act today), otherwise 0 — a framework in the registry with no catalog
is a placeholder, and the UI should say so rather than imply coverage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database.models.compliance_framework import ComplianceFramework
from app.database.models.tenant import Tenant
from app.services.compliance import offering

PROFILE_FRAMEWORKS_KEY = "frameworks"
PROFILE_REGISTRY_KEY = "registry_enabled"


def _profile(tenant: Tenant) -> Dict[str, Any]:
    profile = tenant.compliance_profile
    return dict(profile) if isinstance(profile, dict) else {}


def _enabled_sets(tenant: Tenant) -> tuple[set, set]:
    profile = _profile(tenant)
    tags = profile.get(PROFILE_FRAMEWORKS_KEY) or []
    ids = profile.get(PROFILE_REGISTRY_KEY) or []
    return (
        {t for t in tags if isinstance(t, str)},
        {i for i in ids if isinstance(i, str)},
    )


def is_enabled(tenant: Tenant, framework: ComplianceFramework) -> bool:
    """Enabled AND offered: a framework the tenant switched on before it
    stopped being offered keeps the stored setting but is off."""
    if offering.catalog(framework.id) is None:
        return False
    tags, ids = _enabled_sets(tenant)
    if framework.tag_key:
        return framework.tag_key in tags
    return framework.id in ids


def obligation_count(framework_id: str) -> int:
    catalog = offering.any_catalog(framework_id)
    return len(catalog.obligations) if catalog is not None else 0


def framework_view(tenant: Tenant, framework: ComplianceFramework) -> Dict[str, Any]:
    data = framework.to_dict()
    data["enabled"] = is_enabled(tenant, framework)
    data["obligation_count"] = obligation_count(framework.id)
    catalog = offering.any_catalog(framework.id)
    # The catalog is the source of truth for verification, not the seeded row.
    if catalog is not None:
        data["verified_against_instrument"] = catalog.verified_against_instrument
        data["instrument_status"] = catalog.instrument_status
        data["verified_on"] = catalog.verified_on.isoformat() if catalog.verified_on else None
    data["offered"] = catalog is not None and offering.is_offered(catalog)
    data["unavailable_reason"] = (
        offering.unavailable_reason(catalog) if catalog is not None
        else "No obligation catalog for this framework."
    )
    return data


def get_framework(db: Session, framework_id: str) -> ComplianceFramework:
    row = db.get(ComplianceFramework, framework_id)
    if row is None:
        known = [r.id for r in db.query(ComplianceFramework.id).order_by(ComplianceFramework.sort_order)]
        raise HTTPException(
            status_code=404,
            detail=f"Unknown compliance framework {framework_id!r}. Available: {known}",
        )
    return row


def list_frameworks(db: Session, tenant: Tenant) -> List[Dict[str, Any]]:
    rows = (
        db.query(ComplianceFramework)
        .order_by(ComplianceFramework.sort_order, ComplianceFramework.id)
        .all()
    )
    return [framework_view(tenant, r) for r in rows]


def set_enabled(
    db: Session, tenant: Tenant, framework_id: str, enabled: bool
) -> Dict[str, Any]:
    """Flip one framework for the tenant and return its updated view.

    Writes the tenant's `compliance_profile` in place, touching only the
    list the framework lives in. Other keys (and the other list) are kept
    so this cannot undo what the tagging PATCH or a policy apply wrote.
    """
    framework = get_framework(db, framework_id)
    if enabled:
        catalog = offering.any_catalog(framework_id)
        reason = (offering.unavailable_reason(catalog) if catalog is not None
                  else "No obligation catalog for this framework.")
        if reason is not None:
            raise HTTPException(status_code=409, detail=reason)
    profile = _profile(tenant)
    key = PROFILE_FRAMEWORKS_KEY if framework.tag_key else PROFILE_REGISTRY_KEY
    member = framework.tag_key or framework.id
    current: List[str] = [
        v for v in (profile.get(key) or []) if isinstance(v, str)
    ]
    if enabled and member not in current:
        current.append(member)
    elif not enabled and member in current:
        current = [v for v in current if v != member]
    profile[key] = current
    # Always carry the tagging list — its absence is what the tagging
    # engine treats as "no profile", and `{}` would read the same way.
    profile.setdefault(PROFILE_FRAMEWORKS_KEY, [])
    tenant.compliance_profile = profile
    flag_modified(tenant, "compliance_profile")
    return framework_view(tenant, framework)


__all__ = [
    "list_frameworks",
    "set_enabled",
    "get_framework",
    "framework_view",
    "is_enabled",
    "obligation_count",
    "PROFILE_FRAMEWORKS_KEY",
    "PROFILE_REGISTRY_KEY",
]

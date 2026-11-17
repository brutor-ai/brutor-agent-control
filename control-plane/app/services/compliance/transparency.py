# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Transparency summary (the Transparency page)

The notice configuration a system resolves to, the notice evidence (the
`notice.before_first_turn` statement: conversations whose notice record
precedes the first assistant turn, n of m), the notice-text declaration
and the synthetic-content declaration — each through the shared resolver
engine, so this page and every framework's obligation row agree.

Without `ai_system_id` the notice evidence is summed across the tenant's
AI Systems (n and m added — the same population, split by system); the
declarations are per system and are omitted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import notices
from app.services.compliance.catalogs import common
from app.services.compliance.resolvers._base import MET, NOT_EVALUABLE, NOT_MET


def _stmt(result: Any) -> Dict[str, Any]:
    data = result.to_dict()
    return {k: data.get(k) for k in ("key", "statement", "basis", "status", "n", "m", "detail", "limit",
                                     "citations", "facts")}


def summary(db: Session, tenant_id: str, system: Optional[ResourceGroup], start: datetime,
            end: datetime) -> Dict[str, Any]:
    from app.services import ai_obligations
    from app.services.compliance import resolvers

    window = (start, end)
    out: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "ai_system_id": system.id if system else None,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "config": notices.effective_config(db, tenant_id, system.id if system else None),
    }
    if system is not None:
        out["notice_evidence"] = _stmt(
            resolvers.evaluate(db, tenant_id, common.notice_before_first_turn(), system.id, window)
        )
        out["notice_text"] = _stmt(resolvers.evaluate(db, tenant_id, common.notice_text(), system.id, window))
        out["synthetic_content"] = _stmt(
            resolvers.evaluate(db, tenant_id, common.synthetic_marking(), system.id, window)
        )
        profile = ai_obligations.load_profile(db, system)
        core = dict(profile.core or {}) if profile is not None else {}
        for flag in ("generates_synthetic_content", "interacts_with_natural_persons"):
            out[flag] = core.get(flag) if isinstance(core.get(flag), bool) else None
        return out

    systems: List[ResourceGroup] = (
        db.query(ResourceGroup)
        .filter(ResourceGroup.tenant_id == tenant_id, ResourceGroup.group_type == GroupType.AI_SYSTEM)
        .all()
    )
    n = m = 0
    per_system = []
    citations: List[str] = []
    for s in systems:
        r = resolvers.evaluate(db, tenant_id, common.notice_before_first_turn(), s.id, window)
        if r.m:
            n += r.n or 0
            m += r.m
            citations.extend(r.citations[:5])
        per_system.append({"ai_system_id": s.id, "status": r.status, "n": r.n, "m": r.m})
    spec = common.notice_before_first_turn()
    status = NOT_EVALUABLE if m == 0 else (MET if n == m else NOT_MET)
    out["notice_evidence"] = {
        "key": spec.key,
        "statement": spec.statement,
        "basis": spec.basis,
        "status": status,
        "n": n,
        "m": m,
        "detail": (f"{n} of {m} portal conversations showed the notice before the first assistant turn."
                   if m else "No portal conversation in the window — nothing to evaluate."),
        "limit": spec.limit,
        "citations": citations[:20],
        "facts": {"per_system": per_system},
    }
    return out


__all__ = ["summary"]

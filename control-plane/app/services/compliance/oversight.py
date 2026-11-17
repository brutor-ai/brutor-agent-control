# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Human Oversight summary (the Human Oversight page)

Per AI System, through the shared resolvers so the page and every
framework's oversight rows agree:

* **assignment and override** — `oversight.override_available` (run:abort
  held; oversight assignment declared on the compliance profile);
* **approvals** — `oversight.exercised` (requested / decided by a human /
  lapsed / in flight — the complete record — and, beside them, the holds
  raised inside declared incident windows that its n-of-m does not judge);
* **movement** — `oversight.not_rubber_stamp`: drift in approval latency or
  denial rate against the system's own baseline. An INDICATOR, never a
  threshold — a fast approval is not a failure, a change in pace is a
  question.

Plus the blind review queue (pending / graded per judged check) and the
human agreement of each judged check's current evaluator.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.database.models.assurance_check import CHECK_TIER_SEMANTIC, AssuranceCheck
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import judged
from app.services.compliance.catalogs import common


def _stmt(result: Any) -> Dict[str, Any]:
    data = result.to_dict()
    return {k: data.get(k) for k in ("key", "statement", "basis", "status", "n", "m", "detail", "limit",
                                     "citations", "facts")}


def summary(db: Session, tenant_id: str, ai_system_id: Optional[str], start: datetime,
            end: datetime) -> Dict[str, Any]:
    from app.services import ai_obligations
    from app.services.compliance import resolvers

    q = db.query(ResourceGroup).filter(
        ResourceGroup.tenant_id == tenant_id, ResourceGroup.group_type == GroupType.AI_SYSTEM
    )
    if ai_system_id:
        q = q.filter(ResourceGroup.id == ai_system_id)
    systems: List[ResourceGroup] = q.order_by(ResourceGroup.id).all()

    window = (start, end)
    totals = {"requested": 0, "decided": 0, "decided_by_human": 0, "lapsed": 0, "in_flight": 0,
              "excluded_in_incident_windows": 0}
    per_system = []
    for s in systems:
        profile = ai_obligations.load_profile(db, s)
        core = dict(profile.core or {}) if profile is not None else {}
        override = resolvers.evaluate(db, tenant_id, common.override_available(), s.id, window)
        exercised = resolvers.evaluate(db, tenant_id, common.oversight_exercised(), s.id, window)
        movement = resolvers.evaluate(db, tenant_id, common.not_rubber_stamp(), s.id, window)
        for k in totals:
            totals[k] += int((exercised.facts or {}).get(k) or 0)
        per_system.append({
            "ai_system_id": s.id,
            "name": s.display_name or s.name,
            "oversight_assignment": core.get("oversight_assignment"),
            "assignment_basis": "declared",
            "override_available": _stmt(override),
            "approvals": _stmt(exercised),
            "movement": _stmt(movement),
        })

    checks = (
        db.query(AssuranceCheck)
        .filter(AssuranceCheck.tenant_id == tenant_id, AssuranceCheck.tier == CHECK_TIER_SEMANTIC)
        .order_by(AssuranceCheck.name)
        .all()
    )
    if ai_system_id:
        checks = [
            c for c in checks
            if not (c.scope or {}).get("ai_system_ids") or ai_system_id in (c.scope or {}).get("ai_system_ids")
        ]
    queue = judged.queue_counts(db, tenant_id, [c.id for c in checks])
    judged_checks = []
    for c in checks:
        judged_checks.append({
            "check_id": c.id,
            "name": c.name,
            "template": judged.template_of(c),
            "enabled": bool(c.enabled),
            "review_queue": queue.get(c.id, {"pending": 0, "graded": 0, "skipped": 0}),
            "agreement": judged.agreement_for(db, c),
        })
    return {
        "tenant_id": tenant_id,
        "ai_system_id": ai_system_id,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "approvals": totals,
        "systems": per_system,
        "review_queue": {
            "pending": sum(q["review_queue"]["pending"] for q in judged_checks),
            "graded": sum(q["review_queue"]["graded"] for q in judged_checks),
        },
        "judged_checks": judged_checks,
        "movement_note": (
            "Approval latency and denial rate are indicators of movement against each system's own "
            "baseline, never thresholds."
        ),
    }


__all__ = ["summary"]

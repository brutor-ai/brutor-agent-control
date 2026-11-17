# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The evidence programme's own inbox findings

Two silent failures the programme must never let read as "all fine":

* **`evidence_unwitnessed`** — a tenant that runs a high-risk AI System
  whose newest tree head, issued more than an hour ago, carries no
  independent witness receipt (grade `standalone` or `self_witnessed`). The log is self-attested until a witness countersigns;
  for a high-risk system that is a finding, not a footnote.
* **`capture_gap`** — governed actions in the last hour that should carry a
  sealed record and do not (`proxy_logs.record_id` NULL), counted by the
  same rule the `records.every_verdict_sealed` statement uses (a mirror of
  the core's consequential rule — read-only tool calls and plain LLM
  completions are not expected to seal). Tenants with evidence disabled
  (no tenant log was ever created: the core has no key) are skipped — a
  disabled programme is shown as disabled elsewhere, not as a gap every
  hour.

Both follow the inbox's dedup convention: one open item per
`<category>:<discriminators>` fingerprint, bumped on re-occurrence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.evidence_log import (
    GRADE_SELF_WITNESSED,
    GRADE_STANDALONE,
    EvidenceLog,
    EvidenceTreeHead,
)
from app.database.models.resource_group import GroupType, ResourceGroup

log = logging.getLogger("brutor.compliance.findings")

UNWITNESSED_AFTER = timedelta(hours=1)
CAPTURE_WINDOW = timedelta(hours=1)
#: The writer seals within seconds; rows younger than this are not yet a gap.
SEAL_LAG_GRACE = timedelta(minutes=5)
UNWITNESSED_GRADES = (GRADE_STANDALONE, GRADE_SELF_WITNESSED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def high_risk_system_ids(db: Session, tenant_id: str) -> Set[str]:
    """AI Systems classified high-risk in any framework (the legacy EU
    column or any profile block's `risk_tier`)."""
    ids = {
        r[0]
        for r in db.query(ResourceGroup.id)
        .filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
            ResourceGroup.eu_ai_act_risk_tier == "high",
        )
        .all()
    }
    for profile in db.query(AISystemComplianceProfile).filter(AISystemComplianceProfile.tenant_id == tenant_id):
        blocks = profile.frameworks if isinstance(profile.frameworks, dict) else {}
        if any(isinstance(b, dict) and str(b.get("risk_tier") or "").lower() == "high" for b in blocks.values()):
            ids.add(profile.ai_system_id)
    return ids


def scan_unwitnessed(db: Session, *, now: Optional[datetime] = None, inbox=None) -> int:
    """Raise `evidence_unwitnessed` per tenant. Returns items raised/bumped."""
    from app.services.assurance_inbox import AssuranceInboxService

    now = now or _now()
    inbox = inbox or AssuranceInboxService()
    cutoff = now - UNWITNESSED_AFTER
    raised = 0
    for tenant_log in db.query(EvidenceLog).all():
        head = (
            db.query(EvidenceTreeHead)
            .filter(EvidenceTreeHead.log_id == tenant_log.log_id, EvidenceTreeHead.issued_at < cutoff)
            .order_by(EvidenceTreeHead.issued_at.desc(), EvidenceTreeHead.tree_size.desc())
            .first()
        )
        if head is None or head.grade not in UNWITNESSED_GRADES:
            continue
        systems = sorted(high_risk_system_ids(db, tenant_log.tenant_id))
        if not systems:
            continue
        issued = _aware(head.issued_at)
        item = inbox.raise_item(
            db,
            tenant_id=tenant_log.tenant_id,
            category="evidence_unwitnessed",
            severity="warning",
            fingerprint=f"evidence_unwitnessed:{tenant_log.log_id}",
            title=(
                f"Evidence log head at size {head.tree_size} has no independent witness receipt "
                f"({head.grade}) — records for high-risk systems are self-attested until one countersigns"
            ),
            evidence={
                "log_id": tenant_log.log_id,
                "tree_head_id": head.id,
                "tree_size": int(head.tree_size),
                "grade": head.grade,
                "issued_at": issued.isoformat() if issued else None,
                "high_risk_system_ids": systems[:20],
            },
        )
        raised += int(item is not None)
    db.flush()
    return raised


def _unsealed_for(db: Session, tenant_id: str, system: ResourceGroup, start: datetime, end: datetime) -> Dict[str, Any]:
    """`{expected, sealed, unsealed}` for one system — the
    `records.every_verdict_sealed` statement over the window."""
    from app.services.compliance import resolvers
    from app.services.compliance.catalogs import common

    result = resolvers.evaluate(db, tenant_id, common.every_verdict_sealed(), system.id, (start, end))
    n = result.n or 0
    m = result.m or 0
    return {"expected": m, "sealed": n, "unsealed": max(0, m - n), "status": result.status}


def scan_capture_gaps(db: Session, *, now: Optional[datetime] = None, inbox=None,
                      tenants: Optional[Iterable[str]] = None) -> int:
    """Raise `capture_gap` per AI System with unsealed governed rows in the
    last hour. Returns items raised/bumped."""
    from app.services.assurance_inbox import AssuranceInboxService

    now = now or _now()
    inbox = inbox or AssuranceInboxService()
    end = now - SEAL_LAG_GRACE
    start = end - CAPTURE_WINDOW
    enabled = {r[0] for r in db.query(EvidenceLog.tenant_id).all()}
    if tenants is not None:
        enabled &= set(tenants)
    raised = 0
    for tenant_id in sorted(enabled):
        systems: List[ResourceGroup] = (
            db.query(ResourceGroup)
            .filter(ResourceGroup.tenant_id == tenant_id, ResourceGroup.group_type == GroupType.AI_SYSTEM)
            .all()
        )
        for system in systems:
            try:
                counts = _unsealed_for(db, tenant_id, system, start, end)
            except Exception:  # noqa: BLE001 — one system must not stop the scan
                log.exception("capture-gap scan failed for %s/%s", tenant_id, system.id)
                continue
            if counts["unsealed"] <= 0:
                continue
            item = inbox.raise_item(
                db,
                tenant_id=tenant_id,
                category="capture_gap",
                severity="warning",
                fingerprint=f"capture_gap:{system.id}",
                title=(
                    f"{counts['unsealed']} of {counts['expected']} governed actions in the last hour "
                    f"have no sealed record ({system.display_name or system.name})"
                ),
                ai_system_id=system.id,
                evidence={
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "expected": counts["expected"],
                    "sealed": counts["sealed"],
                    "unsealed": counts["unsealed"],
                },
            )
            raised += int(item is not None)
    db.flush()
    return raised


__all__ = [
    "CAPTURE_WINDOW",
    "UNWITNESSED_AFTER",
    "high_risk_system_ids",
    "scan_capture_gaps",
    "scan_unwitnessed",
]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Weekly assurance digest.

STRICTLY a projection: every number here comes from the tables the
Assurance Report and Mission Control already read — materialised health
(`ai_system_health`, written by `upsert_health`), the inbox, check
results, drift events, and the run ledger. The digest never computes its
own definition of anything; if a number here could disagree with the
console, the design has failed.

Generation is idempotent per (tenant, week): one stored
``assurance_digests`` row, which is also exactly what the console's
weekly-summary page shows — the operator sees what was sent, not a
re-computation.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_run import AISystemRun
from app.database.models.assurance_check import AssuranceDigest
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.api.v1.endpoints.admin.mission_control_cards import effective_exclusion

# Worst states first, so the digest leads with what needs attention.
_HEALTH_ORDER = {
    "suspended": 0,
    "stalled": 1,
    "silent": 2,
    "failing": 3,
    "degraded": 4,
    "drifting": 5,
    "healthy": 6,
    "unknown": 7,
}

DEFAULT_DIGEST_WEEKDAY = 0  # Monday
DEFAULT_DIGEST_HOUR = 8
DEFAULT_DIGEST_TZ = "UTC"


def digest_schedule(tenant: Optional[Tenant]) -> Dict[str, Any]:
    """The tenant's digest schedule: weekday/hour/timezone with RFC
    defaults (Monday 08:00 tenant-local; UTC when no timezone is set)."""
    settings = (tenant.settings or {}) if tenant else {}
    cfg = settings.get("assurance_digest") or {}
    weekday = cfg.get("weekday", DEFAULT_DIGEST_WEEKDAY)
    hour = cfg.get("hour", DEFAULT_DIGEST_HOUR)
    tz = cfg.get("timezone", DEFAULT_DIGEST_TZ)
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — a bad tz must not kill the scheduler
        tz = DEFAULT_DIGEST_TZ
    return {
        "weekday": int(weekday) if isinstance(weekday, (int, float)) else 0,
        "hour": int(hour) if isinstance(hour, (int, float)) else 8,
        "timezone": tz,
    }


def current_week_start(now_utc: datetime, tz: str) -> date:
    """Monday of the current week in the tenant's timezone."""
    local = now_utc.astimezone(ZoneInfo(tz))
    return (local.date() - timedelta(days=local.weekday()))


def digest_due(now_utc: datetime, schedule: Dict[str, Any]) -> Optional[date]:
    """If the scheduled send point for this week has passed, return the
    week_start it covers (the PREVIOUS week); else None."""
    tz = ZoneInfo(schedule["timezone"])
    local = now_utc.astimezone(tz)
    this_monday = local.date() - timedelta(days=local.weekday())
    send_day = this_monday + timedelta(days=schedule["weekday"])
    send_at = datetime.combine(
        send_day, time(hour=schedule["hour"]), tzinfo=tz
    )
    if local >= send_at:
        # The digest covers the week BEFORE the send point.
        return this_monday - timedelta(days=7)
    return None


def build_digest(db: Session, tenant_id: str, week_start: date) -> Dict[str, Any]:
    """Assemble the digest content for one week. Pure read."""
    start = datetime.combine(week_start, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=7)

    systems = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM.value,
            ResourceGroup.is_active.is_(True),
        )
        .all()
    )
    health_rows = {
        h.ai_system_id: h
        for h in db.query(AISystemHealth)
        .filter(AISystemHealth.tenant_id == tenant_id)
        .all()
    }

    def _count_items(**filters) -> Dict[str, int]:
        q = db.query(
            AssuranceInboxItem.severity, func.count(AssuranceInboxItem.id)
        ).filter(AssuranceInboxItem.tenant_id == tenant_id)
        for attr, cond in filters.items():
            q = q.filter(cond)
        return {sev: int(n) for sev, n in q.group_by(AssuranceInboxItem.severity)}

    # Four GROUP BY queries for the whole estate, not four per system.
    # This runs from the weekly loop but is also reachable interactively
    # via GET /admin/assurance-digest/preview, so a 20-system estate meant
    # 80 round trips while an operator waited.
    system_ids = [s.id for s in systems]

    runs_by_system: Dict[str, tuple] = {}
    raised_by_system: Dict[str, int] = {}
    critical_by_system: Dict[str, int] = {}
    drift_by_system: Dict[str, int] = {}

    if system_ids:
        runs_by_system = {
            sid: (int(n or 0), float(cost or 0))
            for sid, n, cost in db.query(
                AISystemRun.ai_system_id,
                func.count(AISystemRun.id),
                func.coalesce(func.sum(AISystemRun.total_cost_usd), 0),
            )
            .filter(
                AISystemRun.tenant_id == tenant_id,
                AISystemRun.ai_system_id.in_(system_ids),
                AISystemRun.started_at >= start,
                AISystemRun.started_at < end,
                AISystemRun.is_replay.is_(False),
            )
            .group_by(AISystemRun.ai_system_id)
        }
        raised_by_system = {
            sid: int(n or 0)
            for sid, n in db.query(
                AssuranceInboxItem.ai_system_id, func.count(AssuranceInboxItem.id)
            )
            .filter(
                AssuranceInboxItem.tenant_id == tenant_id,
                AssuranceInboxItem.ai_system_id.in_(system_ids),
                AssuranceInboxItem.first_seen_at >= start,
                AssuranceInboxItem.first_seen_at < end,
            )
            .group_by(AssuranceInboxItem.ai_system_id)
        }
        critical_by_system = {
            sid: int(n or 0)
            for sid, n in db.query(
                AssuranceInboxItem.ai_system_id, func.count(AssuranceInboxItem.id)
            )
            .filter(
                AssuranceInboxItem.tenant_id == tenant_id,
                AssuranceInboxItem.ai_system_id.in_(system_ids),
                AssuranceInboxItem.state != "resolved",
                AssuranceInboxItem.severity == "critical",
            )
            .group_by(AssuranceInboxItem.ai_system_id)
        }
        drift_by_system = {
            sid: int(n or 0)
            for sid, n in db.query(
                AISystemDriftEvent.ai_system_id, func.count(AISystemDriftEvent.id)
            )
            .filter(
                AISystemDriftEvent.tenant_id == tenant_id,
                AISystemDriftEvent.ai_system_id.in_(system_ids),
                AISystemDriftEvent.detected_at >= start,
                AISystemDriftEvent.detected_at < end,
            )
            .group_by(AISystemDriftEvent.ai_system_id)
        }

    system_sections: List[Dict[str, Any]] = []
    for system in systems:
        health = health_rows.get(system.id)
        runs, cost = runs_by_system.get(system.id, (0, 0.0))
        system_sections.append(
            {
                "ai_system_id": system.id,
                "name": system.name,
                "health_state": health.health_state if health else "unknown",
                "health_score": health.health_score if health else None,
                "runs": runs,
                "cost_usd": round(cost, 4),
                "items_raised": raised_by_system.get(system.id, 0),
                "open_critical": critical_by_system.get(system.id, 0),
                "drift_events": drift_by_system.get(system.id, 0),
            }
        )
    system_sections.sort(
        key=lambda s: (_HEALTH_ORDER.get(s["health_state"], 9), -s["open_critical"])
    )

    # Tenant-wide inbox movement for the week.
    raised_by_sev = _count_items(
        first_seen=AssuranceInboxItem.first_seen_at >= start,
        first_seen_end=AssuranceInboxItem.first_seen_at < end,
    )
    resolved_week = int(
        db.query(func.count(AssuranceInboxItem.id))
        .filter(
            AssuranceInboxItem.tenant_id == tenant_id,
            AssuranceInboxItem.resolved_at.isnot(None),
            AssuranceInboxItem.resolved_at >= start,
            AssuranceInboxItem.resolved_at < end,
        )
        .scalar()
        or 0
    )
    open_by_sev = _count_items(state=AssuranceInboxItem.state != "resolved")

    # Top check breaches of the week — inbox items, same rows MC shows.
    breach_rows = (
        db.query(AssuranceInboxItem)
        .filter(
            AssuranceInboxItem.tenant_id == tenant_id,
            AssuranceInboxItem.category == "check_breach",
            AssuranceInboxItem.last_seen_at >= start,
            AssuranceInboxItem.last_seen_at < end,
        )
        .order_by(AssuranceInboxItem.occurrence_count.desc())
        .limit(5)
        .all()
    )

    return {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=7)).isoformat(),
        "totals": {
            "systems": len(system_sections),
            "runs": sum(s["runs"] for s in system_sections),
            "cost_usd": round(sum(s["cost_usd"] for s in system_sections), 4),
            "items_raised": int(sum(raised_by_sev.values())),
            "items_resolved": resolved_week,
            "open_critical": int(open_by_sev.get("critical", 0)),
            "open_total": int(sum(open_by_sev.values())),
        },
        # Exclusions are disclosed wherever the rollup is. A digest
        # that hid them would let an estate look calmer than it is.
        "excluded_systems": [
            {
                "ai_system_id": g.id,
                "name": g.name,
                "reason": excl.get("reason"),
                "expires_at": excl.get("expires_at"),
            }
            for g, excl in ((g, effective_exclusion(g)) for g in systems)
            if excl is not None
        ],
        "raised_by_severity": raised_by_sev,
        "open_by_severity": open_by_sev,
        "top_check_breaches": [
            {
                "title": item.title,
                "occurrences": item.occurrence_count,
                "severity": item.severity,
                "ai_system_id": item.ai_system_id,
            }
            for item in breach_rows
        ],
        "systems": system_sections,
    }


def store_digest(
    db: Session, tenant_id: str, week_start: date, content: Dict[str, Any]
) -> Optional[AssuranceDigest]:
    """Insert the weekly row; None when it already exists (idempotent)."""
    existing = (
        db.query(AssuranceDigest)
        .filter(
            AssuranceDigest.tenant_id == tenant_id,
            AssuranceDigest.week_start == week_start,
        )
        .first()
    )
    if existing:
        return None
    row = AssuranceDigest(
        id=f"dig-{ULID()}",
        tenant_id=tenant_id,
        week_start=week_start,
        content=content,
    )
    db.add(row)
    db.flush()
    return row

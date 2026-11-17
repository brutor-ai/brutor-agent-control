# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Scheduled evidence capture

The "long-dangling scheduled factsheet job", finally attached to something.
`TRIGGER_SCHEDULED` is a trigger kind; this is the job behind it, for AI
Systems.

## Why automatic capture is the point

EU AI Act Art. 72-style post-market monitoring and ISO 42001 clause 9.1 both
want *continuous* evidence, and evidence that exists only when an operator
remembers to click is not continuous. A snapshot taken every day by a
machine is worth more at audit time than a thorough one taken twice a year
by a person.

## Off unless switched on

`tenants.assurance_capture_interval_hours` is NULL by default. A job nobody
asked for should not silently accumulate a snapshot of every system every
day — that is storage growth an operator did not choose and cannot explain.

## Also refreshes health

The health row is materialised on the same tick. It is cheap, it keeps
fleet tiles from recomputing per render, and doing it here means a system
nobody has opened in the UI still has a current score.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.database.models.ai_system_health import AISystemHealth
from app.services.assurance_inbox import AssuranceInboxService
from app.database import SessionLocal
from app.services.scheduler_leader import run_as_leader

log = logging.getLogger("brutor-assurance-scheduler")

#: Interval intent is hours; checking every 15 minutes is granular enough
#: that "daily" lands within a quarter hour of the day boundary.
TICK_INTERVAL_SECONDS = 900

#: Systems captured per tick. A large fleet drains over several ticks
#: rather than writing four hundred snapshots in one transaction.
MAX_PER_TICK = 50


def _due(db, tenant_id: str, ai_system_id: str, interval_hours: int) -> bool:
    from app.database.models.ai_system_health import AISystemAssuranceReport

    last = (
        db.query(AISystemAssuranceReport.created_at)
        .filter(
            AISystemAssuranceReport.tenant_id == tenant_id,
            AISystemAssuranceReport.ai_system_id == ai_system_id,
        )
        .order_by(AISystemAssuranceReport.created_at.desc())
        .first()
    )
    if last is None or last[0] is None:
        return True
    when = last[0]
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when >= timedelta(hours=interval_hours)


# Health states that warrant a human look when ENTERED. `healthy`,
# `learning` and `unknown` transitions are not findings.
_INBOX_STATES = ("degraded", "drifting", "stalled", "failing", "silent", "suspended")


def _raise_if_exclusion_lapsed(db, system) -> None:
    """A lapsed rollup exclusion re-enters the rollup by itself; this
    makes sure somebody is told, once, so the decision gets re-made rather
    than the system quietly turning the estate red again."""
    raw = getattr(system, "rollup_exclusion", None)
    if not raw or not isinstance(raw, dict) or not raw.get("expires_at"):
        return
    from app.api.v1.endpoints.admin.mission_control_cards import effective_exclusion

    if effective_exclusion(system) is not None:
        return  # still in force
    from app.services.assurance_inbox import AssuranceInboxService

    AssuranceInboxService().raise_item(
        db,
        tenant_id=system.tenant_id,
        ai_system_id=system.id,
        category="health_transition",
        severity="warning",
        fingerprint=f"rollup_exclusion_lapsed:{system.id}:{raw.get('expires_at')}",
        title=f"Estate exclusion for '{system.name}' has lapsed",
        evidence={
            "reason": raw.get("reason"),
            "note": raw.get("note"),
            "expired_at": raw.get("expires_at"),
            "set_by": raw.get("by"),
            "what_now": (
                "The system is back in Mission Control's worst-of rollup. Renew "
                "the exclusion with a new expiry, or leave it in."
            ),
        },
    )


def run_once() -> int:
    """One pass. Returns how many reports were captured."""
    from app.database.models.ai_system_health import REPORT_TRIGGER_SCHEDULED
    from app.database.models.resource_group import GroupType, ResourceGroup
    from app.database.models.tenant import Tenant
    from app.services.ai_system_assurance import capture, upsert_health

    captured = 0
    db = SessionLocal()
    try:
        tenants = (
            db.query(Tenant.id, Tenant.assurance_capture_interval_hours)
            .filter(Tenant.assurance_capture_interval_hours.isnot(None))
            .all()
        )
        if not tenants:
            return 0

        intervals = {t.id: t.assurance_capture_interval_hours for t in tenants}
        systems = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.tenant_id.in_(list(intervals)),
                ResourceGroup.group_type == GroupType.AI_SYSTEM,
                ResourceGroup.is_active.is_(True),
            )
            .limit(MAX_PER_TICK)
            .all()
        )
    except Exception:
        log.exception("assurance scheduler: could not load candidates")
        db.close()
        return 0
    finally:
        db.close()

    for system in systems:
        db = SessionLocal()
        try:
            fresh = db.merge(system, load=True)
            _raise_if_exclusion_lapsed(db, fresh)
            # Capture the prior state so a transition into a
            # fault state raises an assurance-inbox item exactly once (the
            # fingerprint pins the state; repeats bump the open item).
            prior_state = (
                db.query(AISystemHealth.health_state)
                .filter(
                    AISystemHealth.tenant_id == fresh.tenant_id,
                    AISystemHealth.ai_system_id == fresh.id,
                )
                .scalar()
            )
            # Health is refreshed every tick; a report only when due. The
            # tile should never be stale, but snapshots are evidence and
            # should arrive on the interval the operator asked for.
            row = upsert_health(db, fresh)
            new_state = row.health_state
            if new_state != prior_state and new_state in _INBOX_STATES:
                AssuranceInboxService().raise_item(
                    db,
                    tenant_id=fresh.tenant_id,
                    ai_system_id=fresh.id,
                    category="health_transition",
                    severity=(
                        "critical"
                        if new_state in ("silent", "stalled", "failing", "suspended")
                        else "warning"
                    ),
                    fingerprint=f"health_transition:{fresh.id}:{new_state}",
                    title=(
                        f"Health transitioned "
                        f"{prior_state or 'unknown'} → {new_state}"
                    ),
                    evidence={
                        "previous_state": prior_state,
                        "new_state": new_state,
                        "health_score": row.health_score,
                        "reasons": row.reasons,
                    },
                )
            if _due(db, fresh.tenant_id, fresh.id, intervals[fresh.tenant_id]):
                capture(db, fresh, trigger=REPORT_TRIGGER_SCHEDULED)
                captured += 1
            db.commit()
        except Exception:
            db.rollback()
            log.exception("assurance scheduler: %s failed", system.id)
        finally:
            db.close()

    return captured


async def assurance_scheduler_loop() -> None:
    while True:
        try:
            # Leader election — the due-check is read-then-write against
            # the last capture time, so concurrent workers both see the
            # same system due and produce duplicate assurance snapshots.
            await asyncio.to_thread(run_as_leader, "ai_system_assurance", run_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("assurance scheduler: tick crashed")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_assurance_scheduler() -> asyncio.Task:
    """Spawn the loop. Call once at startup, on the lead instance."""
    return asyncio.create_task(
        assurance_scheduler_loop(), name="ai-system-assurance-scheduler"
    )


__all__ = [
    "MAX_PER_TICK",
    "TICK_INTERVAL_SECONDS",
    "assurance_scheduler_loop",
    "run_once",
    "start_assurance_scheduler",
]

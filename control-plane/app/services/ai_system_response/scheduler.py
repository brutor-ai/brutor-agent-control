# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The closed-loop tick

Detection runs in gateway-core, next to the traffic. The **response** runs
here, because it writes configuration and config writes belong to the
control plane where the ORM owns the column constraints.

The two are joined by the database rather than by a call: the drift sweeper
writes a finding, this loop notices it. That is deliberately loose — an
outage in one does not take the other down, and a finding written while
this process was restarting is picked up on the next tick rather than lost.

# Concurrency safety

Same assumption as the other control-plane schedulers: **one process runs
this**. In a horizontally-scaled deployment only the lead instance should
call `start_response_scheduler`.

The blast radius of getting that wrong is bounded but real — two instances
could each plan a response to the same finding. `response_taken` is stamped
inside the same transaction as the actions, so the second would find the
finding already answered and refuse; the cooldown on the policy is the
second guard. Neither is a substitute for leader election, which follows
the same path the other schedulers take when a customer needs HA.

# Why the tick is slow

Sixty seconds. A drift finding is minutes old by the time it exists — the
sweeper runs every fifteen minutes — so a tighter tick buys nothing and
polls the database for no reason. What matters is that the response is
*bounded*, not instant.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.services.assurance_inbox import AssuranceInboxService
from app.database import SessionLocal
from app.services.drift_inbox_link import (
    INBOX_CATEGORY_DRIFT,
    SELF_RAISED_DRIFT_CLASSES,
    drift_inbox_fingerprint,
)
from app.services.scheduler_leader import run_as_leader
from app.services.ai_system_response.actuator import (
    apply_plan,
    pending_drift_events,
    plan_for_drift,
)

log = logging.getLogger("brutor-ai-system-response-scheduler")

#: How often to look for findings that have not been considered.
TICK_INTERVAL_SECONDS = 60

#: Findings handled per tick. A bound rather than "all of them": a backlog
#: after an outage should drain steadily instead of firing four hundred
#: automatic actions in one transaction.
MAX_PER_TICK = 25


def _inbox_severity(drift_severity: str | None) -> str:
    """Drift severities (low/medium/high/critical) → inbox vocabulary."""
    return {
        "low": "info",
        "medium": "warning",
        "high": "warning",
        "critical": "critical",
    }.get(drift_severity or "medium", "warning")


def run_once(*, now: Optional[datetime] = None) -> int:
    """One pass. Returns how many findings were considered.

    Each finding gets its own transaction. One that crashes must not roll
    back the responses that already succeeded, and must not stop the rest of
    the batch — a single malformed policy should not wedge the closed loop
    for the whole fleet.
    """
    now = now or datetime.now(timezone.utc)
    considered = 0

    db = SessionLocal()
    try:
        events = list(pending_drift_events(db, limit=MAX_PER_TICK))
    except Exception:
        log.exception("closed loop: could not load pending findings")
        db.close()
        return 0
    finally:
        db.close()

    for event in events:
        db = SessionLocal()
        try:
            fresh = db.merge(event, load=True)
            plan = plan_for_drift(db, fresh, now=now)
            apply_plan(db, fresh, plan, now=now)
            # Every drift finding the closed loop considers also
            # lands in the assurance inbox for human review. `contract`
            # class events are raised at their own emitter (envelope) with
            # a term-level fingerprint, and `agent` class events at the
            # agent checks (inbox category `agent`); skip both
            # here to avoid doubles.
            if fresh.drift_class not in SELF_RAISED_DRIFT_CLASSES:
                AssuranceInboxService().raise_item(
                    db,
                    tenant_id=fresh.tenant_id,
                    ai_system_id=fresh.ai_system_id,
                    category=INBOX_CATEGORY_DRIFT,
                    severity=_inbox_severity(fresh.severity),
                    fingerprint=drift_inbox_fingerprint(
                        fresh.ai_system_id, fresh.drift_class, fresh.metric_key
                    ),
                    title=(
                        f"Drift detected: {fresh.drift_class} on "
                        f"{fresh.metric_key}"
                    ),
                    evidence={"drift_event_id": fresh.id},
                )
            db.commit()
            considered += 1
            if plan.will_act:
                log.info(
                    "closed loop acted on %s (policy %s)", fresh.id, plan.policy_id
                )
        except Exception:
            db.rollback()
            log.exception("closed loop: finding %s crashed during response", event.id)
        finally:
            db.close()

    return considered


async def response_scheduler_loop() -> None:
    while True:
        try:
            # Leader election — this tick *actuates*: it flips gate
            # state and writes inbox events. Two workers ticking together
            # act on the same finding twice, so the operator sees the same
            # response recorded twice against one drift.
            await asyncio.to_thread(run_as_leader, "ai_system_response", run_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad tick kill the loop. A closed loop that
            # silently stopped running is the failure mode this whole phase
            # is built to avoid in the systems it watches; it would be
            # embarrassing to ship it here.
            log.exception("closed loop: tick crashed")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_response_scheduler() -> asyncio.Task:
    """Spawn the loop. Call once at FastAPI startup, on the lead instance."""
    return asyncio.create_task(
        response_scheduler_loop(), name="ai-system-response-scheduler"
    )


__all__ = [
    "MAX_PER_TICK",
    "TICK_INTERVAL_SECONDS",
    "response_scheduler_loop",
    "run_once",
    "start_response_scheduler",
]

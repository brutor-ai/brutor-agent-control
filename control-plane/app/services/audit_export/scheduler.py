# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Audit-export scheduler — background loop that ticks every minute and
fires any destination whose `last_run_at + interval_seconds` has passed.

# Design

A single asyncio task — `audit_export_scheduler_loop` — wakes up every
`TICK_INTERVAL_SECONDS` (60s), opens a short-lived DB session, finds
all enabled destinations whose due time has passed, and runs each one
sequentially via `AuditExportService.run_destination`.

# Why one task, not one task per destination

  * Destinations are typically tens, not thousands. Iterating a few
    rows per minute is cheap; the work each run does is bounded
    (50k rows max, gzipped JSONL).
  * One task means one set of logs and one place to look when the
    scheduler stops ticking. Per-destination tasks would race for
    DB connections and produce a confusing aggregate log.
  * Failures are isolated by destination (we catch broadly inside the
    loop), so a single bad webhook doesn't stop the others.

# Concurrency safety

Every process may start this loop; only one of them does the work per
tick. `run_as_leader` takes a Postgres advisory lock keyed on the
scheduler name for the duration of the tick and skips the tick when a
sibling holds it — see `app/services/scheduler_leader.py` for why the
lock is per-tick rather than a held lease. This covers both Gunicorn's
multiple workers within one container and multiple containers sharing
the database.

# Why not cron / APScheduler / Celery

Same reasoning as `lifecycle.py`'s scheduler: one Python task in the
same process is the lowest-deps option that matches our existing
patterns. Operators don't need another moving part.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from app.database import SessionLocal
from app.services.scheduler_leader import run_as_leader


log = logging.getLogger("brutor-audit-export-scheduler")


# How often to wake up and check for due destinations. One minute is
# the right granularity: destination intervals are minutes/hours, not
# seconds. Tighter ticks waste DB queries; looser ticks miss the
# operator's intent ("hourly" should mean within ~1m of the hour).
TICK_INTERVAL_SECONDS = 60


async def audit_export_scheduler_loop(
    encryption_service,
    tick_interval_seconds: int = TICK_INTERVAL_SECONDS,
) -> None:
    """The actual background loop. Long-running, never returns under
    normal operation.

    Args:
        encryption_service: Used to decrypt destination secrets when
            backends run. Constructed once at startup so we don't pay
            the PBKDF2 cost per tick.
        tick_interval_seconds: Override for tests; production should
            leave the default.
    """
    log.info(
        "Audit-export scheduler started — tick every %ds, "
        "destination minimum interval enforced by MIN_INTERVAL_SECONDS.",
        tick_interval_seconds,
    )

    while True:
        try:
            # Per-tick beacon. Stamped BEFORE the
            # actual work so a slow destination doesn't make the
            # badge flap. Best-effort: a beacon write failure logs
            # but never blocks the tick.
            await asyncio.to_thread(_write_beacon_safe)
            # Leader election (see app/services/scheduler_leader.py).
            # Gunicorn runs 2 workers by default and each one has its
            # own lifespan, so without this a single container exports
            # every destination twice per tick — duplicate objects in
            # the sink and a doubled row cursor.
            await asyncio.to_thread(
                run_as_leader,
                "audit_export",
                _run_due_destinations,
                encryption_service,
            )
        except Exception:
            # Defensive — `_run_due_destinations` already catches per-
            # destination failures and logs them. This handles
            # catastrophic failures (DB unreachable, OOM during query)
            # so the loop doesn't silently die.
            log.exception("Audit-export scheduler tick failed at the top level")
        await asyncio.sleep(tick_interval_seconds)


def _write_beacon_safe() -> None:
    """Write the per-tick beacon for the audit-export scheduler.

    Wraps the bare helper with a session + exception swallow so a
    DB blip can't kill the scheduler loop. The cost of a missed
    beacon is one flapping badge until the next tick; the cost of
    a crashed loop is destinations never running.
    """
    from app.services.system_ops.schedulers import write_beacon

    db = SessionLocal()
    try:
        write_beacon(db, "audit_export")
    except Exception:
        log.exception("Audit-export beacon write failed (non-fatal)")
    finally:
        db.close()


def _run_due_destinations(encryption_service) -> None:
    """One scheduler tick: synchronous, runs inside `asyncio.to_thread`
    so the SQLAlchemy session doesn't fight the event loop.

    Each tick:
      1. Opens a short-lived session.
      2. Computes the due set (enabled + due-time-elapsed).
      3. Runs each destination sequentially.
      4. Commits + closes.
    """
    # Lazy imports keep the scheduler module light at import time —
    # the heavy `AuditExportService` + ORM models only load when the
    # loop first ticks. Saves ~30ms at FastAPI startup.
    from app.database.models.audit_export_destination import (
        AuditExportDestination,
    )
    from app.services.audit_export.service import AuditExportService

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        due_ids = _fetch_due_destination_ids(db, now)
        if not due_ids:
            log.debug("Audit-export tick: no destinations due")
            return

        svc = AuditExportService(db=db, encryption_service=encryption_service)
        for dest_id in due_ids:
            try:
                result = svc.run_destination(dest_id)
                db.commit()  # commit per destination so failures don't roll back successes
                log.info(
                    "Audit-export %s → %s (rows=%d)",
                    dest_id, result.outcome.value, result.rows_exported,
                )
            except Exception:
                db.rollback()
                log.exception(
                    "Audit-export destination %s crashed during tick", dest_id,
                )
    finally:
        db.close()


def _fetch_due_destination_ids(db, now: datetime) -> List[str]:
    """Return IDs of all enabled destinations whose next-run time has
    passed.

    Rule: a destination is due iff
      enabled = TRUE
      AND (
        last_run_at IS NULL                              -- never ran
        OR last_run_at + interval_seconds <= now         -- next tick passed
      )

    We compute the comparison in Python (post-query) rather than in
    SQL because Postgres' `interval` arithmetic with a column on each
    side is awkward and we have <100 rows in practice; a full table
    scan filtered in Python is cheaper than a CASE expression."""
    from app.database.models.audit_export_destination import (
        AuditExportDestination,
    )

    rows = (
        db.query(AuditExportDestination)
        .filter(AuditExportDestination.enabled == True)  # noqa: E712
        .all()
    )
    due: List[str] = []
    for r in rows:
        if r.last_run_at is None:
            due.append(r.id)
            continue
        next_due = r.last_run_at + timedelta(seconds=r.interval_seconds)
        if next_due <= now:
            due.append(r.id)
    return due


def start_audit_export_scheduler(encryption_service) -> asyncio.Task:
    """Spawn the scheduler as a background task. Call once at FastAPI
    startup.

    Returns the asyncio task so callers can cancel it on shutdown
    (currently nobody does, but the hook exists for graceful exit
    when we move to FastAPI's lifespan API).
    """
    return asyncio.create_task(audit_export_scheduler_loop(encryption_service))

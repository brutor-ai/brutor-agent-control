# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Compliance evidence scheduler

One leader-elected tick every 15 minutes, each job fail-open and on its own
transaction (a failing job never stops the others; a crashing tick never
kills the loop — the contract every CP scheduler keeps):

* `incident_deadline` inbox items — deadlines due within 24 h or passed;
* `evidence_unwitnessed` and `capture_gap` inbox items;
* judged evidence — freeze rubrics, queue last week's blind sample,
  `judge_uncalibrated` / `calibration_stalled` (hourly);
* Evidence Reports — yesterday's daily, last week's weekly, last month's
  monthly per enabled framework (hourly; idempotent on the period key, so a
  tick after the period closed generates it once and every later tick finds
  it).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

from app.database import SessionLocal
from app.services.scheduler_leader import run_as_leader

log = logging.getLogger("brutor.compliance.scheduler")

TICK_INTERVAL_SECONDS = 900
#: Heavier jobs (resolver evaluation, sampling) run at most this often.
HOURLY = timedelta(hours=1)

_last_run: Dict[str, datetime] = {}


def _due(name: str, now: datetime, every: Optional[timedelta]) -> bool:
    if every is None:
        return True
    last = _last_run.get(name)
    return last is None or now - last >= every


def _job(name: str, fn: Callable, stats: Dict[str, int], now: datetime, every: Optional[timedelta] = None) -> None:
    if not _due(name, now, every):
        return
    db = SessionLocal()
    try:
        result = fn(db)
        db.commit()
        if isinstance(result, dict):
            for k, v in result.items():
                stats[f"{name}.{k}"] = int(v)
        else:
            stats[name] = int(result or 0)
        _last_run[name] = now
    except Exception:  # noqa: BLE001 — one job must not stop the rest
        db.rollback()
        log.exception("compliance scheduler job %s failed", name)
    finally:
        db.close()


def run_once(now: Optional[datetime] = None) -> Dict[str, int]:
    from app.services.compliance import evidence_findings, incidents, judged, reports

    now = now or datetime.now(timezone.utc)
    stats: Dict[str, int] = {}
    _job("incident_deadlines", lambda db: incidents.scan_deadlines(db, now=now), stats, now)
    _job("evidence_unwitnessed", lambda db: evidence_findings.scan_unwitnessed(db, now=now), stats, now)
    _job("capture_gap", lambda db: evidence_findings.scan_capture_gaps(db, now=now), stats, now)
    _job("judged", lambda db: judged.run_judged_cycle(db, now=now), stats, now, HOURLY)
    _job("reports", lambda db: reports.run_scheduled(db, today=now.date()), stats, now, HOURLY)
    return stats


async def compliance_loop() -> None:
    while True:
        try:
            stats = await asyncio.to_thread(run_as_leader, "compliance_evidence", run_once)
            if stats and any(stats.values()):
                log.info("compliance evidence tick: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("compliance evidence: tick crashed")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_compliance_scheduler() -> asyncio.Task:
    """Spawn the loop. Call once at FastAPI startup."""
    return asyncio.create_task(compliance_loop(), name="compliance-evidence-scheduler")


__all__ = ["run_once", "start_compliance_scheduler", "TICK_INTERVAL_SECONDS"]

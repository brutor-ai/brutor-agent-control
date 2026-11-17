# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Batch worker for Continuous Assurance Checks.

One 5-minute tick does two jobs:

* **Windowed tier-A thresholds** (`rate_over`): aggregate the per-run
  boolean results the Rust core already wrote, compute the true-rate over
  the spec's window, and raise a `check_breach` inbox item when the rate
  crosses the line. The window result is itself recorded as a result row
  (`window_start`/`window_end`) so the report can cite it.
* **Tier-B judge batches**: hand every enabled semantic check to the
  [`JudgeExecutor`].

Same loop contract as every CP scheduler: each tick in a worker thread
with its own session; a crashing tick never kills the loop.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.database import SessionLocal
from app.database.models.assurance_check import (
    CHECK_TIER_DETERMINISTIC,
    CHECK_TIER_SEMANTIC,
    THRESHOLD_KIND_RATE_OVER,
    AssuranceCheck,
    AssuranceCheckResult,
)
from app.services.assurance_checks.judge import JudgeExecutor, _value_breached
from app.services.assurance_inbox import AssuranceInboxService
from app.services.scheduler_leader import run_as_leader

log = logging.getLogger("brutor.assurance-checks")

TICK_INTERVAL_SECONDS = 300

_executor: Optional[JudgeExecutor] = None


def evaluate_rate_windows(db: Session, inbox: AssuranceInboxService) -> int:
    """Evaluate every enabled deterministic check with a rate_over
    threshold. Returns the number of breaches raised."""
    checks = (
        db.query(AssuranceCheck)
        .filter(
            AssuranceCheck.enabled.is_(True),
            AssuranceCheck.tier == CHECK_TIER_DETERMINISTIC,
        )
        .all()
    )
    breaches = 0
    now = datetime.now(timezone.utc)
    for check in checks:
        threshold = check.threshold or {}
        if threshold.get("kind") != THRESHOLD_KIND_RATE_OVER:
            continue
        window_hours = int(threshold.get("window_hours") or 24)
        window_start = now - timedelta(hours=window_hours)

        # Column-only scan, counted in Python: a JSON-boolean SQL filter
        # is not portable across PG and the SQLite test harness, and a
        # window holds at most a few days of one check's results.
        values = (
            db.query(AssuranceCheckResult.value)
            .filter(
                AssuranceCheckResult.check_id == check.id,
                AssuranceCheckResult.check_version == check.version,
                AssuranceCheckResult.backtest.is_(False),
                AssuranceCheckResult.run_id.isnot(None),
                AssuranceCheckResult.evaluated_at >= window_start,
            )
            .yield_per(1000)
        )
        total = 0
        hits = 0
        for (value,) in values:
            total += 1
            if isinstance(value, dict) and value.get("value") is True:
                hits += 1
        if total == 0:
            continue
        rate = hits / total
        breached = _value_breached(
            {"op": threshold.get("op", "gt"), "value": threshold.get("value", 0)},
            rate,
        )
        db.add(
            AssuranceCheckResult(
                id=f"ckr-{ULID()}",
                tenant_id=check.tenant_id,
                check_id=check.id,
                check_version=check.version,
                run_id=None,
                window_start=window_start,
                window_end=now,
                value={"value": rate, "hits": hits, "total": total},
                evidence={"window_hours": window_hours},
                evaluator_fingerprint=check.spec_hash,
                backtest=False,
            )
        )
        if breached and check.route_to_inbox:
            breaches += 1
            inbox.raise_item(
                db,
                tenant_id=check.tenant_id,
                category="check_breach",
                severity=threshold.get("severity", "warning"),
                fingerprint=f"check_breach:{check.id}:v{check.version}:rate",
                title=(
                    f"Check rate breached: {check.name} "
                    f"({hits}/{total} over {window_hours}h)"
                ),
                evidence={
                    "check_id": check.id,
                    "check_version": check.version,
                    "rate": rate,
                    "hits": hits,
                    "total": total,
                    "window_hours": window_hours,
                },
            )
        db.commit()
    return breaches


def run_once() -> Dict[str, int]:
    """One tick. Returns counters for tests and the log line."""
    stats = {
        "rate_breaches": 0, "judged": 0, "judge_failed": 0,
        "composition_raised": 0, "composition_resolved": 0,
        "agent_raised": 0, "agent_resolved": 0, "agent_release_events": 0,
    }
    db = SessionLocal()
    try:
        inbox = AssuranceInboxService()
        stats["rate_breaches"] = evaluate_rate_windows(db, inbox)

        # Structural composition checks. Their own guard:
        # a composition failure must not stop the judge batches below.
        try:
            from app.services.assurance_checks.composition import (
                run_composition_checks_all,
            )

            comp = run_composition_checks_all(db)
            stats["composition_raised"] = comp["raised"]
            stats["composition_resolved"] = comp["resolved"]
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("assurance checks: composition checks crashed")

        # Agent implementation checks and new-release
        # lifecycle events. Their own guard, like the composition checks.
        try:
            from app.services.assurance_checks.agent import run_agent_checks_all

            agent = run_agent_checks_all(db)
            stats["agent_raised"] = agent["raised"]
            stats["agent_resolved"] = agent["resolved"]
            stats["agent_release_events"] = agent["release_events"]
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("assurance checks: agent checks crashed")

        if _executor is not None:
            semantic = (
                db.query(AssuranceCheck)
                .filter(
                    AssuranceCheck.enabled.is_(True),
                    AssuranceCheck.tier == CHECK_TIER_SEMANTIC,
                )
                .all()
            )
            for check in semantic:
                try:
                    check_stats = _executor.run_check(db, check)
                    stats["judged"] += check_stats.get("judged", 0)
                    stats["judge_failed"] += check_stats.get("failed", 0)
                except Exception:  # noqa: BLE001 — one check must not stop the rest
                    log.exception(
                        "assurance checks: judge batch crashed for %s", check.id
                    )
    finally:
        db.close()
    return stats


async def assurance_checks_loop() -> None:
    while True:
        try:
            # Leader election — a tier-B check spends judge budget and
            # records a check result per run; a duplicate tick bills the
            # tenant twice for the same evidence.
            stats = await asyncio.to_thread(
                run_as_leader, "assurance_checks", run_once
            )
            if stats and any(stats.values()):
                log.info("assurance checks tick: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("assurance checks: tick crashed")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_assurance_checks_scheduler(logger) -> asyncio.Task:
    """Spawn the loop. Call once at FastAPI startup, on the lead instance."""
    global _executor
    _executor = JudgeExecutor(logger)
    return asyncio.create_task(
        assurance_checks_loop(), name="assurance-checks-scheduler"
    )

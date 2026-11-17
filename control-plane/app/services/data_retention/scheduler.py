# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Retention scheduler: every hour, sweep every tenant that has a policy.

Same shape as the other control-plane loops (digest, checks): one asyncio
task, the tick runs in a thread, one tenant's failure is logged and does
not stop the rest. Every process starts the loop; only the worker that
wins the per-tick advisory lock actually sweeps (see
`app/services/scheduler_leader.py`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from ulid import ULID

from app.database import SessionLocal
from app.database.models.system_alert import SystemAlert
from app.database.models.tenant import Tenant
from app.services.data_retention.policy import effective_policy
from app.services.data_retention.sweeper import SweepResult, sweep_tenant
from app.services.scheduler_leader import run_as_leader

log = logging.getLogger("brutor.retention")

TICK_INTERVAL_SECONDS = 3600


def run_once(*, now: Optional[datetime] = None) -> list[SweepResult]:
    """One tick across all tenants. Returns per-tenant results."""
    now = now or datetime.now(timezone.utc)
    db = SessionLocal()
    results: list[SweepResult] = []
    try:
        tenants = db.query(Tenant).filter(Tenant.data_retention.isnot(None)).all()
        for tenant in tenants:
            policy = effective_policy(tenant)
            if policy is None:
                continue
            # Framework retention floors: never prune ledger
            # rows or runs younger than the largest floor of any of the
            # tenant's AI Systems (EU AI Act high-risk 183 d, HIPAA 6 y …).
            try:
                from app.services.compliance.retention_floors import effective_tenant_policy

                policy = effective_tenant_policy(db, tenant) or policy
            except Exception:  # noqa: BLE001 — a floor lookup must not stop the sweep
                log.exception(
                    "retention: floor composition failed for tenant %s — "
                    "skipping the sweep rather than pruning below a floor",
                    tenant.id,
                )
                continue
            result = sweep_tenant(db, tenant, policy, now=now)
            results.append(result)
            if result.unconverged:
                # The tenant is writing faster than the sweeper drains.
                # Left silent this reads as a healthy sweep with a big row
                # count, while the declared retention window is not being
                # met — so it is raised, not just logged.
                log.warning(
                    "retention sweep did not converge tenant=%s passes=%s",
                    tenant.id,
                    ",".join(result.unconverged),
                )
                _raise_backlog_alert(db, tenant, result.unconverged)
            if result.touched or result.errors:
                log.info(
                    "retention sweep tenant=%s bodies=%d batch_bodies=%d logs=%d "
                    "runs=%d feedback=%d prunes=%d errors=%s",
                    tenant.id,
                    result.bodies_scrubbed,
                    result.batch_bodies_scrubbed,
                    result.logs_deleted,
                    result.runs_deleted,
                    result.feedback_deleted,
                    result.prune_records,
                    result.errors or "none",
                )
    finally:
        db.close()
    return results


def _raise_backlog_alert(db, tenant, passes: list[str]) -> None:
    """Raise/refresh a `resource_health` alert for a tenant that cannot drain.

    Deduped per tenant, so a backlog that persists across ticks bumps
    `occurrence_count` rather than filling the console. Never raises: a
    reporting failure must not stop the remaining tenants' sweeps.
    """
    dedup_key = f"{tenant.id}|resource_health|retention.sweeper|backlog"
    try:
        existing = (
            db.query(SystemAlert)
            .filter(
                SystemAlert.dedup_key == dedup_key,
                SystemAlert.status.in_(("active", "acknowledged")),
            )
            .first()
        )
        message = (
            "The retention sweeper spent its per-tick budget with rows still "
            f"older than the declared window ({', '.join(passes)}). This tenant "
            "is producing rows faster than they are being removed, so the "
            "retention policy shown on the data-handling page is not the policy "
            "in force. Raise the batch budget or shorten the window."
        )
        if existing is not None:
            existing.last_seen_at = datetime.now(timezone.utc)
            existing.occurrence_count = (existing.occurrence_count or 0) + 1
            existing.message = message
            existing.context = {"passes": passes}
        else:
            db.add(
                SystemAlert(
                    id=f"sysalert-{ULID()}",
                    tenant_id=tenant.id,
                    severity="high",
                    category="resource_health",
                    source="retention.sweeper",
                    resource_type="tenant",
                    resource_id=tenant.id,
                    resource_name=tenant.name,
                    error_code="retention_backlog",
                    title="Retention sweep is not keeping up",
                    message=message,
                    context={"passes": passes},
                    status="active",
                    dedup_key=dedup_key,
                )
            )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("retention: could not raise backlog alert for %s", tenant.id)


async def retention_loop() -> None:
    while True:
        try:
            # One sweeper per tick across the whole deployment. Two
            # workers sweeping the same tenant concurrently interleave
            # their batches: both read the same `BATCH_SIZE` window and
            # one of them deletes rows the other has already deleted,
            # which halves effective drain rate rather than doubling it.
            await asyncio.to_thread(run_as_leader, "data_retention", run_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("retention: tick crashed")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


def start_retention_scheduler() -> asyncio.Task:
    """Spawn the loop. Call once at FastAPI startup, on the lead instance."""
    return asyncio.create_task(retention_loop(), name="data-retention-scheduler")

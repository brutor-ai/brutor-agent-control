# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM model retirement warner.

# What this does

Walks all `llm_model` rows that carry a `retire_date` and emits
operator-facing `system_alerts` rows when:

  1. **Reminder fired** (`retirement_reminder_date <= today` AND
     `retire_date > today` AND `enabled = true`) — severity = `medium`.
     This is the early-warning so the operator can:
       - Configure the recommended replacement
       - Tell downstream users about the upcoming change
       - Update their resource group bindings

  2. **Already retired** (`retire_date <= today` AND `enabled = true`)
     — severity = `high`. The model is calling out to a provider
     endpoint that will return 404 / 401 / silently degrade. Operators
     should disable the model row and migrate users immediately.

# Why this exists

Providers retire models on a fixed schedule (OpenAI retired DALL-E 3
in May 2026, Anthropic retired Claude Sonnet 4 in June 2026, etc.).
Without proactive operator warnings, the first signal a tenant gets is
a flood of 401/404 errors from end-users — which is not the experience
a platform aiming for production-grade reliability should ship.

The warner runs on the same 60-second tick as the audit-export
scheduler. Dedup is via SystemAlert.compute_dedup_key — the same
(tenant, model_id) pair can't emit twice while the previous alert is
still `active` or `acknowledged`. Once resolved, a new occurrence
re-arms.

# Operational notes

* Idempotent — calling `run_once()` repeatedly is safe.
* No-op when the schema migration hasn't been applied — defensively
  catches `UndefinedColumn` so an old control-plane container booting
  against a new database (or vice-versa) doesn't crash on the first
  scheduler tick.
* Reads from the tenants table to surface tenant_id even for
  system-wide models; if the model row has tenant_id="default" or
  similar, that's what lands in the alert. Operators on the
  per-tenant alerts dashboard see only their own retirements.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import List

from sqlalchemy import and_, or_
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.llm_model import LLModel
from app.database.models.system_alert import SystemAlert
from app.services.llm import availability


_SOURCE = "llm_model.retirement_warner"
_RESOURCE_TYPE = "llm_model"


def _build_warning_alert(model: LLModel, today: date) -> SystemAlert:
    """Build (but don't persist) the SystemAlert row for an upcoming
    retirement. Severity is `medium` because the operator still has
    time to react — disable the model, configure a replacement, tell
    users. After `retire_date` we raise severity to `high` via
    `_build_retired_alert` below."""
    days_remaining = (model.retire_date - today).days
    replacement = model.retirement_replacement_model_name
    title = f"LLM model '{model.name}' retires in {days_remaining} day(s)"
    message_lines = [
        f"Model **{model.name}** ({model.model_name}) is scheduled to "
        f"retire on {model.retire_date.isoformat()}.",
        "",
        f"That's **{days_remaining} day(s)** from today. After the "
        "retirement date the provider will reject requests to this "
        "model — switch your tenants over to the recommended "
        "replacement before then.",
    ]
    if replacement:
        rp_provider = (
            model.retirement_replacement_provider_key
            or (model.provider.key if model.provider else "")
        )
        provider_clause = f" (provider: `{rp_provider}`)" if rp_provider else ""
        message_lines += [
            "",
            f"**Recommended replacement:** `{replacement}`{provider_clause}.",
        ]
    if model.retirement_notes:
        message_lines += ["", "**Provider notes:**", model.retirement_notes]
    message = "\n".join(message_lines)

    error_code = f"retirement_warning:{model.id}"
    dedup_key = SystemAlert.compute_dedup_key(
        tenant_id=model.tenant_id,
        category="resource_health",
        source=_SOURCE,
        error_code=error_code,
    )

    return SystemAlert(
        id=f"sysalert-{ULID()}",
        tenant_id=model.tenant_id,
        severity="medium",
        category="resource_health",
        source=_SOURCE,
        resource_type=_RESOURCE_TYPE,
        resource_id=model.id,
        resource_name=model.name,
        error_code=error_code,
        title=title,
        message=message,
        context={
            "kind": "retirement_warning",
            "model_id": model.id,
            "model_name": model.model_name,
            "retire_date": model.retire_date.isoformat(),
            "days_remaining": days_remaining,
            "replacement_model_name": replacement,
            "replacement_provider_key": model.retirement_replacement_provider_key,
        },
        status="active",
        dedup_key=dedup_key,
    )


def _build_retired_alert(model: LLModel, today: date) -> SystemAlert:
    """The model's retirement date is in the past but the row is
    still enabled. Higher severity — every user call is now hitting
    a dead endpoint. Operators should disable the model and migrate
    users immediately."""
    days_overdue = (today - model.retire_date).days
    replacement = model.retirement_replacement_model_name
    title = (
        f"LLM model '{model.name}' retired {days_overdue} day(s) ago "
        "and is still enabled"
    )
    message_lines = [
        f"Model **{model.name}** ({model.model_name}) was retired by "
        f"the provider on {model.retire_date.isoformat()} — that's "
        f"**{days_overdue} day(s) ago**.",
        "",
        "Requests to this model will now fail at the provider. "
        "Disable the row in Admin Console → AI Models, and migrate "
        "any tenants that depended on it.",
    ]
    if replacement:
        rp_provider = (
            model.retirement_replacement_provider_key
            or (model.provider.key if model.provider else "")
        )
        provider_clause = f" (provider: `{rp_provider}`)" if rp_provider else ""
        message_lines += [
            "",
            f"**Recommended replacement:** `{replacement}`{provider_clause}.",
        ]
    if model.retirement_notes:
        message_lines += ["", "**Provider notes:**", model.retirement_notes]
    message = "\n".join(message_lines)

    error_code = f"retirement_overdue:{model.id}"
    dedup_key = SystemAlert.compute_dedup_key(
        tenant_id=model.tenant_id,
        category="resource_health",
        source=_SOURCE,
        error_code=error_code,
    )

    return SystemAlert(
        id=f"sysalert-{ULID()}",
        tenant_id=model.tenant_id,
        severity="high",
        category="resource_health",
        source=_SOURCE,
        resource_type=_RESOURCE_TYPE,
        resource_id=model.id,
        resource_name=model.name,
        error_code=error_code,
        title=title,
        message=message,
        context={
            "kind": "retirement_overdue",
            "model_id": model.id,
            "model_name": model.model_name,
            "retire_date": model.retire_date.isoformat(),
            "days_overdue": days_overdue,
            "replacement_model_name": replacement,
            "replacement_provider_key": model.retirement_replacement_provider_key,
        },
        status="active",
        dedup_key=dedup_key,
    )


def run_once(
    db: Session,
    *,
    today: date | None = None,
    logger: BrutorLogger | None = None,
) -> List[SystemAlert]:
    """Single sweep across all enabled models with a retirement
    schedule. Returns the list of alerts INSERTED (existing
    active/acknowledged alerts are skipped via the partial unique
    index on `dedup_key`).

    Caller is responsible for the transaction. We `db.add()` +
    `db.flush()` for each new alert so the unique-index conflict
    surfaces synchronously; the surrounding scheduler decides
    whether to commit per-alert or per-sweep.

    `today` defaults to the UTC day: the gateway stops calling a model on
    its retire date in UTC (`availability`), and "already retired" says so."""
    log = logger or BrutorLogger("llm-retirement-warner")
    today = today or availability._today()

    try:
        models = (
            db.query(LLModel)
            .filter(LLModel.enabled.is_(True))
            .filter(LLModel.retire_date.isnot(None))
            .filter(
                or_(
                    # Warning window: reminder has fired but retirement
                    # isn't here yet.
                    and_(
                        LLModel.retirement_reminder_date <= today,
                        LLModel.retire_date > today,
                    ),
                    # Overdue window: retirement is in the past.
                    LLModel.retire_date <= today,
                )
            )
            .all()
        )
    except (ProgrammingError, OperationalError) as exc:
        # Schema migration hasn't landed yet — bail gracefully.
        log.warning(
            "llm_model retirement columns not present yet; skipping sweep",
            context={"error": str(exc)},
        )
        return []

    inserted: List[SystemAlert] = []
    for model in models:
        if model.retire_date is None:
            continue  # shouldn't happen — filter above — defensive
        if model.retire_date <= today:
            alert = _build_retired_alert(model, today)
        else:
            alert = _build_warning_alert(model, today)

        # Idempotent persist. PRE-CHECK against the partial-unique scope
        # (`status IN ('active','acknowledged')`, matching the
        # `uq_system_alerts_active_dedup` index predicate) BEFORE inserting.
        # The previous version always issued the INSERT and relied on the
        # unique-index conflict to no-op it — but that made Postgres log an
        # ERROR ("duplicate key value violates uq_system_alerts_active_dedup")
        # for every still-retired model on every sweep, even though the app
        # caught it. On a recurrence we now bump `occurrence_count` +
        # `last_seen_at` on the existing row (the documented intent — a
        # recurrence signal) instead of re-inserting.
        existing = (
            db.query(SystemAlert)
            .filter(
                SystemAlert.dedup_key == alert.dedup_key,
                SystemAlert.status.in_(("active", "acknowledged")),
            )
            .first()
        )
        if existing is not None:
            existing.occurrence_count = (existing.occurrence_count or 0) + 1
            existing.last_seen_at = datetime.now(timezone.utc)
            continue

        # New alert. Keep the SAVEPOINT + catch purely as a race guard now
        # (a concurrent sweep inserting the same dedup_key between our
        # pre-check and flush) — the common "already active" path no longer
        # reaches the failing INSERT, so this branch is rare and quiet.
        try:
            with db.begin_nested():
                db.add(alert)
                db.flush()
            inserted.append(alert)
        except Exception as e:  # noqa: BLE001 — concurrent-sweep race on the partial-unique index
            log.debug(
                "Retirement alert already active for model; skipping",
                context={
                    "model_id": model.id,
                    "tenant_id": model.tenant_id,
                    "reason": type(e).__name__,
                },
                exc_info=False,
            )

    if inserted:
        log.info(
            "Retirement sweep emitted alerts",
            context={"count": len(inserted)},
        )
    return inserted


# ---------------------------------------------------------------------------
# Scheduler entry point — mirrors the audit-export scheduler shape.
# ---------------------------------------------------------------------------

_DEFAULT_INTERVAL_SECONDS = 60


async def _scheduler_loop(db_factory, interval_seconds: int) -> None:
    log = BrutorLogger("llm-retirement-warner-scheduler")
    log.info(
        "Retirement-warner scheduler started",
        context={"interval_seconds": interval_seconds},
    )
    # Lazy import — keeps the module's startup-time graph small. The
    # write_beacon helper only loads on the first tick.
    from app.services.scheduler_leader import scheduler_lease
    from app.services.system_ops.schedulers import write_beacon

    while True:
        try:
            db = db_factory()
            try:
                # Per-tick beacon. Stamped first so
                # the badge updates even when run_once() takes a
                # while. Best-effort: failures are logged and
                # swallowed so the sweep still runs.
                try:
                    write_beacon(db, "llm_retirement_warner")
                except Exception:  # noqa: BLE001
                    log.warning(
                        "Retirement-warner beacon write failed (non-fatal)",
                        exc_info=True,
                    )
                # Leader election — `run_once` inserts system_alerts
                # after a check-then-insert on the existing alert, so
                # two workers ticking together raise the same
                # retirement warning twice in the operator's console.
                with scheduler_lease("llm_retirement_warner") as leader:
                    if leader:
                        run_once(db, logger=log)
                        db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 — must not kill the loop
            log.error(
                "Retirement-warner sweep failed",
                context={"error": str(e)},
                exc_info=True,
            )
        await asyncio.sleep(interval_seconds)


def start_retirement_warner_scheduler(
    db_factory,
    *,
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
) -> asyncio.Task:
    """Schedule the warner loop on the current asyncio event loop.
    Returns the task handle so the lifespan handler can cancel it
    on shutdown if needed. `db_factory` is the SessionLocal callable
    — pass-through pattern matches `start_metadata_refresher`."""
    loop = asyncio.get_event_loop()
    return loop.create_task(_scheduler_loop(db_factory, interval_seconds))


__all__ = [
    "run_once",
    "start_retirement_warner_scheduler",
]

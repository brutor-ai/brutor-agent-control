# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Startup and shutdown of the control plane, as ordered lists of named steps.

`STARTUP` is everything the application does before it serves requests, in
order: configuration and database first, then the background schedulers, then
the Redis publisher. `SHUTDOWN` is what it does on the way out. The lifespan
in `app/app.py` runs the two lists; it holds no step of its own.

A step is a function of the `StartupContext`. Each step keeps its own error
handling, because the steps differ on purpose:

  - the configuration check and the database must succeed, so they raise and
    the application does not start;
  - a preflight only informs the operator, so its failure is logged quietly;
  - a scheduler that fails to start is logged as an error naming what will not
    run until restart, and startup continues.

A step that raises stops the startup.

These are the Community Edition's steps: none names a
feature. The Enterprise Edition's steps are its extension's, each tagged with
its feature (`LifecycleStep.feature`); they run after these at start-up and
before these at shutdown.

Note on horizontal scaling: every process starts its own copy of each
scheduler loop, but the ticks are gated by a per-scheduler Postgres advisory
lock (`app/services/scheduler_leader.py`), so exactly one worker does the work
per tick. That matters within a single container too, not only across
replicas: `gunicorn.conf.py` defaults to 2 workers and each one runs the
lifespan. No dedicated scheduler container is needed.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, FrozenSet, Optional, Sequence, Tuple

from fastapi import FastAPI

from app.composition.services import Services
from app.core.logging_system import BrutorLogger
from app.database import SessionLocal, init_database
from app.env_vars import REDIS_URL


@dataclass(frozen=True)
class StartupContext:
    """What a startup or shutdown step works with.

    `enterprise_features` is the Enterprise Edition features the application
    was composed of: empty for a Community composition, None
    when the caller did not compose one (the process-wide composition is
    read then).
    """

    app: FastAPI
    services: Services
    logger: BrutorLogger
    enterprise_features: Optional[FrozenSet[str]] = None


@dataclass(frozen=True)
class LifecycleStep:
    """One named step of startup or shutdown.

    `run` may be a plain function or a coroutine function. `feature` is the
    Enterprise Edition feature the step belongs to, or None for Community.
    """

    name: str
    run: Callable[[StartupContext], Optional[Awaitable[None]]]
    feature: Optional[str] = None


async def run_steps(context: StartupContext, steps: Sequence[LifecycleStep]) -> None:
    """Run the steps in order. A step that raises stops the run."""
    for step in steps:
        result = step.run(context)
        if inspect.isawaitable(result):
            await result


def _init_database(context: StartupContext) -> None:
    logger = context.logger

    # Initialize database tables and seed initial data. The edition check
    # runs in there, between the schema and the seed: an
    # Enterprise composition writes the edition marker, a Community one
    # refuses a database Enterprise has used, and the refusal stops the start
    # here like any other database failure.
    init_database(enterprise_features=context.enterprise_features)
    logger.info("Database initialized")


def _secret_preflight(context: StartupContext) -> None:
    logger = context.logger
    encryption_service = context.services.encryption_service

    # Secret-consistency preflight — non-fatal. Catches the
    # "reused an old Postgres volume with a new trial download"
    # trap: each download mints unique ENCRYPTION_KEY /
    # BRUTOR_GATEWAY_SERVICE_API_KEY, so data encrypted/hashed under
    # the old secrets stops validating (HMAC failures + opaque
    # 401s). Surface the real cause + fix on the first page of
    # logs instead of leaving the operator to decode per-request
    # errors.
    try:
        from app.services.secret_preflight import (
            run_secret_consistency_preflight,
        )
        _pf_db = SessionLocal()
        try:
            run_secret_consistency_preflight(_pf_db, encryption_service, logger)
        finally:
            _pf_db.close()
    except Exception as e:  # noqa: BLE001 — preflight must never crash startup
        logger.debug(
            "Secret-consistency preflight failed — continuing",
            context={"error": str(e)},
        )



def _log_ready(context: StartupContext) -> None:
    logger = context.logger
    config_manager = context.services.config_manager

    logger.info("Control plane ready", context={
        "server": config_manager.get_listen_addr(),
        "database": config_manager.get_database_url().split('@')[-1] if '@' in config_manager.get_database_url() else "local",
    })


def _start_audit_export(context: StartupContext) -> None:
    logger = context.logger
    encryption_service = context.services.encryption_service

    # Audit-export scheduler. Background loop that ticks every
    # minute, exporting proxy_logs to whatever sinks each
    # tenant has configured. Started after the application is
    # ready, so a misbehaving destination can't block the
    # control plane from coming up (the loop itself catches
    # broadly per destination — see scheduler.py for the
    # error-isolation rationale).
    try:
        from app.services.audit_export import (
            start_audit_export_scheduler,
        )
        start_audit_export_scheduler(encryption_service)
        logger.info("Audit-export scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start audit-export scheduler — "
            "configured destinations will NOT run until restart",
            context={"error": str(e)},
            exc_info=True,
        )


def _start_ai_system_response(context: StartupContext) -> None:
    logger = context.logger

    # The closed loop. Detection runs in
    # gateway-core next to the traffic; the *response* runs here
    # because it writes configuration, and config writes belong to
    # the control plane where the ORM owns column constraints.
    try:
        from app.services.ai_system_response.scheduler import (
            start_response_scheduler,
        )
        start_response_scheduler()
        logger.info("AI System response scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start AI System response scheduler — drift "
            "findings will be recorded but NOT acted on until restart",
            context={"error": str(e)},
            exc_info=True,
        )


def _start_assurance(context: StartupContext) -> None:
    logger = context.logger
    encryption_service = context.services.encryption_service

    # Scheduled assurance capture. Continuous
    # post-market monitoring evidence, produced automatically rather
    # than by an operator remembering to click. Off unless the tenant
    # sets an interval.
    try:
        from app.services.ai_system_response.assurance_scheduler import (
            start_assurance_scheduler,
        )
        start_assurance_scheduler()
        # Assurance notification dispatcher: tails
        # assurance_inbox_events and delivers real-time alerts to the
        # tenant's Slack/webhook channels. Alerts are a projection of
        # the inbox; the loop is fail-open by construction.
        from app.services.notification_dispatcher import (
            start_notification_dispatcher,
        )
        start_notification_dispatcher(encryption_service)
        logger.info("Assurance notification dispatcher started")
        logger.info("AI System assurance scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start AI System assurance scheduler — health "
            "tiles and scheduled evidence will be stale until restart: "
            f"{e}"
        )


def _start_assurance_checks(context: StartupContext) -> None:
    logger = context.logger

    # Batch worker for Continuous Assurance
    # Checks: windowed rate thresholds + tier-B judge batches.
    # Separate try block: a checks-worker failure must not take the
    # assurance scheduler down with it (or vice versa).
    try:
        from app.services.assurance_checks.scheduler import (
            start_assurance_checks_scheduler,
        )
        start_assurance_checks_scheduler(logger)
        logger.info("Assurance checks scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start assurance checks scheduler — windowed "
            "thresholds and semantic checks will not run until restart",
            context={"error": str(e)},
            exc_info=True,
        )


def _start_compliance(context: StartupContext) -> None:
    logger = context.logger

    # Incident deadlines, evidence findings
    # (unwitnessed heads, capture gaps), judged-evidence sampling and
    # calibration, and the daily/weekly/monthly Evidence Reports.
    try:
        from app.services.compliance.scheduler import (
            start_compliance_scheduler,
        )
        start_compliance_scheduler()
        logger.info("Compliance evidence scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start compliance evidence scheduler — reports, "
            "incident deadlines and evidence findings will not run until restart",
            context={"error": str(e)},
            exc_info=True,
        )


def _start_assurance_digest(context: StartupContext) -> None:
    logger = context.logger
    encryption_service = context.services.encryption_service

    # Weekly digest generation + channel delivery.
    try:
        from app.services.assurance_digest_scheduler import (
            start_assurance_digest_scheduler,
        )
        start_assurance_digest_scheduler(encryption_service)
        logger.info("Assurance digest scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start assurance digest scheduler — weekly "
            "digests will not be generated until restart",
            context={"error": str(e)},
            exc_info=True,
        )



def _start_data_retention(context: StartupContext) -> None:
    logger = context.logger
    app = context.app

    # Data retention sweeper. Hourly; per-tenant policy on
    # tenants.data_retention. Deletions are declared to the audit
    # chain, so a failure here costs nothing but delay.
    try:
        from app.services.data_retention.scheduler import (
            start_retention_scheduler,
        )
        app.state.retention_task = start_retention_scheduler()
        logger.info("Data-retention scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start data-retention scheduler — retention "
            "policies will NOT be enforced until restart",
            context={"error": str(e)},
            exc_info=True,
        )


def _start_retirement_warner(context: StartupContext) -> None:
    logger = context.logger

    # LLM model retirement warner. Background loop that ticks
    # every minute, finds enabled `llm_model` rows whose
    # `retirement_reminder_date <= today` or `retire_date <=
    # today`, and emits operator-facing system_alerts.
    # No-op when no models have a retirement schedule —
    # the query filters on `retire_date IS NOT NULL` (which
    # is backed by a partial index from migration `a050_llm_model_retirement_columns`).
    try:
        from app.services.llm.retirement_warner import (
            start_retirement_warner_scheduler,
        )
        start_retirement_warner_scheduler(db_factory=SessionLocal)
        logger.info("LLM retirement warner started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start LLM retirement warner — "
            "operators will NOT see proactive retirement "
            "warnings until restart",
            context={"error": str(e)},
            exc_info=True,
        )


def _start_skill_source_sync(context: StartupContext) -> None:
    logger = context.logger

    # Skill source sync scheduler. Background loop that
    # polls SkillSource rows for due syncs, walks their
    # configured GitHub repos, and feeds discovered
    # SKILL.md / scripts/ / references/ / assets/ into the
    # `_ingest_discovered_skills` reconciler. The legacy
    # Rust implementation in gateway-core has been retired —
    # config writes belong in the control plane so the
    # SQLAlchemy ORM owns column constraints (enums,
    # defaults, varchar caps) rather than being re-derived
    # by hand in raw SQL.
    try:
        from app.services.skill_source_sync import (
            start_skill_source_sync_scheduler,
        )
        start_skill_source_sync_scheduler()
        logger.info("Skill source sync scheduler started")
    except Exception as e:  # noqa: BLE001 — must not crash boot
        logger.error(
            "Failed to start skill source sync scheduler — "
            "configured SkillSource rows will NOT auto-sync "
            "until restart",
            context={"error": str(e)},
            exc_info=True,
        )



async def _connect_cache_invalidation_publisher(context: StartupContext) -> None:
    logger = context.logger
    app = context.app

    # Redis pub/sub client for cross-process cache invalidation.
    # The control plane PUBLISHES invalidation messages on
    # `brutor:cache:invalidate`; every gateway-core replica
    # subscribes and evicts the matching entries — config caches
    # AND the LLM response/semantic cache. Without this client,
    # `publish_invalidation` (app/services/cache_invalidation.py)
    # reads `app.state.redis` as None and silently no-ops, so admin
    # actions like the resource-group "Clear Cache" button return
    # 204 success while the proxy keeps serving cached responses
    # until their TTL. Non-fatal: when REDIS_URL is unset or Redis
    # is unreachable, push invalidation degrades to the proxy's
    # TTL-based refresh (config ~30s; response cache hours).
    #
    # The client is created lazily and kept even when Redis is not
    # reachable yet: redis.asyncio connects on first command and each
    # publish reconnects (bounded timeouts, see
    # create_invalidation_publisher). Gating `app.state.redis` on a
    # boot-time ping made a transient startup race (0.11.25 trial:
    # CP up before Redis) disable push invalidation until restart.
    # The ping below is for the boot log line only.
    app.state.redis = None
    try:
        redis_url = REDIS_URL.exact()
        if redis_url:
            from app.services.cache_invalidation import (
                create_invalidation_publisher,
                probe_invalidation_publisher,
            )
            app.state.redis = create_invalidation_publisher(redis_url)
            probe_error = await probe_invalidation_publisher(app.state.redis)
            if probe_error is None:
                logger.info("Redis cache-invalidation publisher connected")
            else:
                # Expected, handled degradation — a clean one-liner,
                # NOT a stack trace (exc_info=False; the structured
                # logger would otherwise auto-capture). Logged once
                # for this outage; recovery is logged at INFO by the
                # publisher on the first successful publish.
                logger.warning(
                    "Redis cache-invalidation publisher not reachable "
                    "at startup — will reconnect on next publish; "
                    "until then proxy TTL refresh applies",
                    context={"error": probe_error},
                    exc_info=False,
                )
        else:
            logger.info(
                "REDIS_URL unset — cache invalidation degrades to "
                "proxy TTL refresh (no instant push)"
            )
    except Exception as e:  # noqa: BLE001 — must not crash boot
        # Only a malformed REDIS_URL / missing redis package lands
        # here (client construction does no I/O) — that one is a
        # configuration error, not a transient outage.
        logger.warning(
            "Redis cache-invalidation publisher could not be created — "
            "admin cache-clear / config-change push disabled; proxy "
            "TTL refresh still applies",
            context={"error": str(e)},
            exc_info=False,
        )


async def _close_cache_invalidation_publisher(context: StartupContext) -> None:
    app = context.app

    _redis_client = getattr(app.state, "redis", None)
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:  # noqa: BLE001 — best-effort close
            pass


STARTUP: Tuple[LifecycleStep, ...] = (
    LifecycleStep("database", _init_database),
    LifecycleStep("secret_preflight", _secret_preflight),
    LifecycleStep("ready", _log_ready),
    LifecycleStep("audit_export_scheduler", _start_audit_export),
    LifecycleStep("ai_system_response_scheduler", _start_ai_system_response),
    LifecycleStep("assurance_scheduler", _start_assurance),
    LifecycleStep("assurance_checks_scheduler", _start_assurance_checks),
    LifecycleStep("compliance_scheduler", _start_compliance),
    LifecycleStep("assurance_digest_scheduler", _start_assurance_digest),
    LifecycleStep("data_retention_scheduler", _start_data_retention),
    LifecycleStep("llm_retirement_warner", _start_retirement_warner),
    LifecycleStep("skill_source_sync_scheduler", _start_skill_source_sync),
    LifecycleStep("cache_invalidation_publisher", _connect_cache_invalidation_publisher),
)

SHUTDOWN: Tuple[LifecycleStep, ...] = (
    LifecycleStep("cache_invalidation_publisher", _close_cache_invalidation_publisher),
)

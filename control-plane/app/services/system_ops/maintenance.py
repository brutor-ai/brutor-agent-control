# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""System Operations — Maintenance backend.

Four operator-facing entry points live here:

  * ``get_maintenance``        — current per-tenant state read.
  * ``set_maintenance``        — toggle off / read_only / drain / paused.
                                 Writes audit + publishes NOTIFY.
  * ``trigger_rolling_restart`` — audit-only surface. The actual restart
                                 is operator-driven (kubectl rollout /
                                 ECS deployment); this function is the
                                 announce-and-record layer so the Audit
                                 tab shows WHO initiated it and WHEN.
  * ``run_db_vacuum``          — execute VACUUM ANALYZE on a whitelisted
                                 table. Whitelist is enforced *here*,
                                 not the endpoint, so any caller through
                                 the service layer is bounded.

Why these are colocated:

  * They share the audit + NOTIFY discipline. Every mutation writes an
    audit row BEFORE publishing — if NOTIFY fails for any reason, the
    audit row still records the operator's intent. The forensic trail
    survives delivery failures.
  * The maintenance mode read/write touches the SAME ``tenants.row``
    that the middleware reads; the NOTIFY consumer in gateway-core /
    gateway-control invalidates its per-tenant cache so the next
    request picks up the new mode without waiting for TTL expiry.

Whitelist for VACUUM:

  * Operators must not be able to VACUUM arbitrary tables. The
    parameter is interpolated into raw SQL because Postgres doesn't
    allow bound parameters for table names; without a whitelist this
    is a SQL-injection surface. The whitelist is the canonical set of
    high-churn tables where VACUUM is a meaningful operator action.
  * Adding a table requires a code change + code review, by design.
    Operators argue for additions in PRs, not via a runtime knob.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, Union

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.system_operation_log import (
    ACTION_DB_VACUUM,
    ACTION_MAINTENANCE_TOGGLE,
    ACTION_RESTART_ROLLING,
    SCOPE_DB_TABLE,
    SCOPE_TENANT,
)
from app.database.models.tenant import Tenant
from app.services.system_ops.audit_row import write_system_operation
from app.services.system_ops.notify import (
    CHANNEL_MAINTENANCE,
    publish_system_notify,
)


logger = BrutorLogger(__name__)


# Whitelist of tables operators are allowed to VACUUM ANALYZE through
# the System Console. Choices justified per row:
#
#   * proxy_logs           — hottest write path on the platform; vacuum
#                            here regains row-density between the
#                            background autovacuum's runs.
#   * usage_logs           — second-hottest write path (one row per
#                            LLM call + tool call).
#   * system_operation_log — the audit table this very feature writes
#                            to; vacuuming reclaims bloat from CASCADE
#                            tenant deletes.
#   * admin_audit_log      — same shape as system_operation_log; same
#                            justification.
#   * proxy_log_request_chunks / proxy_log_response_chunks — large
#                            TOAST-attached rows that don't reclaim
#                            cleanly via autovacuum at peak rates.
#   * tool_approvals       — high churn on the portal approval flow,
#                            many rows expire and need cleanup.
#
# Operators that want a different table must either argue for the
# whitelist addition in a PR, or run VACUUM via direct DB access.
ALLOWED_VACUUM_TABLES = (
    "proxy_logs",
    "usage_logs",
    "system_operation_log",
    "admin_audit_log",
    "proxy_log_request_chunks",
    "proxy_log_response_chunks",
    "tool_approvals",
)


class MaintenanceError(Exception):
    """Base class for maintenance backend failures."""


class UnknownTenantError(MaintenanceError):
    """Raised when the supplied tenant_id doesn't exist."""


class InvalidVacuumTableError(MaintenanceError):
    """Raised when run_db_vacuum is asked to operate on a table that
    isn't in ALLOWED_VACUUM_TABLES. Surfaced as a 400 by the endpoint.
    """


def get_maintenance(db: Session, tenant_id: str) -> Dict[str, Any]:
    """Return the current per-tenant maintenance state.

    Shape matches ``MaintenanceStateResponse`` in
    ``app/api/v1/models/admin/system_ops.py``. ``maintenance_mode_at``
    and ``maintenance_mode_by`` are serialised verbatim; the endpoint
    layer turns the datetime into an ISO string via the Pydantic
    response model.

    Raises:
        UnknownTenantError: tenant row not found.
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        raise UnknownTenantError(f"Tenant {tenant_id!r} not found")
    return {
        "tenant_id": tenant.id,
        "mode": getattr(tenant, "maintenance_mode", None) or "off",
        "maintenance_mode_at": (
            getattr(tenant, "maintenance_mode_at", None).isoformat()
            if getattr(tenant, "maintenance_mode_at", None)
            else None
        ),
        "maintenance_mode_by": getattr(tenant, "maintenance_mode_by", None),
        "drain_grace_secs": getattr(tenant, "drain_grace_secs", None),
    }


def set_maintenance(
    db: Session,
    *,
    tenant_id: str,
    mode: str,
    actor: AdminPrincipal,
    drain_grace_secs: Optional[int] = None,
    reason: Optional[str] = None,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    """Toggle the maintenance flag for ``tenant_id``.

    Order of operations matters — it's the audit-trail-integrity
    guarantee that distinguishes this from a casual UPDATE:

      1. Read the BEFORE state for the audit diff. We snapshot mode +
         drain_grace_secs from the row so the Audit tab can render a
         "off → drain" diff with the operator's reason inline.
      2. UPDATE the row (mode + at + by + drain_grace_secs). Commit.
      3. Write the audit row with the BEFORE/AFTER diff. The
         ``write_system_operation`` helper commits its OWN transaction
         so the audit row survives even if the NOTIFY in step 4 fails.
      4. Publish NOTIFY on ``brutor:maintenance`` so every gateway-core
         and gateway-control replica drops its cached row for this
         tenant. The next request reads the new mode.

    If steps 1 or 2 fail, we never reached step 3 / 4 — the
    operator gets a 500 and the row is unchanged. If step 3 fails,
    the row was updated but the audit log is missing — that's a
    monitoring-alert situation (the audit-log integrity check
    notices the missing row). If step 4 fails, the row is updated
    and audited but the proxy replicas keep stale state for up to
    the cache TTL (1s); they pick up the change on the next miss.

    Raises:
        UnknownTenantError: tenant row not found.
        ValueError: ``mode`` not in MaintenancePayload's allowed set.
    """
    if mode not in ("off", "read_only", "drain", "paused"):
        raise ValueError(
            f"Invalid maintenance mode {mode!r}. Allowed: off, read_only, "
            "drain, paused."
        )
    if drain_grace_secs is not None and (
        drain_grace_secs < 0 or drain_grace_secs > 3600
    ):
        raise ValueError(
            f"drain_grace_secs {drain_grace_secs} out of range [0, 3600]"
        )

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        raise UnknownTenantError(f"Tenant {tenant_id!r} not found")

    before_state = {
        "mode": getattr(tenant, "maintenance_mode", None) or "off",
        "drain_grace_secs": getattr(tenant, "drain_grace_secs", None),
        "maintenance_mode_at": (
            getattr(tenant, "maintenance_mode_at", None).isoformat()
            if getattr(tenant, "maintenance_mode_at", None)
            else None
        ),
        "maintenance_mode_by": getattr(tenant, "maintenance_mode_by", None),
    }

    actor_id = getattr(actor, "id", None)
    now = datetime.now(timezone.utc)
    tenant.maintenance_mode = mode
    tenant.maintenance_mode_at = now
    tenant.maintenance_mode_by = actor_id
    # Only stash drain_grace_secs when the caller supplied it — leave
    # the existing value untouched otherwise so toggling between modes
    # without changing the grace window preserves the operator's
    # earlier per-tenant override.
    if drain_grace_secs is not None:
        tenant.drain_grace_secs = drain_grace_secs

    db.add(tenant)
    db.commit()
    db.refresh(tenant)

    after_state = {
        "mode": tenant.maintenance_mode,
        "drain_grace_secs": tenant.drain_grace_secs,
        "maintenance_mode_at": (
            tenant.maintenance_mode_at.isoformat()
            if tenant.maintenance_mode_at
            else None
        ),
        "maintenance_mode_by": tenant.maintenance_mode_by,
    }

    write_system_operation(
        db=db,
        actor=actor,
        action=ACTION_MAINTENANCE_TOGGLE,
        tenant_id=tenant_id,
        scope_kind=SCOPE_TENANT,
        scope_id=tenant_id,
        payload={
            "mode": mode,
            "drain_grace_secs": drain_grace_secs,
            "reason": reason,
        },
        before_state=before_state,
        after_state=after_state,
        request=request,
    )

    # NOTIFY is fire-and-forget by design. If pg_notify fails, the
    # update already landed and the audit row already recorded the
    # operator's intent — the only consequence is that proxy replicas
    # take up to the TTL to pick up the new mode. We log loudly so a
    # repeated failure shows up in monitoring.
    try:
        publish_system_notify(
            db=db,
            channel=CHANNEL_MAINTENANCE,
            payload={
                "tenant_id": tenant_id,
                "mode": mode,
                "drain_grace_secs": drain_grace_secs,
            },
        )
    except Exception as exc:
        logger.warning(
            "Maintenance NOTIFY publish failed; replicas will pick up "
            "the new mode on cache expiry",
            context={"tenant_id": tenant_id, "mode": mode, "error": str(exc)},
        )

    return {
        "tenant_id": tenant_id,
        "mode": tenant.maintenance_mode,
        "maintenance_mode_at": after_state["maintenance_mode_at"],
        "maintenance_mode_by": after_state["maintenance_mode_by"],
        "drain_grace_secs": tenant.drain_grace_secs,
    }


def trigger_rolling_restart(
    db: Session,
    *,
    actor: AdminPrincipal,
    tenant_id: Optional[str] = None,
    reason: Optional[str] = None,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    """Record an operator-initiated rolling restart in the Audit log.

    No process actually restarts here — the actual rolling restart is
    operator-driven (``kubectl rollout restart`` on a k8s deployment,
    or a force-new-deployment on ECS). What this function provides:

      * The forensic trail. The Audit tab shows WHO clicked "Rolling
        restart" and WHEN, with the operator-supplied reason. Future
        post-mortems can correlate the restart with downstream
        symptoms.
      * The announce-channel hook (NO-OP today, plumbing for future).
        We DO NOT NOTIFY here — there's no proxy-side consumer that
        should act on a restart announce, and we don't want to trick
        the cache invalidator into thinking config changed. If/when a
        "graceful drain before restart" coordinator ships, it'll get
        its own channel.

    Returns the audit row id so the UI can deep-link into the Audit
    tab and surface "restart recorded — kick the rollout when ready".
    """
    row = write_system_operation(
        db=db,
        actor=actor,
        action=ACTION_RESTART_ROLLING,
        tenant_id=tenant_id,
        scope_kind=SCOPE_TENANT if tenant_id else None,
        scope_id=tenant_id,
        payload={"reason": reason},
        request=request,
    )
    return {
        "audit_id": row.id,
        "recorded_at": (
            row.created_at.isoformat()
            if row.created_at
            else datetime.now(timezone.utc).isoformat()
        ),
    }


def run_db_vacuum(
    db: Session,
    *,
    table: str,
    actor: AdminPrincipal,
    tenant_id: Optional[str] = None,
    reason: Optional[str] = None,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute ``VACUUM ANALYZE <table>`` against the live database.

    ``VACUUM`` cannot run inside an open transaction; SQLAlchemy
    sessions normally wrap every statement in a transaction. We work
    around this by reaching for a raw connection (via
    ``db.connection().connection``) and switching the underlying DB-API
    connection's isolation level to AUTOCOMMIT for the single
    statement, then restoring the prior level. The whole dance is
    isolated in this function so endpoint code never has to touch
    isolation modes.

    Args:
        table: Table name to vacuum. MUST be in
            ALLOWED_VACUUM_TABLES; the check protects against an
            operator (or a confused frontend) requesting a vacuum on
            a table that shouldn't be touched, and against an SQL
            injection vector — because Postgres won't let us bind a
            table name parameter we have to interpolate the string
            directly. The whitelist makes this safe.

    Returns:
        {"table": "...", "duration_ms": ..., "audit_id": "..."}
    """
    if table not in ALLOWED_VACUUM_TABLES:
        raise InvalidVacuumTableError(
            f"Table {table!r} is not whitelisted for VACUUM via the System "
            f"Console. Allowed: {sorted(ALLOWED_VACUUM_TABLES)}. To vacuum "
            "a different table, add it to ALLOWED_VACUUM_TABLES in a code "
            "change with review."
        )

    # Write the audit row BEFORE we start vacuuming. If VACUUM blows
    # up, the operator's intent is still recorded.
    audit_row = write_system_operation(
        db=db,
        actor=actor,
        action=ACTION_DB_VACUUM,
        tenant_id=tenant_id,
        scope_kind=SCOPE_DB_TABLE,
        scope_id=table,
        payload={"table": table, "reason": reason},
        request=request,
    )

    # VACUUM must NOT be inside a transaction. SQLAlchemy's Session
    # opens an implicit transaction on first statement; reaching for
    # the raw DB-API connection and flipping it to AUTOCOMMIT lets us
    # issue the VACUUM cleanly. We restore the prior isolation level
    # in the finally so we don't leak the AUTOCOMMIT state to the
    # next caller that reuses this session.
    started = datetime.now(timezone.utc)
    raw_connection = None
    prior_isolation_level = None
    try:
        # Commit anything outstanding on the session so we don't
        # hold an open snapshot during the AUTOCOMMIT swap.
        db.commit()
        raw_connection = db.connection().connection
        prior_isolation_level = raw_connection.isolation_level
        raw_connection.set_isolation_level(0)  # AUTOCOMMIT
        with raw_connection.cursor() as cur:
            # Table name is whitelisted; safe to interpolate. Double-
            # quote the identifier so a future addition to the
            # whitelist that uses a mixed-case name still works.
            cur.execute(f'VACUUM ANALYZE "{table}"')
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "VACUUM ANALYZE complete",
            context={
                "table": table,
                "duration_secs": elapsed,
                "audit_id": audit_row.id,
            },
        )
        return {
            "table": table,
            "duration_ms": int(elapsed * 1000),
            "audit_id": audit_row.id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        # Restore isolation so the next user of this session sees
        # the same level they expected. Defensive — at this point
        # if the cursor raised we still want to leave the connection
        # in a sane state.
        if raw_connection is not None and prior_isolation_level is not None:
            try:
                raw_connection.set_isolation_level(prior_isolation_level)
            except Exception:
                logger.warning(
                    "Failed to restore isolation level after VACUUM",
                    context={"table": table},
                )

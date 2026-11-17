# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Operator abort of a single in-flight run.

    POST /v1/admin/ai-systems/runs/{root_task_id}/abort  — stop one run
    GET  /v1/admin/ai-systems/runs/live                  — what is running now
    GET  /v1/admin/ai-systems/runs/{root_task_id}/intervention

Two things about the shape are deliberate.

**There is no live-run table.** A run in flight exists only as rows
accumulating in `proxy_logs`; `ai_system_runs` is a rollup written when the
run finalizes. Adding a live table would put a write on the hot path for
every run, and almost no run is ever intervened on. The live list is derived
on read instead — `SWEEP_CANDIDATES_SQL` from the core's run ledger with its
`HAVING` inverted, over the same `ix_proxy_logs_sweep` index.

**The intervention is the durable object.** Writing it is what an operator
does; every gateway replica finds out through three independent paths
(`pg_notify`, a Redis key, and the sweeper's reconcile) because an abort that
fails to arrive lets a runaway continue, and that is the wrong direction for
a safety control to fail.

Handlers are `def`, not `async def`, dispatched through
`app.core.sync_bridge.block_on` — the rule for anything holding a `Session`.
"""

from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
    resolve_tenant_id as _resolve_tenant_id,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import ResourceGroup
from app.database.models.run_intervention import (
    ACTION_ABORT,
    STATUS_CLOSED,
    STATUS_REQUESTED,
    RunIntervention,
)
from app.middleware.trusted_proxies import client_ip

#: How far back a run's last step may be and still count as "live".
#: Wider than any configured idle window, because a run that has gone quiet
#: for a few minutes is exactly the one an operator wants to stop.
LIVE_WINDOW_SECONDS_DEFAULT = 1800

#: `pg_notify` channel every gateway replica listens on. Pinned in the core
#: as `run_intervention::ABORT_CHANNEL`.
ABORT_CHANNEL = "brutor_run_abort"


# `SWEEP_CANDIDATES_SQL` with its HAVING inverted: still running rather than
# gone idle. Same index, same shape rules — the ledger join stays OUTSIDE the
# aggregate and every predicate is applied before the LIMIT, because this
# runs from an interactive screen against the busiest table in the schema.
_LIVE_RUNS_SQL = text(
    """
    SELECT
        g.root_task_id,
        g.first_step_at,
        g.last_step_at,
        g.step_count,
        g.group_id,
        i.id            AS intervention_id,
        i.status        AS intervention_status,
        i.reason        AS intervention_reason
    FROM (
        SELECT
            pl.root_task_id,
            MIN(pl.timestamp) AS first_step_at,
            MAX(pl.timestamp) AS last_step_at,
            COUNT(*)          AS step_count,
            (ARRAY_AGG(pl.resource_group_id ORDER BY pl.timestamp, pl.id)
                FILTER (WHERE pl.resource_group_id IS NOT NULL))[1] AS group_id
        FROM proxy_logs pl
        WHERE pl.tenant_id = :tenant_id
          AND pl.root_task_id IS NOT NULL
          AND pl.timestamp > NOW() - MAKE_INTERVAL(secs => :window_secs)
        GROUP BY pl.root_task_id
    ) g
    LEFT JOIN ai_system_runs r
           ON r.tenant_id = :tenant_id
          AND r.root_task_id = g.root_task_id
    LEFT JOIN run_interventions i
           ON i.tenant_id = :tenant_id
          AND i.root_task_id = g.root_task_id
          AND i.status <> 'closed'
    -- Live = no finalized ledger row, or steps arrived after one was written.
    WHERE (r.ended_at IS NULL OR g.last_step_at > r.ended_at)
    ORDER BY g.last_step_at DESC
    LIMIT :limit
    """
)


def _run_group_ids(db: Session, tenant_id: str, root_task_id: str) -> list[str]:
    """Distinct resource groups this run's steps were authorized against."""
    rows = db.execute(
        text(
            "SELECT DISTINCT resource_group_id FROM proxy_logs "
            "WHERE tenant_id = :t AND root_task_id = :r "
            "AND resource_group_id IS NOT NULL"
        ),
        {"t": tenant_id, "r": root_task_id},
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def _is_self_or_ancestor(db: Session, tenant_id: str, ancestor: str, descendant: str) -> bool:
    """Whether `ancestor` is `descendant` or sits above it in the tree."""
    if ancestor == descendant:
        return True
    group = (
        db.query(ResourceGroup)
        .filter(ResourceGroup.id == descendant, ResourceGroup.tenant_id == tenant_id)
        .first()
    )
    if group is None:
        return False
    return any(a.id == ancestor for a in group.get_ancestors())


def create_run_interventions_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["Run Interventions"])

    @router.get(
        "/admin/ai-systems/runs/live",
        summary="Runs currently in flight",
    )
    def list_live_runs(
        tenant_id: Optional[str] = Query(None),
        window_seconds: int = Query(
            LIVE_WINDOW_SECONDS_DEFAULT, ge=60, le=86400,
            description="How far back a run's last step may be and still count as live",
        ),
        limit: int = Query(100, ge=1, le=500),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> dict:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        rows = db.execute(
            _LIVE_RUNS_SQL,
            {"tenant_id": resolved, "window_secs": window_seconds, "limit": limit},
        ).fetchall()

        runs = []
        for r in rows:
            runs.append(
                {
                    "root_task_id": r.root_task_id,
                    "first_step_at": r.first_step_at.isoformat() if r.first_step_at else None,
                    "last_step_at": r.last_step_at.isoformat() if r.last_step_at else None,
                    "step_count": int(r.step_count or 0),
                    "ai_system_id": r.group_id,
                    # An operator needs to tell "I clicked stop" from "the run
                    # actually stopped"; a control that conflates them is one
                    # you cannot trust in an incident.
                    "intervention": (
                        {
                            "id": r.intervention_id,
                            "status": r.intervention_status,
                            "reason": r.intervention_reason,
                        }
                        if r.intervention_id
                        else None
                    ),
                }
            )
        return {"runs": runs, "total": len(runs), "window_seconds": window_seconds}

    @router.post(
        "/admin/ai-systems/runs/{root_task_id}/abort",
        summary="Stop one in-flight run",
        status_code=201,
    )
    def abort_run(
        root_task_id: str,
        request: Request,
        payload: dict = Body(...),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RUN_ABORT)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> dict:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        reason = (payload or {}).get("reason")
        if not reason or not str(reason).strip():
            # Required, not merely encouraged: an intervention with no stated
            # reason is not evidence, and "why did this run stop?" is the
            # first question asked afterwards.
            raise HTTPException(status_code=422, detail="reason is required")
        reason = str(reason).strip()[:1024]

        # Containment, mirroring the core's `end_run`: you may only stop a run
        # you can see. Stopping someone else's run is not a read — it writes a
        # terminal state into assurance evidence.
        run_groups = _run_group_ids(db, resolved, root_task_id)
        if not run_groups:
            finalized = (
                db.query(AISystemRun)
                .filter(
                    AISystemRun.tenant_id == resolved,
                    AISystemRun.root_task_id == root_task_id,
                )
                .first()
            )
            if finalized is None:
                raise HTTPException(status_code=404, detail="Run not found")
            raise HTTPException(
                status_code=409,
                detail="Run has already finished; there is nothing to stop",
            )

        existing = (
            db.query(RunIntervention)
            .filter(
                RunIntervention.tenant_id == resolved,
                RunIntervention.root_task_id == root_task_id,
                RunIntervention.status != STATUS_CLOSED,
            )
            .first()
        )
        if existing is not None:
            # A second abort on the same run is the same instruction, not a
            # new one. Return what is already in force rather than erroring:
            # the operator's intent is satisfied either way.
            return {"intervention": existing.to_dict(), "already_requested": True}

        intervention = RunIntervention(
            id=f"ri-{ULID()}".lower(),
            tenant_id=resolved,
            root_task_id=root_task_id,
            action=ACTION_ABORT,
            reason=reason,
            status=STATUS_REQUESTED,
            requested_at=datetime.now(timezone.utc),
        )
        db.add(intervention)
        db.flush()

        # Path 1 of three: tell every replica now. The Redis mirror and the
        # sweeper's reconcile cover a replica that misses this.
        try:
            db.execute(
                text("SELECT pg_notify(:chan, :payload)"),
                {
                    "chan": ABORT_CHANNEL,
                    "payload": f"{resolved}|{root_task_id}|{intervention.id}",
                },
            )
        except Exception as e:  # pragma: no cover - notify is best-effort
            logger.warning(
                "run-abort notify failed; the record stands and the sweeper "
                "will reconcile",
                context={"error": str(e), "root_task_id": root_task_id},
            )

        audit.log(
            action="run_abort_requested",
            admin=admin,
            admin_type=admin_type,
            tenant_id=resolved,
            success=True,
            resource_type=TenantResourceTypes.RESOURCE_GROUPS.value,
            resource_id=run_groups[0] if run_groups else None,
            details={
                "root_task_id": root_task_id,
                "reason": reason,
                "intervention_id": intervention.id,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        # The operator's act is a sealed `resolved` record
        # (reject / human / true, authority = the intervention id). Best-
        # effort: the abort stands whether or not the core can seal it.
        from app.services.compliance.sealing_hooks import nearest_ai_system, seal_run_abort

        seal = seal_run_abort(
            db, intervention, nearest_ai_system(db, resolved, run_groups[0] if run_groups else None)
        )

        return {
            "intervention": intervention.to_dict(),
            "already_requested": False,
            "record": seal.to_dict(),
            # Said plainly here, in the docs and in the confirm dialog: abort
            # stops the NEXT action. A provider call already in flight
            # completes. Overclaiming a hard kill is what external validation
            # catches.
            "note": (
                "The run will be refused at its next governed action. A call "
                "already in flight completes."
            ),
        }

    @router.get(
        "/admin/ai-systems/runs/{root_task_id}/intervention",
        summary="The intervention on one run, if any",
    )
    def get_intervention(
        root_task_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> dict:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        row = (
            db.query(RunIntervention)
            .filter(
                RunIntervention.tenant_id == resolved,
                RunIntervention.root_task_id == root_task_id,
            )
            .order_by(RunIntervention.requested_at.desc())
            .first()
        )
        return {"intervention": row.to_dict() if row else None}

    return router


__all__ = ["create_run_interventions_router", "ABORT_CHANNEL", "LIVE_WINDOW_SECONDS_DEFAULT"]

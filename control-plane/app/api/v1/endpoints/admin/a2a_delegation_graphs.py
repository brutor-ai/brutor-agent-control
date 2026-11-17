# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A Delegation Graph admin API (Mission Control).

Endpoints under `/admin/a2a/delegation-graphs`:

  GET /                  — list recent graphs (summaries, paginated)
  GET /{root_task_id}    — full tree of steps for a single graph
  GET /stats             — aggregate counters + percentiles over a window

All endpoints are tenant-scoped:
  * Tenant admins are pinned to their own tenant. Naming a *different*
    tenant in `?tenant_id=` is refused with 403 — the parameter is not
    silently ignored, which would answer about tenant A under a heading
    that says B. Naming their own tenant is fine.
  * System admins must supply `tenant_id` explicitly.

Feature gated on `features.a2a` — tenants without the opt-in get 404
so the surface is invisible rather than merely forbidden.
"""

from datetime import datetime
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.v1.models.admin.a2a_delegation import (
    A2ADelegationGraphDetail,
    A2ADelegationGraphList,
    A2ADelegationGraphSummary,
    A2ADelegationStep,
    A2ADelegationStats,
    A2ADelegationTopCard,
)
from app.core.admin_user.dependencies import (
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.tenant import Tenant
from app.services.a2a.delegation_graph_service import A2ADelegationGraphService


A2A_FEATURE_FLAG = "a2a"


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _enforce_a2a_enabled(db: Session, tenant_id: str) -> Tenant:
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if tenant is None:
        raise HTTPException(404, f"Tenant {tenant_id!r} not found")
    if not tenant.has_feature(A2A_FEATURE_FLAG):
        raise HTTPException(
            404,
            "A2A feature is not enabled for this tenant",
        )
    return tenant


def create_a2a_delegation_graphs_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(
        prefix="/admin/a2a/delegation-graphs",
        tags=["A2A Delegation Graphs"],
    )
    service = A2ADelegationGraphService(logger)

    # ────────────────────────────────────────────────────────────────
    # /stats — declared BEFORE the /{root_task_id} GET so FastAPI's
    # first-match routing doesn't try to resolve 'stats' as a ULID.
    # ────────────────────────────────────────────────────────────────
    @router.get(
        "/stats",
        response_model=A2ADelegationStats,
        summary="Aggregate stats over a window of A2A steps",
    )
    def get_stats(
        tenant_id: Optional[str] = Query(None),
        since: Optional[datetime] = Query(None),
        until: Optional[datetime] = Query(None),
        top_n_cards: int = Query(5, ge=1, le=50),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_DELEGATION_READ)),
    ) -> A2ADelegationStats:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved)

        result = service.compute_stats(
            db,
            tenant_id=resolved,
            since=since,
            until=until,
            top_n_cards=top_n_cards,
        )
        top_cards_raw = result.pop("top_cards", [])
        return A2ADelegationStats(
            **result,
            top_cards=[A2ADelegationTopCard(**c) for c in top_cards_raw],
        )

    # ────────────────────────────────────────────────────────────────
    # List
    # ────────────────────────────────────────────────────────────────
    @router.get(
        "",
        response_model=A2ADelegationGraphList,
        summary="List recent A2A delegation graphs",
    )
    def list_graphs(
        tenant_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        since: Optional[datetime] = Query(None),
        until: Optional[datetime] = Query(None),
        success: Optional[bool] = Query(
            None,
            description="If set, only graphs whose every step matches this value are returned",
        ),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_DELEGATION_READ)),
    ) -> A2ADelegationGraphList:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved)

        summaries, total = service.list_graphs(
            db,
            tenant_id=resolved,
            limit=limit,
            offset=offset,
            since=since,
            until=until,
            success=success,
        )
        return A2ADelegationGraphList(
            graphs=[A2ADelegationGraphSummary(**s) for s in summaries],
            total=total,
            limit=limit,
            offset=offset,
        )

    # ────────────────────────────────────────────────────────────────
    # Detail
    # ────────────────────────────────────────────────────────────────
    @router.get(
        "/{root_task_id}",
        response_model=A2ADelegationGraphDetail,
        summary="Full step tree for a single A2A delegation graph",
    )
    def get_graph(
        root_task_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_DELEGATION_READ)),
    ) -> A2ADelegationGraphDetail:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved)

        graph = service.get_graph(db, tenant_id=resolved, root_task_id=root_task_id)
        if graph is None:
            raise HTTPException(404, f"Delegation graph {root_task_id!r} not found")

        return A2ADelegationGraphDetail(
            root_task_id=graph["root_task_id"],
            tenant_id=graph["tenant_id"],
            total_steps=graph["total_steps"],
            max_depth=graph["max_depth"],
            steps=[A2ADelegationStep(**h) for h in graph["steps"]],
        )

    return router

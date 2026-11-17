# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A v1.0 stateful task admin API (read-only).

The Rust proxy writes `a2a_tasks` rows directly; the admin API is a
Mission Control browser over the same data, identical in spirit to
`a2a_delegation_graphs.py`.

Endpoints:
  GET  /admin/a2a-tasks                    — List with filters + pagination
  GET  /admin/a2a-tasks/{task_id}          — Task detail (task_json blob)
  GET  /admin/a2a-tasks/{task_id}/push-configs
                                           — Push-notification configs for a task

Filters on list:
  * state            — exact match on denormalised `state` column
  * context_id       — exact match
  * resource_group_id — exact match (resource-group scoping)
  * since / until    — ISO-8601 window on `status_timestamp`
  * limit / offset   — pagination (limit capped at 200)

All endpoints are gated by:
  * RBAC permission A2A_TASK_READ
  * Tenant feature flag (`features.a2a`) — 404 if disabled
"""

from datetime import datetime
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.api.v1.models.admin.a2a_task import (
    A2aPushNotificationConfigListItem,
    A2aPushNotificationConfigListResponse,
    A2aTaskDetailResponse,
    A2aTaskListItem,
    A2aTaskListResponse,
)
from app.core.admin_user.dependencies import (
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.a2a_task import A2aPushNotificationConfig, A2aTask
from app.database.models.admin import TenantAdmin
from app.database.models.tenant import Tenant


A2A_FEATURE_FLAG = "a2a"

LIST_MAX_LIMIT = 200
LIST_DEFAULT_LIMIT = 50


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


def _task_to_list_item(row: A2aTask) -> A2aTaskListItem:
    return A2aTaskListItem(
        id=row.id,
        tenant_id=row.tenant_id,
        resource_group_id=row.resource_group_id,
        context_id=row.context_id,
        state=row.state,
        status_timestamp=row.status_timestamp.isoformat() if row.status_timestamp else "",
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _task_to_detail(row: A2aTask) -> A2aTaskDetailResponse:
    return A2aTaskDetailResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        resource_group_id=row.resource_group_id,
        context_id=row.context_id,
        state=row.state,
        status_timestamp=row.status_timestamp.isoformat() if row.status_timestamp else "",
        task_json=row.task_json or {},
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _parse_iso(ts: Optional[str], field: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Accept both "+00:00" and "Z" — Python's fromisoformat doesn't
        # take "Z" before 3.11.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO-8601 timestamp for {field}: {exc}",
        )


def create_a2a_tasks_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/a2a-tasks", tags=["A2A Tasks"])

    @router.get(
        "",
        response_model=A2aTaskListResponse,
        summary="List A2A v1.0 tasks for the caller's tenant",
    )
    def list_tasks(
        tenant_id: Optional[str] = Query(None),
        state: Optional[str] = Query(None),
        context_id: Optional[str] = Query(None),
        resource_group_id: Optional[str] = Query(None),
        since: Optional[str] = Query(None),
        until: Optional[str] = Query(None),
        limit: int = Query(LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_TASK_READ)),
    ) -> A2aTaskListResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        since_dt = _parse_iso(since, "since")
        until_dt = _parse_iso(until, "until")

        filters = [A2aTask.tenant_id == resolved_tenant]
        if state:
            filters.append(A2aTask.state == state)
        if context_id:
            filters.append(A2aTask.context_id == context_id)
        if resource_group_id:
            filters.append(A2aTask.resource_group_id == resource_group_id)
        if since_dt:
            filters.append(A2aTask.status_timestamp >= since_dt)
        if until_dt:
            filters.append(A2aTask.status_timestamp <= until_dt)

        where = and_(*filters)
        total = db.query(A2aTask).filter(where).count()
        rows = (
            db.query(A2aTask)
            .filter(where)
            .order_by(A2aTask.status_timestamp.desc(), A2aTask.id.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return A2aTaskListResponse(
            tasks=[_task_to_list_item(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/{task_id}",
        response_model=A2aTaskDetailResponse,
        summary="Get an A2A v1.0 task by id",
    )
    def get_task(
        task_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_TASK_READ)),
    ) -> A2aTaskDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        row = (
            db.query(A2aTask)
            .filter(A2aTask.id == task_id, A2aTask.tenant_id == resolved_tenant)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, f"Task {task_id!r} not found")
        return _task_to_detail(row)

    @router.get(
        "/{task_id}/push-configs",
        response_model=A2aPushNotificationConfigListResponse,
        summary="List push-notification configs attached to a task",
    )
    def list_push_configs(
        task_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.A2A_TASK_READ)),
    ) -> A2aPushNotificationConfigListResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        _enforce_a2a_enabled(db, resolved_tenant)

        # Scope read to (tenant, task) so a malformed task_id can't leak
        # cross-tenant config rows even if the task itself is missing.
        rows = (
            db.query(A2aPushNotificationConfig)
            .filter(
                A2aPushNotificationConfig.tenant_id == resolved_tenant,
                A2aPushNotificationConfig.task_id == task_id,
            )
            .order_by(A2aPushNotificationConfig.created_at.desc())
            .all()
        )
        return A2aPushNotificationConfigListResponse(
            configs=[
                A2aPushNotificationConfigListItem(
                    id=r.id,
                    tenant_id=r.tenant_id,
                    task_id=r.task_id,
                    config_json=r.config_json or {},
                    created_at=r.created_at.isoformat() if r.created_at else "",
                )
                for r in rows
            ],
            total=len(rows),
        )

    return router

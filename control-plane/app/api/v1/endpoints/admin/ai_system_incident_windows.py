# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Incident windows admin API

    GET    /v1/admin/incident-windows                       — tenant listing
    POST   /v1/admin/incident-windows                       — declare (body ai_system_id, null = tenant-wide)
    GET    /v1/admin/ai-systems/{id}/incident-windows       — windows that apply to the system
    POST   /v1/admin/ai-systems/{id}/incident-windows       — declare for the system
    PATCH  /v1/admin/incident-windows/{window_id}           — edit (bounds, cause, reason, resource)
    DELETE /v1/admin/incident-windows/{window_id}           — withdraw

A window is a declared period whose samples are excluded from judgement
but kept as evidence. Declaring, editing and withdrawing are each sealed
declarations; every write response carries `seal`. A window
declared by a `cause_fixed` resolution is listed here like any other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin._compliance_http import load_ai_system, seal_status
from app.api.v1.models.admin.drift_resolution import (
    IncidentWindowCreate,
    IncidentWindowDeleteResponse,
    IncidentWindowListResponse,
    IncidentWindowResponse,
    IncidentWindowUpdate,
    IncidentWindowWriteResponse,
)
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.ai_system_drift_resolution import AISystemIncidentWindow
from app.database.models.resource_group import ResourceGroup
from app.services import drift_resolution
from app.services.ai_system_judgement_exclusion import (
    window_findings,
    window_run_count,
    windows_for,
)


def _names(db: Session, tenant_id: str, ids: List[str]) -> Dict[str, str]:
    if not ids:
        return {}
    return {
        gid: (display or name)
        for gid, name, display in db.query(
            ResourceGroup.id, ResourceGroup.name, ResourceGroup.display_name
        ).filter(ResourceGroup.tenant_id == tenant_id, ResourceGroup.id.in_(ids))
    }


def _response(db: Session, w: AISystemIncidentWindow, names: Dict[str, str]) -> IncidentWindowResponse:
    return IncidentWindowResponse(
        **w.to_dict(),
        ai_system_name=names.get(w.ai_system_id) if w.ai_system_id else None,
        runs_excluded=window_run_count(db, w),
        drift_event_ids=window_findings(db, [w.id])[w.id],
    )


def _list(db: Session, tenant_id: str, rows: List[AISystemIncidentWindow]) -> IncidentWindowListResponse:
    names = _names(db, tenant_id, sorted({w.ai_system_id for w in rows if w.ai_system_id}))
    return IncidentWindowListResponse(
        windows=[_response(db, w, names) for w in rows], total=len(rows)
    )


def _write_response(db: Session, w: AISystemIncidentWindow, outcome) -> IncidentWindowWriteResponse:
    names = _names(db, w.tenant_id, [w.ai_system_id] if w.ai_system_id else [])
    base = _response(db, w, names)
    return IncidentWindowWriteResponse(**base.model_dump(), seal=seal_status(outcome))


def _load_window(db: Session, tenant_id: str, window_id: str) -> AISystemIncidentWindow:
    w = (
        db.query(AISystemIncidentWindow)
        .filter(
            AISystemIncidentWindow.id == window_id,
            AISystemIncidentWindow.tenant_id == tenant_id,
        )
        .first()
    )
    if w is None:
        raise HTTPException(status_code=404, detail=f"Incident window {window_id!r} not found")
    return w


def create_ai_system_incident_windows_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["AI System Incident Windows"])

    def _declare(db, admin, tenant_id: str, ai_system_id: Optional[str],
                 payload: IncidentWindowCreate) -> IncidentWindowWriteResponse:
        if ai_system_id is not None:
            load_ai_system(db, tenant_id, ai_system_id)
        try:
            window, outcome = drift_resolution.declare_window(
                db,
                tenant_id=tenant_id,
                ai_system_id=ai_system_id,
                starts_at=payload.starts_at,
                ends_at=payload.ends_at,
                cause_category=payload.cause_category,
                reason=payload.reason,
                by=getattr(admin, "username", None),
                resource_type=payload.resource_type,
                resource_id=payload.resource_id,
            )
        except drift_resolution.ResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        logger.info(
            "Incident window declared",
            context={"tenant_id": tenant_id, "window_id": window.id, "ai_system_id": ai_system_id},
        )
        return _write_response(db, window, outcome)

    @router.get(
        "/admin/incident-windows",
        response_model=IncidentWindowListResponse,
        summary="Incident windows in the tenant",
    )
    def list_windows(
        tenant_id: Optional[str] = Query(None),
        ai_system_id: Optional[str] = Query(
            None, description="Only windows that apply to this system (its own + tenant-wide)"
        ),
        since: Optional[datetime] = Query(None, description="Windows ending after this"),
        until: Optional[datetime] = Query(None, description="Windows starting before this"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> IncidentWindowListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        rows = windows_for(
            db, resolved, [ai_system_id] if ai_system_id else None, since=since, until=until
        )
        rows = sorted(rows, key=lambda w: (w.starts_at, w.id), reverse=True)
        return _list(db, resolved, rows)

    @router.post(
        "/admin/incident-windows",
        response_model=IncidentWindowWriteResponse,
        status_code=201,
        summary="Declare an incident window (tenant-wide unless ai_system_id is given)",
    )
    def create_window(
        payload: IncidentWindowCreate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> IncidentWindowWriteResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        return _declare(db, admin, resolved, payload.ai_system_id, payload)

    @router.get(
        "/admin/ai-systems/{ai_system_id}/incident-windows",
        response_model=IncidentWindowListResponse,
        summary="Incident windows that apply to this AI System (its own and tenant-wide)",
    )
    def list_system_windows(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        since: Optional[datetime] = Query(None),
        until: Optional[datetime] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> IncidentWindowListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        load_ai_system(db, resolved, ai_system_id)
        rows = windows_for(db, resolved, [ai_system_id], since=since, until=until)
        rows = sorted(rows, key=lambda w: (w.starts_at, w.id), reverse=True)
        return _list(db, resolved, rows)

    @router.post(
        "/admin/ai-systems/{ai_system_id}/incident-windows",
        response_model=IncidentWindowWriteResponse,
        status_code=201,
        summary="Declare an incident window for this AI System",
    )
    def create_system_window(
        ai_system_id: str,
        payload: IncidentWindowCreate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> IncidentWindowWriteResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        if payload.ai_system_id not in (None, ai_system_id):
            raise HTTPException(
                status_code=422, detail="ai_system_id in the body must match the path"
            )
        return _declare(db, admin, resolved, ai_system_id, payload)

    @router.patch(
        "/admin/incident-windows/{window_id}",
        response_model=IncidentWindowWriteResponse,
        summary="Edit an incident window (a new sealed declaration)",
    )
    def update_window(
        window_id: str,
        payload: IncidentWindowUpdate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> IncidentWindowWriteResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        window = _load_window(db, resolved, window_id)
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(status_code=422, detail="nothing to change")
        try:
            window, outcome = drift_resolution.update_window(
                db, window, changes=changes, by=getattr(admin, "username", None)
            )
        except drift_resolution.ResolutionError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _write_response(db, window, outcome)

    @router.delete(
        "/admin/incident-windows/{window_id}",
        response_model=IncidentWindowDeleteResponse,
        summary="Withdraw an incident window (sealed); its samples count again",
    )
    def delete_window(
        window_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> IncidentWindowDeleteResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        window = _load_window(db, resolved, window_id)
        outcome = drift_resolution.withdraw_window(db, window)
        return IncidentWindowDeleteResponse(id=window_id, seal=seal_status(outcome))

    return router


__all__ = ["create_ai_system_incident_windows_router"]

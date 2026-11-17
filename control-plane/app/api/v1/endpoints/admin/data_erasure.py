# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Data-subject erasure endpoints. See https://docs.brutor.ai/governance/data-handling/
"""

from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.v1.models.admin.data_erasure import (
    DataErasureListResponse,
    DataErasureRequest,
    DataErasureResponse,
)
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.constants.admin import AdminType
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.data_erasure import DataErasureEvent
from app.services.data_erasure import erase


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.middleware.trusted_proxies import client_ip


def create_data_erasure_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/data-erasure", tags=["Data Erasure"])

    @router.post(
        "",
        response_model=DataErasureResponse,
        status_code=status.HTTP_200_OK,
        summary="Erase a data subject's personal data from the ledger",
        description=(
            "Removes stored bodies, headers and direct identifiers on the subject's "
            "proxy-log rows, their feedback rows, and the end_users record. Keeps the "
            "record layer and its pseudonymous keys (the audit hash chain covers them). "
            "Every request is recorded as an append-only erasure event; dry_run=true "
            "returns the counts without changing anything."
        ),
    )
    def erase_subject(
        body: DataErasureRequest,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: AdminPrincipal = Depends(require_permission(Permission.DATA_ERASURE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> DataErasureResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            event = erase(db, resolved, body, requested_by=str(admin.id))
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.error("data erasure failed", context={"tenant_id": resolved, "error": str(exc)})
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Erasure failed; nothing was changed")
        audit.log(
            action="data_erasure_dry_run" if body.dry_run else "data_erasure",
            admin=admin,
            admin_type=admin_type,
            tenant_id=resolved,
            success=True,
            resource_type="data_erasure_event",
            resource_id=event.id,
            details={"subject_kinds": event.subject_kinds, "counts": event.counts},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )
        return DataErasureResponse(**event.to_dict())

    @router.get(
        "",
        response_model=DataErasureListResponse,
        summary="List erasure events",
    )
    def list_events(
        tenant_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: AdminPrincipal = Depends(require_permission(Permission.DATA_ERASURE)),
    ) -> DataErasureListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        q = db.query(DataErasureEvent).filter(DataErasureEvent.tenant_id == resolved)
        total = q.count()
        rows = q.order_by(DataErasureEvent.requested_at.desc()).offset(offset).limit(limit).all()
        return DataErasureListResponse(
            events=[DataErasureResponse(**r.to_dict()) for r in rows], total=total
        )

    return router

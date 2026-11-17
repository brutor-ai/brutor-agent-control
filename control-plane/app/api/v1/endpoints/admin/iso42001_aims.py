# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Admin API — ISO/IEC 42001 AI Management System: control coverage / Statement of
Applicability, readiness rollup, and the auditor-facing evidence pack. Distinct
from the runtime compliance-tagging surface (compliance.py). Routes under
/admin/iso42001.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.services import iso42001_service as svc
from app.services.iso42001_service import Iso42001Error


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


class CoverageUpdate(BaseModel):

    model_config = ConfigDict(extra="forbid")
    status: str
    owner: Optional[str] = None
    evidence: Any = None
    notes: Optional[str] = None
    next_review: Optional[str] = None


def create_iso42001_aims_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/iso42001", tags=["ISO 42001 (AIMS)"])

    @router.get("/coverage", summary="Statement of Applicability + readiness rollup")
    def coverage(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ):
        admin, at = admin_and_type
        t = _resolve_tenant_id(admin, at, tenant_id)
        return svc.get_coverage(db, t)

    @router.put("/coverage/{control_id}", summary="Set a control's coverage status")
    def set_coverage(
        control_id: str,
        body: CoverageUpdate,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ):
        admin, at = admin_and_type
        t = _resolve_tenant_id(admin, at, tenant_id)
        try:
            row = svc.set_coverage(db, t, control_id, updated_by=admin.id, **body.model_dump())
        except Iso42001Error as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        audit.log(action="iso42001.coverage.set", db=db, admin=admin,
                  resource_type="iso42001_control", resource_id=control_id, tenant_id=t,
                  details={"status": body.status})
        return row.to_dict()

    @router.get("/evidence-pack", summary="Auditor-facing ISO 42001 evidence pack")
    def evidence_pack(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ):
        admin, at = admin_and_type
        t = _resolve_tenant_id(admin, at, tenant_id)
        # generated_at supplied by the caller layer would be ideal; use DB clock
        # via a lightweight now() to keep the service pure/testable.
        import datetime as _dt
        return svc.evidence_pack(db, t, generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat())

    return router

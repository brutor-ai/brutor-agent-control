# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Weekly assurance digest admin API.

The console twin of the Slack digest: stored digests are exactly what
was pushed to channels (same row); preview computes the current week live
without storing or sending — the testable-without-a-webhook surface.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import (
    resolve_tenant_id,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.assurance_check import AssuranceDigest
from app.services.assurance_digest import build_digest, current_week_start


class DigestResponse(BaseModel):
    id: str
    tenant_id: str
    week_start: str
    content: Dict[str, Any]
    generated_at: Optional[str] = None


class DigestListResponse(BaseModel):
    digests: List[DigestResponse]
    total: int


def create_assurance_digest_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/assurance-digest", tags=["Assurance Digest"])

    # One tenant-scoping rule for every admin route — see
    # app/core/admin_user/dependencies.py::resolve_tenant_id.
    _tenant_of = resolve_tenant_id

    @router.get("", response_model=DigestListResponse,
                summary="List stored weekly digests")
    def list_digests(
        tenant_id: Optional[str] = Query(None),
        limit: int = Query(12, ge=1, le=52),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> DigestListResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        q = db.query(AssuranceDigest).filter(
            AssuranceDigest.tenant_id == tenant_id
        )
        total = q.count()
        rows = q.order_by(AssuranceDigest.week_start.desc()).limit(limit).all()
        return DigestListResponse(
            digests=[DigestResponse(**r.to_dict()) for r in rows], total=total
        )

    @router.post("/preview", summary="Compute the current week's digest live")
    def preview_digest(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        week_start = current_week_start(datetime.now(timezone.utc), "UTC")
        return build_digest(db, tenant_id, week_start)

    return router

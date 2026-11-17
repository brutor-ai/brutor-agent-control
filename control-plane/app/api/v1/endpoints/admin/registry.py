# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unified Agent Registry admin API — `/v1/admin/agent-registry`.

One coverage view over every AI resource: governed assets (ai_assets, enriched
with version + capability metadata) alongside the discovered-but-ungoverned
inventory, with "N seen, M governed" counts. Read-only. Permission:
AI_ASSET_READ (it's a read view of the asset registry).
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import get_current_admin, require_permission, resolve_tenant_id
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.services.registry_service import RegistryService


def create_agent_registry_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/agent-registry", tags=["Agent Registry"])
    service = RegistryService(logger=logger)

    @router.get("", summary="Unified agent registry (governed + discovered coverage)")
    def get_registry(
        tenant_id: Optional[str] = Query(None),
        admin_and_tenant: tuple = Depends(get_current_admin),
        _: Any = Depends(require_permission(Permission.AI_ASSET_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        admin, admin_type = admin_and_tenant
        # The principal decides which tenant.
        tid = resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            return service.coverage(db, tid)
        except Exception as e:
            logger.error("Failed to build unified registry", context={
                "tenant_id": tid, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to build unified registry",
            )

    return router

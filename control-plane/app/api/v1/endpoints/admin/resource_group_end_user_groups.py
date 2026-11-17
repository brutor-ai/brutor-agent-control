# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# RG-side EndUserGroup binding endpoints

The symmetric counterpart to `/admin/end-user-groups/{id}/resource-groups`.
Used by the existing Resource Group → Members tab when the operator
picks an end-user group to grant RG access. Same idempotent
semantics; same EndUserGroupManager under the hood.

## Routes

  GET    /admin/resource-groups/{group_id}/end-user-groups
      List the EndUserGroups currently bound to this RG.

  POST   /admin/resource-groups/{group_id}/end-user-groups
      Bind one or more EndUserGroups to this RG. Idempotent —
      already-bound groups are silently skipped.

  DELETE /admin/resource-groups/{group_id}/end-user-groups
      Unbind one or more EndUserGroups from this RG. Idempotent.

Mounting this as a separate router (rather than tacking onto the
existing 7000-line resource_groups.py) keeps this surface
isolated and lets us write focused tests for it.
"""

from __future__ import annotations

from typing import Any, Dict, Union

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.api.v1.models.admin.end_user_group import (
    EndUserGroupResourceGroupRef,
    EndUserGroupSummary,
    ResourceGroupBindEndUserGroupRequest,
    ResourceGroupUnbindEndUserGroupRequest,
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
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.end_user_group import EndUserGroup
from app.database.models.resource_group import ResourceGroup
from app.services.cache_invalidation import invalidate_proxy_cache
from app.services.end_user_group_manager import (
    EndUserGroupError,
    EndUserGroupManager,
)


_ERROR_HTTP_MAP: Dict[str, int] = {
    "not_found": 404,
    "cross_tenant_group": 400,
}


def _http_for(err: EndUserGroupError) -> HTTPException:
    return HTTPException(
        status_code=_ERROR_HTTP_MAP.get(err.code, 400),
        detail={"code": err.code, "message": err.message},
    )


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def create_resource_group_end_user_groups_router(
    logger: BrutorLogger,
    manager: EndUserGroupManager | None = None,
) -> APIRouter:
    mgr = manager or EndUserGroupManager(logger=logger)
    router = APIRouter(
        prefix="/admin/resource-groups",
        tags=["Resource Group — End-User Group Bindings (Admin)"],
    )

    # ------------------------------------------------------------------
    # LIST bound user groups for this RG
    # ------------------------------------------------------------------

    @router.get(
        "/{group_id}/end-user-groups",
        response_model=Dict[str, Any],
        summary="List end-user groups bound to this Resource Group",
    )
    def list_bound_user_groups(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_MEMBERS_READ)
        ),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        rg = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == tenant_id,
        ).first()
        if rg is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "not_found",
                    "message": f"ResourceGroup {group_id!r} not found",
                },
            )
        # `resource_groups.end_user_groups` is lazy=selectin on the
        # EndUserGroup side; pull via the relationship for consistency
        # with the other detail endpoints.
        bound = list(rg.end_user_groups or [])
        return {
            "end_user_groups": [
                EndUserGroupSummary(
                    id=g.id,
                    tenant_id=g.tenant_id,
                    name=g.name,
                    display_name=g.display_name,
                    description=g.description,
                    source=g.source,
                    source_external_id=g.source_external_id,
                    idp_server_id=g.idp_server_id,
                    member_count=g.member_count,
                    created_at=g.created_at.isoformat() if g.created_at else None,
                    updated_at=g.updated_at.isoformat() if g.updated_at else None,
                ).model_dump()
                for g in bound
            ],
            "total": len(bound),
        }

    # ------------------------------------------------------------------
    # BIND user groups TO this RG
    # ------------------------------------------------------------------

    @router.post(
        "/{group_id}/end-user-groups",
        response_model=Dict[str, Any],
        summary="Bind one or more EndUserGroups to this Resource Group",
    )
    def bind_user_groups(
        group_id: str,
        payload: ResourceGroupBindEndUserGroupRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_MEMBERS_ADD)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        try:
            mgr.bind_to_resource_group(
                db,
                tenant_id=tenant_id,
                resource_group_id=group_id,
                end_user_group_ids=payload.end_user_group_ids,
                created_by=admin_obj.id,
            )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        # EndUserGroup↔ResourceGroup binding changed — drop the proxy's
        # group caches so EndUserGroup-mediated access reflects at once.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        try:
            audit.log(
                action="resource_group.end_user_group.bind",
                db=db,
                admin=admin_obj,
                resource_type="resource_group",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={
                    "end_user_group_ids": list(payload.end_user_group_ids),
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Audit log failed for resource_group.end_user_group.bind",
                context={"resource_group_id": group_id},
            )
        # Return the fresh list so the UI can re-render without a refetch.
        rg = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == tenant_id,
        ).first()
        bound = list((rg.end_user_groups or []) if rg else [])
        return {
            "end_user_groups": [
                EndUserGroupResourceGroupRef(
                    id=g.id, name=g.name, display_name=g.display_name,
                ).model_dump()
                for g in bound
            ],
            "total": len(bound),
        }

    # ------------------------------------------------------------------
    # UNBIND user groups FROM this RG
    # ------------------------------------------------------------------

    @router.delete(
        "/{group_id}/end-user-groups",
        response_model=Dict[str, Any],
        summary="Unbind one or more EndUserGroups from this Resource Group",
    )
    def unbind_user_groups(
        group_id: str,
        payload: ResourceGroupUnbindEndUserGroupRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_MEMBERS_REMOVE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        try:
            mgr.unbind_from_resource_group(
                db,
                tenant_id=tenant_id,
                resource_group_id=group_id,
                end_user_group_ids=payload.end_user_group_ids,
            )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        # EndUserGroup↔ResourceGroup binding changed — drop the proxy's
        # group caches so EndUserGroup-mediated access reflects at once.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        try:
            audit.log(
                action="resource_group.end_user_group.unbind",
                db=db,
                admin=admin_obj,
                resource_type="resource_group",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={
                    "end_user_group_ids": list(payload.end_user_group_ids),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        rg = db.query(ResourceGroup).filter(
            ResourceGroup.id == group_id,
            ResourceGroup.tenant_id == tenant_id,
        ).first()
        bound = list((rg.end_user_groups or []) if rg else [])
        return {
            "end_user_groups": [
                EndUserGroupResourceGroupRef(
                    id=g.id, name=g.name, display_name=g.display_name,
                ).model_dump()
                for g in bound
            ],
            "total": len(bound),
        }

    return router

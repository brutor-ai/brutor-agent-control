# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Admin REST endpoints for EndUserGroup

REST surface for the new EndUserGroup entity. The endpoints operate
under `/v1/admin/end-user-groups/...` and are protected by the new
`END_USER_GROUP_*` permissions added in the same sprint.

## Routes

  GET    /admin/end-user-groups                 list (paginated, filterable)
  POST   /admin/end-user-groups                 create manual group
  GET    /admin/end-user-groups/{id}            detail (members + RGs inlined)
  PATCH  /admin/end-user-groups/{id}            partial update
  DELETE /admin/end-user-groups/{id}            delete (cascades memberships)

  POST   /admin/end-user-groups/{id}/members    idempotent ADD
  DELETE /admin/end-user-groups/{id}/members    idempotent REMOVE
  PUT    /admin/end-user-groups/{id}/members    authoritative SET

  POST   /admin/end-user-groups/{id}/resource-groups   bind to RGs
  DELETE /admin/end-user-groups/{id}/resource-groups   unbind from RGs

## What's intentionally NOT here

  * The source of a group is always 'manual' (the only source in this
    release). The Pydantic create model hides `source` /
    `source_external_id` / `idp_server_id` from the request body.
  * Renaming a stored group of another source is refused at the
    service layer.

## Audit

Every mutation goes through `UnifiedAdminAuditLogger` so deletes
and membership changes appear in the audit trail.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.api.v1.models.admin.end_user_group import (
    AddMembersRequest,
    EndUserGroupBindResourceGroupsRequest,
    EndUserGroupCreate,
    EndUserGroupDetail,
    EndUserGroupMember,
    EndUserGroupResourceGroupRef,
    EndUserGroupSummary,
    EndUserGroupUnbindResourceGroupsRequest,
    EndUserGroupUpdate,
    RemoveMembersRequest,
    SetMembersRequest,
)
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.services.cache_invalidation import invalidate_proxy_cache
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.end_user_group import EndUserGroup
from app.services.end_user_group_manager import (
    EndUserGroupError,
    EndUserGroupManager,
)


# Manager-error-code → HTTP-status. Centralising this keeps every
# handler's `except EndUserGroupError` block identical.
_ERROR_HTTP_MAP: Dict[str, int] = {
    "not_found": 404,
    "name_conflict": 409,
    "rename_not_allowed": 409,
    "invalid_name": 400,
    "invalid_source_filter": 400,
    "cross_tenant_user": 400,
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


def _to_summary(group: EndUserGroup) -> EndUserGroupSummary:
    """Light projection for list responses."""
    return EndUserGroupSummary(
        id=group.id,
        tenant_id=group.tenant_id,
        name=group.name,
        display_name=group.display_name,
        description=group.description,
        source=group.source,
        source_external_id=group.source_external_id,
        idp_server_id=group.idp_server_id,
        member_count=group.member_count,
        created_at=group.created_at.isoformat() if group.created_at else None,
        updated_at=group.updated_at.isoformat() if group.updated_at else None,
    )


def _to_detail(group: EndUserGroup) -> EndUserGroupDetail:
    """Detail projection — inlines members + RGs."""
    summary = _to_summary(group)
    members = [
        EndUserGroupMember(
            id=u.id,
            username=u.username,
            email=u.email,
            display_name=u.display_name,
            is_active=bool(u.is_active),
        )
        for u in (group.members or [])
    ]
    rgs = [
        EndUserGroupResourceGroupRef(
            id=rg.id, name=rg.name, display_name=rg.display_name,
        )
        for rg in (group.resource_groups or [])
    ]
    return EndUserGroupDetail(
        **summary.model_dump(),
        members=members,
        resource_groups=rgs,
    )


def create_end_user_groups_router(
    logger: BrutorLogger,
    manager: Optional[EndUserGroupManager] = None,
) -> APIRouter:
    """Factory matching the project convention.

    `manager` is injectable so tests can swap in a no-op or
    instrumented variant.
    """
    mgr = manager or EndUserGroupManager(logger=logger)
    router = APIRouter(
        prefix="/admin/end-user-groups",
        tags=["End User Group Management (Admin)"],
    )

    # ------------------------------------------------------------------
    # LIST
    # ------------------------------------------------------------------

    @router.get(
        "",
        response_model=Dict[str, Any],
        summary="List end-user groups",
    )
    def list_groups(
        request: Request,
        search: Optional[str] = Query(
            None, description="Case-insensitive substring search on name/display_name"
        ),
        source: Optional[str] = Query(
            None, description="Filter by source: manual (the only source in this release)"
        ),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.END_USER_GROUP_READ)
        ),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        try:
            rows, total = mgr.list(
                db,
                tenant_id=tenant_id,
                source=source,
                search=search,
                limit=limit,
                offset=offset,
            )
        except EndUserGroupError as e:
            raise _http_for(e)
        return {
            "groups": [_to_summary(g).model_dump() for g in rows],
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "returned": len(rows),
            },
            "filters": {"source": source, "search": search},
        }

    # ------------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------------

    @router.post(
        "",
        response_model=EndUserGroupDetail,
        status_code=status.HTTP_201_CREATED,
        summary="Create a manual end-user group",
    )
    def create_group(
        payload: EndUserGroupCreate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.END_USER_GROUP_CREATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        # Tenant quota — Max End User Groups.
        TenantService.check_quota(db, tenant_id, TenantResourceTypes.END_USER_GROUPS)
        try:
            group = mgr.create_manual(
                db,
                tenant_id=tenant_id,
                name=payload.name,
                display_name=payload.display_name,
                description=payload.description,
                initial_member_ids=payload.initial_member_ids,
            )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        # Reload with relationships inlined for the response.
        full = mgr.get(
            db, tenant_id=tenant_id, group_id=group.id,
            with_members=True, with_resource_groups=True,
        )
        try:
            audit.log(
                action="end_user_group.create",
                db=db,
                admin=admin_obj,
                resource_type="end_user_group",
                resource_id=group.id,
                tenant_id=tenant_id,
                details={
                    "name": group.name,
                    "initial_member_count": len(payload.initial_member_ids or []),
                },
            )
        except Exception:  # noqa: BLE001 — audit must never fail the request
            logger.warning(
                "Audit log failed for end_user_group.create",
                context={"group_id": group.id},
            )
        return _to_detail(full)

    # ------------------------------------------------------------------
    # GET DETAIL
    # ------------------------------------------------------------------

    @router.get(
        "/{group_id}",
        response_model=EndUserGroupDetail,
        summary="Get an end-user group with members and RG bindings",
    )
    def get_group(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.END_USER_GROUP_READ)
        ),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        group = mgr.get(
            db, tenant_id=tenant_id, group_id=group_id,
            with_members=True, with_resource_groups=True,
        )
        if group is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "not_found",
                    "message": f"EndUserGroup {group_id!r} not found",
                },
            )
        return _to_detail(group)

    # ------------------------------------------------------------------
    # UPDATE
    # ------------------------------------------------------------------

    @router.patch(
        "/{group_id}",
        response_model=EndUserGroupDetail,
        summary="Partial-update an end-user group",
    )
    def update_group(
        group_id: str,
        payload: EndUserGroupUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.END_USER_GROUP_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        try:
            group = mgr.update(
                db,
                tenant_id=tenant_id,
                group_id=group_id,
                name=payload.name,
                display_name=payload.display_name,
                description=payload.description,
            )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        full = mgr.get(
            db, tenant_id=tenant_id, group_id=group.id,
            with_members=True, with_resource_groups=True,
        )
        try:
            audit.log(
                action="end_user_group.update",
                db=db,
                admin=admin_obj,
                resource_type="end_user_group",
                resource_id=group.id,
                tenant_id=tenant_id,
                details=payload.model_dump(exclude_unset=True),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Audit log failed for end_user_group.update",
                context={"group_id": group.id},
            )
        return _to_detail(full)

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------

    @router.delete(
        "/{group_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Delete an end-user group",
    )
    def delete_group(
        group_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.END_USER_GROUP_DELETE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        # Fetch first so the audit log carries the name + source even
        # after delete.
        existing = mgr.get(db, tenant_id=tenant_id, group_id=group_id)
        try:
            mgr.delete(db, tenant_id=tenant_id, group_id=group_id)
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        # Deleting a group cascades away its resource-group bindings —
        # drop the proxy's group caches so access reflects at once.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        if existing is not None:
            try:
                audit.log(
                    action="end_user_group.delete",
                    db=db,
                    admin=admin_obj,
                    resource_type="end_user_group",
                    resource_id=group_id,
                    tenant_id=tenant_id,
                    details={
                        "name": existing.name,
                        "source": existing.source,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Audit log failed for end_user_group.delete",
                    context={"group_id": group_id},
                )
        return None

    # ------------------------------------------------------------------
    # MEMBERSHIP
    # ------------------------------------------------------------------

    @router.post(
        "/{group_id}/members",
        response_model=EndUserGroupDetail,
        summary="Add users to an end-user group (idempotent)",
    )
    def add_members(
        group_id: str,
        payload: AddMembersRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.END_USER_GROUP_MEMBERS_ADD)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        try:
            mgr.add_members(
                db,
                tenant_id=tenant_id,
                group_id=group_id,
                end_user_ids=payload.end_user_ids,
                source="manual",
            )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        full = mgr.get(
            db, tenant_id=tenant_id, group_id=group_id,
            with_members=True, with_resource_groups=True,
        )
        # Membership changed — drop the proxy's per-user group cache so
        # EndUserGroup-mediated resource access reflects at once.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        try:
            audit.log(
                action="end_user_group.members.add",
                db=db,
                admin=admin_obj,
                resource_type="end_user_group",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={"user_ids": list(payload.end_user_ids)},
            )
        except Exception:  # noqa: BLE001
            pass
        return _to_detail(full)

    @router.delete(
        "/{group_id}/members",
        response_model=EndUserGroupDetail,
        summary="Remove users from an end-user group (idempotent)",
    )
    def remove_members(
        group_id: str,
        payload: RemoveMembersRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.END_USER_GROUP_MEMBERS_REMOVE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        try:
            mgr.remove_members(
                db,
                tenant_id=tenant_id,
                group_id=group_id,
                end_user_ids=payload.end_user_ids,
            )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        full = mgr.get(
            db, tenant_id=tenant_id, group_id=group_id,
            with_members=True, with_resource_groups=True,
        )
        # Membership changed — drop the proxy's per-user group cache so
        # EndUserGroup-mediated resource access reflects at once.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        try:
            audit.log(
                action="end_user_group.members.remove",
                db=db,
                admin=admin_obj,
                resource_type="end_user_group",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={"user_ids": list(payload.end_user_ids)},
            )
        except Exception:  # noqa: BLE001
            pass
        return _to_detail(full)

    @router.put(
        "/{group_id}/members",
        response_model=EndUserGroupDetail,
        summary="Replace the entire roster of an end-user group",
    )
    def set_members(
        group_id: str,
        payload: SetMembersRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            # Roster replace is allow + remove combined — require BOTH
            # by gating on the strictly-more-powerful ADD perm (a
            # principal with ADD-only could not safely run a SET).
            # In practice operators with one usually have the other.
            require_permission(Permission.END_USER_GROUP_MEMBERS_ADD)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        tenant_id = _resolve_tenant_id(admin_obj, admin_type)
        try:
            mgr.set_members(
                db,
                tenant_id=tenant_id,
                group_id=group_id,
                end_user_ids=payload.end_user_ids,
                source="manual",
            )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        full = mgr.get(
            db, tenant_id=tenant_id, group_id=group_id,
            with_members=True, with_resource_groups=True,
        )
        # Membership changed — drop the proxy's per-user group cache so
        # EndUserGroup-mediated resource access reflects at once.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        try:
            audit.log(
                action="end_user_group.members.set",
                db=db,
                admin=admin_obj,
                resource_type="end_user_group",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={
                    "roster_size": len(payload.end_user_ids),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        return _to_detail(full)

    # ------------------------------------------------------------------
    # RESOURCE-GROUP BINDING
    # ------------------------------------------------------------------

    @router.post(
        "/{group_id}/resource-groups",
        response_model=EndUserGroupDetail,
        summary="Bind this end-user group to one or more Resource Groups",
    )
    def bind_resource_groups(
        group_id: str,
        payload: EndUserGroupBindResourceGroupsRequest,
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
        # Existence-check this UG first so we 404 BEFORE making any
        # changes (the manager would otherwise touch each RG without
        # noticing the UG is gone).
        if mgr.get(db, tenant_id=tenant_id, group_id=group_id) is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "not_found",
                    "message": f"EndUserGroup {group_id!r} not found",
                },
            )
        try:
            for rg_id in payload.resource_group_ids:
                mgr.bind_to_resource_group(
                    db,
                    tenant_id=tenant_id,
                    resource_group_id=rg_id,
                    end_user_group_ids=[group_id],
                    created_by=admin_obj.id,
                )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        full = mgr.get(
            db, tenant_id=tenant_id, group_id=group_id,
            with_members=True, with_resource_groups=True,
        )
        # Binding an EndUserGroup to a resource group is what grants its
        # members that group's resources — the same edge the delete and
        # add-member paths above already publish for. Without this the
        # proxy's `user_groups` mapping keeps the pre-bind answer until
        # the group cache's TTL lapses.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        try:
            audit.log(
                action="end_user_group.rg.bind",
                db=db,
                admin=admin_obj,
                resource_type="end_user_group",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={
                    "resource_group_ids": list(payload.resource_group_ids),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        return _to_detail(full)

    @router.delete(
        "/{group_id}/resource-groups",
        response_model=EndUserGroupDetail,
        summary="Unbind this end-user group from one or more Resource Groups",
    )
    def unbind_resource_groups(
        group_id: str,
        payload: EndUserGroupUnbindResourceGroupsRequest,
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
        if mgr.get(db, tenant_id=tenant_id, group_id=group_id) is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "not_found",
                    "message": f"EndUserGroup {group_id!r} not found",
                },
            )
        try:
            for rg_id in payload.resource_group_ids:
                mgr.unbind_from_resource_group(
                    db,
                    tenant_id=tenant_id,
                    resource_group_id=rg_id,
                    end_user_group_ids=[group_id],
                )
            db.commit()
        except EndUserGroupError as e:
            db.rollback()
            raise _http_for(e)
        full = mgr.get(
            db, tenant_id=tenant_id, group_id=group_id,
            with_members=True, with_resource_groups=True,
        )
        # Revoking the binding must reach the proxy promptly — this is the
        # access-removal direction, so a stale cache leaves resources
        # reachable after an admin has taken them away.
        block_on(invalidate_proxy_cache(
            request, "resource_group", tenant_id, group_id, action="update"
        ))
        try:
            audit.log(
                action="end_user_group.rg.unbind",
                db=db,
                admin=admin_obj,
                resource_type="end_user_group",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={
                    "resource_group_ids": list(payload.resource_group_ids),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        return _to_detail(full)

    return router

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Admin endpoints for saved log views.

# Endpoints

  GET    /v1/admin/saved-log-views?scope=...               — list
  POST   /v1/admin/saved-log-views                         — create
  GET    /v1/admin/saved-log-views/{id}                    — read
  PATCH  /v1/admin/saved-log-views/{id}                    — update
  DELETE /v1/admin/saved-log-views/{id}                    — delete

# Tenant isolation

Every row carries `tenant_id`. List/read/update/delete all filter by
tenant_id matched against the requesting admin's tenant — a system
admin without an effective tenant scope must pass `tenant_id` as a
query parameter (mirrors the proxy_logs router's pattern). Cross-
tenant access is fully blocked at the query layer, not just the
response layer, so an enumeration attack can't succeed.

# Ownership + sharing

Each view has `owner_admin_id` set on create. Update and delete
require either being the owner OR being a system admin. The
sharing flag is operator-set — flipping `is_shared = TRUE` lets
every admin on the tenant see the view in their dropdown, but
nobody but the owner / sysadmin can edit it.

# Why no soft delete

Saved views are operator preference state, not audit evidence. A
hard delete is the expected behaviour — the operator can just save
a new view if they want it back. Audit-relevant CRUD (create / update
/ delete) still flows through the audit log so a security review
can reconstruct who set up which preset, even after the row is gone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.v1.models.admin.saved_log_view import (
    SavedLogViewCreateRequest,
    SavedLogViewListResponse,
    SavedLogViewResponse,
    SavedLogViewUpdateRequest,
    SavedViewScope,
)
from app.constants.admin import AdminType
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    is_platform_wide,
    require_permission,
    resolve_tenant_id,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.saved_log_view import SavedLogView
from app.middleware.trusted_proxies import client_ip


def _resolve_tenant_for_views(
    admin_obj,
    admin_type,
    tenant_id_query: Optional[str],
) -> str:
    """Tenant-scope resolver. Same shape as the proxy_logs router's
    `_resolve_tenant_for_log_read` helper — tenant admins are pinned
    to their own tenant, system admins must pass `tenant_id`. Keeping
    the two helpers parallel so a future RBAC change has one obvious
    place to update.
    """
    # The principal decides which tenant.
    return resolve_tenant_id(admin_obj, admin_type, tenant_id_query)


def _get_for_tenant(
    db: Session, tenant_id: str, view_id: str
) -> SavedLogView:
    """Fetch a view, asserting it belongs to the current tenant.
    404 on miss OR wrong-tenant — same response on both branches so
    an attacker can't enumerate other tenants' view IDs by status-code
    differential.
    """
    view = (
        db.query(SavedLogView)
        .filter(
            SavedLogView.id == view_id,
            SavedLogView.tenant_id == tenant_id,
        )
        .one_or_none()
    )
    if view is None:
        raise HTTPException(404, "Saved log view not found")
    return view


def _assert_mutation_allowed(
    view: SavedLogView,
    admin_obj,
    admin_type,
) -> None:
    """Update + delete are restricted to:
      * the view's owner (most common case)
      * any system admin (operator override / cleanup)

    A tenant admin who isn't the owner gets 403 even if `is_shared`
    is True — sharing is read-only for everyone except the owner.
    Otherwise one admin could clobber another's saved triage view.
    """
    if is_platform_wide(admin_type):
        return
    owner_id = view.owner_admin_id
    actor_id = str(getattr(admin_obj, "id", ""))
    if owner_id is None or str(owner_id) != actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the view's owner (or a system admin) can modify "
                "or delete it"
            ),
        )


def create_saved_log_views_router(
    logger: Optional[BrutorLogger] = None,
) -> APIRouter:
    """Create the saved-log-views CRUD router."""
    log = logger or BrutorLogger("saved_log_views_admin")
    router = APIRouter(
        prefix="/admin/saved-log-views",
        tags=["Admin - Saved Log Views"],
    )

    # ------------------------------------------------------------------
    # LIST — returns the requester's own views + every shared view in
    # the tenant, scoped to one viewer at a time.
    # ------------------------------------------------------------------

    @router.get(
        "",
        response_model=SavedLogViewListResponse,
        summary="List saved log views (own + shared) for a viewer",
    )
    def list_views(
        scope: SavedViewScope = Query(..., description="Which viewer"),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: object = Depends(require_permission(Permission.PROXY_LOGS_READ)),
    ):
        admin_obj, admin_type = admin_and_type
        effective_tenant_id = _resolve_tenant_for_views(
            admin_obj, admin_type, tenant_id,
        )
        actor_id = str(getattr(admin_obj, "id", ""))

        # Build the visibility predicate. Tenant admins see their own
        # views + every shared view on the tenant. System admins see
        # everything in the tenant — same override as GET-single /
        # PATCH / DELETE so an incident-response sysadmin can audit
        # the full saved-view inventory without separate plumbing.
        base_filters = [
            SavedLogView.tenant_id == effective_tenant_id,
            SavedLogView.scope == scope,
        ]
        if not is_platform_wide(admin_type):
            base_filters.append(
                (SavedLogView.owner_admin_id == actor_id)
                | (SavedLogView.is_shared.is_(True))
            )
        rows = (
            db.query(SavedLogView)
            .filter(*base_filters)
            .order_by(SavedLogView.name.asc())
            .all()
        )

        return SavedLogViewListResponse(
            items=[SavedLogViewResponse(**r.to_dict()) for r in rows],
        )

    # ------------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------------

    @router.post(
        "",
        response_model=SavedLogViewResponse,
        status_code=201,
        summary="Create a new saved log view",
    )
    def create_view(
        body: SavedLogViewCreateRequest,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: object = Depends(require_permission(Permission.PROXY_LOGS_READ)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        effective_tenant_id = _resolve_tenant_for_views(
            admin_obj, admin_type, tenant_id,
        )
        actor_id = str(getattr(admin_obj, "id", ""))

        # 409 instead of letting the DB unique violation through —
        # gives the admin UI a clean error to display.
        existing = (
            db.query(SavedLogView)
            .filter(
                SavedLogView.tenant_id == effective_tenant_id,
                SavedLogView.scope == body.scope,
                SavedLogView.owner_admin_id == actor_id,
                SavedLogView.name == body.name,
            )
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A saved view named {body.name!r} already exists for "
                    f"this viewer ({body.scope})."
                ),
            )

        view = SavedLogView(
            id=f"slv-{uuid4().hex[:16]}",
            tenant_id=effective_tenant_id,
            scope=body.scope,
            owner_admin_id=actor_id,
            name=body.name,
            description=body.description,
            filters=body.filters,
            is_shared=body.is_shared,
            created_at=datetime.now(timezone.utc),
        )
        db.add(view)
        db.flush()
        db.refresh(view)

        audit.log(
            action="saved_log_view_create",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=effective_tenant_id,
            success=True,
            resource_type="saved_log_view",
            resource_id=view.id,
            details={
                "scope": view.scope,
                "name": view.name,
                "is_shared": view.is_shared,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )
        log.info(
            "Saved log view created",
            context={
                "view_id": view.id,
                "tenant_id": effective_tenant_id,
                "scope": view.scope,
                "owner_admin_id": actor_id,
            },
        )
        return SavedLogViewResponse(**view.to_dict())

    # ------------------------------------------------------------------
    # READ (single)
    # ------------------------------------------------------------------

    @router.get(
        "/{view_id}",
        response_model=SavedLogViewResponse,
        summary="Read one saved log view",
    )
    def get_view(
        view_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: object = Depends(require_permission(Permission.PROXY_LOGS_READ)),
    ):
        admin_obj, admin_type = admin_and_type
        effective_tenant_id = _resolve_tenant_for_views(
            admin_obj, admin_type, tenant_id,
        )
        view = _get_for_tenant(db, effective_tenant_id, view_id)

        # Visibility: must be the owner OR the view is shared. Same
        # rule as the list endpoint applies here so a directly-typed
        # view ID can't leak someone else's private view.
        actor_id = str(getattr(admin_obj, "id", ""))
        if (
            not is_platform_wide(admin_type)
            and not view.is_shared
            and str(view.owner_admin_id or "") != actor_id
        ):
            raise HTTPException(404, "Saved log view not found")

        return SavedLogViewResponse(**view.to_dict())

    # ------------------------------------------------------------------
    # UPDATE
    # ------------------------------------------------------------------

    @router.patch(
        "/{view_id}",
        response_model=SavedLogViewResponse,
        summary="Update a saved log view",
    )
    def update_view(
        view_id: str,
        body: SavedLogViewUpdateRequest,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: object = Depends(require_permission(Permission.PROXY_LOGS_READ)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        effective_tenant_id = _resolve_tenant_for_views(
            admin_obj, admin_type, tenant_id,
        )
        view = _get_for_tenant(db, effective_tenant_id, view_id)
        _assert_mutation_allowed(view, admin_obj, admin_type)

        changes: dict = {}
        update_data = body.model_dump(exclude_unset=True)

        # Rename collision check: if the operator's renaming, make
        # sure the new name doesn't already exist for the same
        # (tenant, scope, owner) — would violate the unique index.
        new_name = update_data.get("name")
        if new_name is not None and new_name != view.name:
            owner_id = view.owner_admin_id
            collision = (
                db.query(SavedLogView)
                .filter(
                    SavedLogView.tenant_id == view.tenant_id,
                    SavedLogView.scope == view.scope,
                    SavedLogView.owner_admin_id == owner_id,
                    SavedLogView.name == new_name,
                    SavedLogView.id != view.id,
                )
                .first()
            )
            if collision is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"A saved view named {new_name!r} already exists for "
                        f"this viewer."
                    ),
                )

        for field, value in update_data.items():
            old = getattr(view, field)
            if old != value:
                # `filters` is too verbose for the audit body — log a
                # boolean flag instead, keeping the row size sane.
                if field == "filters":
                    changes["filters_touched"] = True
                else:
                    changes[field] = {"old": old, "new": value}
                setattr(view, field, value)

        if changes:
            view.updated_at = datetime.now(timezone.utc)
            db.flush()
            db.refresh(view)

            audit.log(
                action="saved_log_view_update",
                admin=admin_obj,
                admin_type=admin_type,
                tenant_id=effective_tenant_id,
                success=True,
                resource_type="saved_log_view",
                resource_id=view.id,
                details={"scope": view.scope, "changes": changes},
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db,
            )

        return SavedLogViewResponse(**view.to_dict())

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------

    @router.delete(
        "/{view_id}",
        status_code=204,
        summary="Delete a saved log view",
    )
    def delete_view(
        view_id: str,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: object = Depends(require_permission(Permission.PROXY_LOGS_READ)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, admin_type = admin_and_type
        effective_tenant_id = _resolve_tenant_for_views(
            admin_obj, admin_type, tenant_id,
        )
        view = _get_for_tenant(db, effective_tenant_id, view_id)
        _assert_mutation_allowed(view, admin_obj, admin_type)

        snapshot = {
            "scope": view.scope,
            "name": view.name,
            "is_shared": view.is_shared,
        }
        db.delete(view)
        db.flush()

        audit.log(
            action="saved_log_view_delete",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=effective_tenant_id,
            success=True,
            resource_type="saved_log_view",
            resource_id=view_id,
            details=snapshot,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

    return router

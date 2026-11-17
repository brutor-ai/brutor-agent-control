# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance Inbox admin API.

Read = ASSURANCE_INBOX_READ (also granted to Auditor / Read-Only — items
and dispositions are review evidence). Mutations = ASSURANCE_INBOX_MANAGE
and are individually admin-audited: dispositions are part of the assurance
evidence chain.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import (
    resolve_tenant_id,
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.assurance_inbox import (
    INBOX_CATEGORIES,
    INBOX_DISPOSITIONS,
    INBOX_SEVERITIES,
    INBOX_STATES,
    AssuranceInboxEvent,
    AssuranceInboxItem,
)
from app.services.assurance_inbox import AssuranceInboxService


class InboxItemResponse(BaseModel):
    id: str
    tenant_id: str
    ai_system_id: Optional[str] = None
    category: str
    severity: str
    fingerprint: str
    title: str
    evidence: Dict[str, Any] = {}
    occurrence_count: int
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    state: str
    acknowledged_at: Optional[str] = None
    acknowledged_by: Optional[str] = None
    assignee_admin: Optional[str] = None
    disposition: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None


class InboxEventResponse(BaseModel):
    id: str
    item_id: str
    at: Optional[str] = None
    admin: Optional[str] = None
    event: str
    detail: Optional[Dict[str, Any]] = None


class InboxItemDetailResponse(InboxItemResponse):
    events: List[InboxEventResponse] = []


class InboxListResponse(BaseModel):
    items: List[InboxItemResponse]
    total: int
    limit: int
    offset: int


class InboxCountsResponse(BaseModel):
    open: int
    acknowledged: int
    resolved: int
    #: Unresolved (open + acknowledged) by severity — the sidebar badges.
    open_critical: int
    open_warning: int = 0
    open_info: int = 0


class AssuranceInboxResolveRequest(BaseModel):
    disposition: str = Field(..., description="|".join(INBOX_DISPOSITIONS))
    note: str = Field(..., min_length=1, max_length=4000)


class AssignRequest(BaseModel):
    assignee: str = Field(..., min_length=1, max_length=255)


class ReopenRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=4000)


class BulkAcknowledgeRequest(BaseModel):
    item_ids: List[str] = Field(..., min_length=1, max_length=200)


def create_assurance_inbox_router(logger: BrutorLogger) -> APIRouter:
    """Build the `/admin/assurance-inbox/*` router."""
    router = APIRouter(prefix="/admin/assurance-inbox", tags=["Assurance Inbox"])
    service = AssuranceInboxService(logger)

    # One tenant-scoping rule for every admin route — see
    # app/core/admin_user/dependencies.py::resolve_tenant_id.
    _tenant_of = resolve_tenant_id

    def _get_item(db: Session, tenant_id: str, item_id: str) -> AssuranceInboxItem:
        item = (
            db.query(AssuranceInboxItem)
            .filter(
                AssuranceInboxItem.id == item_id,
                AssuranceInboxItem.tenant_id == tenant_id,
            )
            .first()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Inbox item not found")
        return item

    # ── Reads ───────────────────────────────────────────────────────────

    @router.get("", response_model=InboxListResponse, summary="List inbox items")
    def list_items(
        state: Optional[str] = Query(None),
        severity: Optional[str] = Query(None),
        category: Optional[str] = Query(None),
        ai_system_id: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(
            None, description="Required for system admins; ignored for tenant admins"
        ),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_READ)),
    ) -> InboxListResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        q = db.query(AssuranceInboxItem).filter(
            AssuranceInboxItem.tenant_id == tenant_id
        )
        if state is not None:
            if state not in INBOX_STATES:
                raise HTTPException(status_code=422, detail=f"Unknown state '{state}'")
            q = q.filter(AssuranceInboxItem.state == state)
        if severity is not None:
            if severity not in INBOX_SEVERITIES:
                raise HTTPException(
                    status_code=422, detail=f"Unknown severity '{severity}'"
                )
            q = q.filter(AssuranceInboxItem.severity == severity)
        if category is not None:
            if category not in INBOX_CATEGORIES:
                raise HTTPException(
                    status_code=422, detail=f"Unknown category '{category}'"
                )
            q = q.filter(AssuranceInboxItem.category == category)
        if ai_system_id is not None:
            q = q.filter(AssuranceInboxItem.ai_system_id == ai_system_id)
        total = q.count()
        rows = (
            q.order_by(
                AssuranceInboxItem.last_seen_at.desc(), AssuranceInboxItem.id.desc()
            )
            .limit(limit)
            .offset(offset)
            .all()
        )
        return InboxListResponse(
            items=[InboxItemResponse(**row.to_dict()) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get(
        "/counts", response_model=InboxCountsResponse, summary="Inbox badge counts"
    )
    def get_counts(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_READ)),
    ) -> InboxCountsResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        return InboxCountsResponse(**service.counts(db, tenant_id=tenant_id))

    @router.get(
        "/{item_id}",
        response_model=InboxItemDetailResponse,
        summary="Inbox item with its event trail",
    )
    def get_item(
        item_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_READ)),
    ) -> InboxItemDetailResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        item = _get_item(db, tenant_id, item_id)
        events = (
            db.query(AssuranceInboxEvent)
            .filter(AssuranceInboxEvent.item_id == item.id)
            .order_by(AssuranceInboxEvent.at.asc())
            .all()
        )
        return InboxItemDetailResponse(
            **item.to_dict(),
            events=[InboxEventResponse(**e.to_dict()) for e in events],
        )

    # ── Mutations (audited) ─────────────────────────────────────────────

    @router.post(
        "/{item_id}/acknowledge",
        response_model=InboxItemResponse,
        summary="Acknowledge an item",
    )
    def acknowledge_item(
        item_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_MANAGE)),
    ) -> InboxItemResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        item = _get_item(db, tenant_id, item_id)
        try:
            service.acknowledge(db, item, admin=admin.username)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        audit.log(
            action="assurance_inbox.acknowledge",
            db=db,
            admin=admin,
            resource_type="assurance_inbox_item",
            resource_id=item.id,
            tenant_id=tenant_id,
        )
        db.commit()
        return InboxItemResponse(**item.to_dict())

    @router.post(
        "/{item_id}/assign",
        response_model=InboxItemResponse,
        summary="Assign an item to an admin",
    )
    def assign_item(
        item_id: str,
        body: AssignRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_MANAGE)),
    ) -> InboxItemResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        item = _get_item(db, tenant_id, item_id)
        try:
            service.assign(db, item, admin=admin.username, assignee=body.assignee)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        audit.log(
            action="assurance_inbox.assign",
            db=db,
            admin=admin,
            resource_type="assurance_inbox_item",
            resource_id=item.id,
            tenant_id=tenant_id,
            details={"assignee": body.assignee},
        )
        db.commit()
        return InboxItemResponse(**item.to_dict())

    @router.post(
        "/{item_id}/resolve",
        response_model=InboxItemResponse,
        summary="Resolve an item with a disposition",
    )
    def resolve_item(
        item_id: str,
        body: AssuranceInboxResolveRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_MANAGE)),
    ) -> InboxItemResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        item = _get_item(db, tenant_id, item_id)
        try:
            service.resolve(
                db, item, admin=admin.username,
                disposition=body.disposition, note=body.note,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        audit.log(
            action="assurance_inbox.resolve",
            db=db,
            admin=admin,
            resource_type="assurance_inbox_item",
            resource_id=item.id,
            tenant_id=tenant_id,
            details={"disposition": body.disposition},
        )
        db.commit()
        return InboxItemResponse(**item.to_dict())

    @router.post(
        "/{item_id}/reopen",
        response_model=InboxItemResponse,
        summary="Reopen a resolved item",
    )
    def reopen_item(
        item_id: str,
        body: ReopenRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_MANAGE)),
    ) -> InboxItemResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        item = _get_item(db, tenant_id, item_id)
        try:
            service.reopen(db, item, admin=admin.username, reason=body.reason)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        audit.log(
            action="assurance_inbox.reopen",
            db=db,
            admin=admin,
            resource_type="assurance_inbox_item",
            resource_id=item.id,
            tenant_id=tenant_id,
            details={"reason": body.reason},
        )
        db.commit()
        return InboxItemResponse(**item.to_dict())

    @router.post(
        "/bulk-acknowledge",
        response_model=InboxCountsResponse,
        summary="Acknowledge many items at once",
    )
    def bulk_acknowledge(
        body: BulkAcknowledgeRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.ASSURANCE_INBOX_MANAGE)),
    ) -> InboxCountsResponse:
        admin, admin_type = admin_and_type
        tenant_id = _tenant_of(admin, admin_type, tenant_id)
        acknowledged = 0
        for item_id in body.item_ids:
            item = (
                db.query(AssuranceInboxItem)
                .filter(
                    AssuranceInboxItem.id == item_id,
                    AssuranceInboxItem.tenant_id == tenant_id,
                    AssuranceInboxItem.state == "open",
                )
                .first()
            )
            if item is not None:
                service.acknowledge(db, item, admin=admin.username)
                acknowledged += 1
        audit.log(
            action="assurance_inbox.bulk_acknowledge",
            db=db,
            admin=admin,
            resource_type="assurance_inbox_item",
            resource_id=None,
            tenant_id=tenant_id,
            details={"requested": len(body.item_ids), "acknowledged": acknowledged},
        )
        db.commit()
        return InboxCountsResponse(**service.counts(db, tenant_id=tenant_id))

    return router

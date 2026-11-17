# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tool Approval Admin Endpoints

Admin endpoints for reviewing and resolving tool approval requests.
These are used by the Admin UI to manage pending approval_required tool calls.

Access control:
- TOOL_APPROVAL_READ: View approval requests and history
- TOOL_APPROVAL_MANAGE: Approve or reject pending requests
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Query, Request, status
from sqlalchemy import and_, func as sa_func, not_, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.database.models.tool_approval_request import ToolApprovalRequest
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    acting_tenant_id,
    require_permission,
    get_audit_logger,
    get_current_admin,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.api.v1.models.admin.tool_approval import (
    ToolApprovalSummary,
    ToolApprovalDetail,
    ToolApprovalListResponse,
    ToolApprovalDetailResponse,
    ToolApprovalResolveRequest,
    ToolApprovalResolveResponse,
    ToolApprovalCountResponse,
)


from app.constants import approvals as approval_status
from app.constants import approval_decision
from app.services.evidence import core_client


def create_tool_approvals_admin_router(logger: BrutorLogger) -> APIRouter:
    """Create admin router for tool approval management."""

    router = APIRouter(prefix="/admin/approvals", tags=["Tool Approvals"])

    # =========================================================================
    # LIST APPROVAL REQUESTS
    # =========================================================================

    @router.get(
        "",
        response_model=ToolApprovalListResponse,
        summary="List tool approval requests",
        description="List and filter tool approval requests with pagination.",
    )
    def list_approvals(
        request: Request,
        status_filter: str = Query(
            approval_status.PENDING,
            description=(
                "Filter by status: pending, approved (includes consumed — the token "
                "was redeemed), consumed, rejected, expired (includes pending rows "
                "past their deadline), all"
            ),
        ),
        group_id: str = Query(None, description="Filter by resource group ID"),
        trace_root_id: str = Query(
            None,
            min_length=1,
            max_length=64,
            description=(
                "Only the holds raised by one call: those whose "
                "`originating_trace_root_id` is this value. Applied with the other filters."
            ),
        ),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        admin=Depends(require_permission(Permission.TOOL_APPROVAL_READ)),
        db: Session = Depends(get_db),
    ):
        tenant_id = acting_tenant_id(request, admin)
        query = db.query(ToolApprovalRequest).filter(
            ToolApprovalRequest.tenant_id == tenant_id
        )

        if status_filter not in approval_status.FILTERS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status_filter must be one of {list(approval_status.FILTERS)}",
            )
        now = datetime.now(timezone.utc)
        lapsed = and_(
            ToolApprovalRequest.status == approval_status.PENDING,
            ToolApprovalRequest.expires_at.isnot(None),
            ToolApprovalRequest.expires_at < now,
        )
        if status_filter == approval_status.PENDING:
            query = query.filter(ToolApprovalRequest.status == approval_status.PENDING, not_(lapsed))
        elif status_filter == approval_status.APPROVED:
            query = query.filter(ToolApprovalRequest.status.in_(approval_status.APPROVED_STATUSES))
        elif status_filter == approval_status.EXPIRED:
            query = query.filter(or_(ToolApprovalRequest.status == approval_status.EXPIRED, lapsed))
        elif status_filter != approval_status.FILTER_ALL:
            query = query.filter(ToolApprovalRequest.status == status_filter)
        if group_id:
            query = query.filter(ToolApprovalRequest.requester_group_id == group_id)
        if trace_root_id:
            query = query.filter(ToolApprovalRequest.originating_trace_root_id == trace_root_id)

        total = query.count()
        items = (
            query.order_by(ToolApprovalRequest.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        return ToolApprovalListResponse(
            items=[
                ToolApprovalSummary(
                    id=r.id,
                    server_name=r.server_name,
                    capability_type=r.capability_type,
                    capability_name=r.capability_name,
                    requester_type=r.requester_type,
                    requester_display=r.requester_display,
                    requester_group_id=r.requester_group_id,
                    status=r.status,
                    decision=approval_status.decision(r.status, r.expires_at, now),
                    token_used=approval_status.token_used(r.status),
                    timeout_seconds=r.timeout_seconds,
                    expires_at=r.expires_at.isoformat() if r.expires_at else None,
                    created_at=r.created_at.isoformat() if r.created_at else None,
                    originating_trace_root_id=r.originating_trace_root_id,
                    originating_trace_root_kind=r.originating_trace_root_kind,
                )
                for r in items
            ],
            total=total,
            page=page,
            page_size=page_size,
            status_filter=status_filter,
        )

    # =========================================================================
    # GET PENDING COUNT
    # =========================================================================

    @router.get(
        "/pending-count",
        response_model=ToolApprovalCountResponse,
        summary="Get pending approval count",
    )
    def get_pending_count(
        request: Request,
        admin=Depends(require_permission(Permission.TOOL_APPROVAL_READ)),
        db: Session = Depends(get_db),
    ):
        count = (
            db.query(sa_func.count(ToolApprovalRequest.id))
            .filter(
                ToolApprovalRequest.tenant_id == acting_tenant_id(request, admin),
                ToolApprovalRequest.status == approval_status.PENDING,
                # A request past its deadline is not actionable, even before the
                # core's sweeper rewrites it to 'expired'.
                or_(
                    ToolApprovalRequest.expires_at.is_(None),
                    ToolApprovalRequest.expires_at >= datetime.now(timezone.utc),
                ),
            )
            .scalar()
        )
        return ToolApprovalCountResponse(pending_count=count or 0)

    # =========================================================================
    # GET APPROVAL DETAIL
    # =========================================================================

    @router.get(
        "/{approval_id}",
        response_model=ToolApprovalDetailResponse,
        summary="Get approval request detail",
    )
    def get_approval_detail(
        request: Request,
        approval_id: str,
        admin=Depends(require_permission(Permission.TOOL_APPROVAL_READ)),
        db: Session = Depends(get_db),
    ):
        req = (
            db.query(ToolApprovalRequest)
            .filter(
                ToolApprovalRequest.id == approval_id,
                ToolApprovalRequest.tenant_id == acting_tenant_id(request, admin),
            )
            .first()
        )
        if not req:
            raise HTTPException(status_code=404, detail="Approval request not found")

        return ToolApprovalDetailResponse(
            approval=ToolApprovalDetail(**req.to_dict())
        )

    # =========================================================================
    # APPROVE / REJECT — decided by the core
    # =========================================================================
    #
    # The control plane authorises the administrator and records the act in
    # the admin audit log. It does not write the request: the gateway core
    # decides, through the same function as its own approve/reject routes,
    # because the decision row the core writes is what mints the `resolved`
    # evidence record with a human disposition, what the run ledger reads and
    # what the compliance resolvers count. A request past its
    # deadline is expired and sealed by the core; a decision is made
    # once. If the core cannot be reached nothing is decided: 503.

    def _decide(approval_id, decision, body, admin, tenant_id, audit_logger, db):
        req = (
            db.query(ToolApprovalRequest)
            .filter(
                ToolApprovalRequest.id == approval_id,
                ToolApprovalRequest.tenant_id == tenant_id,
            )
            .first()
        )
        if not req:
            raise HTTPException(status_code=404, detail="Approval request not found")
        note = body.note if body else None

        result = core_client.decide_approval(
            tenant_id,
            req.id,
            decision,
            approver_id=str(admin.id),
            approver_username=getattr(admin, "username", None) or str(admin.id),
            note=note,
        )
        if core_client.is_error(result):
            logger.error(
                "tool approval not decided: the gateway core did not answer",
                context={"approval_request_id": req.id, "error": result.get("error"),
                         "detail": result.get("detail")},
            )
            raise HTTPException(
                status_code=503,
                detail="The approval could not be decided: the gateway core is unavailable. "
                       "Nothing was changed; try again.",
            )

        outcome = result["outcome"]
        if outcome == approval_decision.NOT_FOUND:
            raise HTTPException(status_code=404, detail="Approval request not found")
        if outcome == approval_decision.EXPIRED:
            raise HTTPException(status_code=410, detail="Approval request has expired")
        if outcome == approval_decision.ALREADY_RESOLVED:
            raise HTTPException(
                status_code=409,
                detail=f"Request already resolved with status: {result.get('status')}",
            )
        if outcome == approval_decision.NO_LONGER_PENDING:
            raise HTTPException(
                status_code=409,
                detail="Approval is no longer pending — it was already resolved or has expired",
            )

        # approved | rejected (decide_approval refuses any other outcome)
        details = {
            "capability": req.capability_name,
            "server": req.server_name,
            "requester": req.requester_display,
        }
        if decision == approval_decision.REJECT:
            details["note"] = note
        audit_logger.log(
            action=f"tool_approval.{decision}",
            db=db,
            admin=admin,
            tenant_id=tenant_id,
            resource_type="tool_approval_request",
            resource_id=req.id,
            details=details,
        )
        return ToolApprovalResolveResponse(
            id=req.id,
            status=result["status"],
            resolved_by=result["resolved_by"],
            resolved_at=result["resolved_at"],
            approval_token=result.get("approval_token"),
            message=f"Tool call '{req.capability_name}' {result['status']}",
        )

    @router.post(
        "/{approval_id}/approve",
        response_model=ToolApprovalResolveResponse,
        summary="Approve a tool call request",
    )
    def approve_request(
        approval_id: str,
        request: Request,
        body: ToolApprovalResolveRequest = None,
        admin=Depends(require_permission(Permission.TOOL_APPROVAL_MANAGE)),
        audit_logger: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db),
    ):
        return _decide(approval_id, approval_decision.APPROVE, body, admin, acting_tenant_id(request, admin), audit_logger, db)

    @router.post(
        "/{approval_id}/reject",
        response_model=ToolApprovalResolveResponse,
        summary="Reject a tool call request",
    )
    def reject_request(
        approval_id: str,
        request: Request,
        body: ToolApprovalResolveRequest = None,
        admin=Depends(require_permission(Permission.TOOL_APPROVAL_MANAGE)),
        audit_logger: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db),
    ):
        return _decide(approval_id, approval_decision.REJECT, body, admin, acting_tenant_id(request, admin), audit_logger, db)

    return router

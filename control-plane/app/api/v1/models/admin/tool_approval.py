# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic models for Tool Approval Request endpoints (Admin + Portal).
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ToolApprovalSummary(BaseModel):
    """Compact approval request for listing views"""
    id: str
    server_name: Optional[str] = None
    capability_type: str
    capability_name: str
    requester_type: str
    requester_display: Optional[str] = None
    requester_group_id: str
    status: str
    #: pending | approved | rejected | expired | unknown — derived from status;
    #: `approved` covers `consumed`, `expired` covers a pending row past its deadline.
    decision: Optional[str] = None
    #: True once the one-time approval token has been redeemed (status consumed).
    token_used: Optional[bool] = None
    timeout_seconds: int
    expires_at: Optional[str] = None
    created_at: Optional[str] = None
    originating_trace_root_id: Optional[str] = Field(
        None,
        description=(
            "The trace root of the call that was held. Holds raised by one "
            "call have the same value; null for a hold the gateway raised "
            "outside a traced call."
        ),
    )
    originating_trace_root_kind: Optional[str] = Field(
        None,
        description="What kind of call the trace root is, as the gateway labelled it; null with the root.",
    )


class ToolApprovalDetail(BaseModel):
    """Full approval request detail"""
    id: str
    tenant_id: str
    #: NULL unless the hold is an MCP one whose server the core resolved: an
    #: A2A call or a skill execution has no server configuration (the column
    #: is nullable).
    server_config_id: Optional[str] = None
    server_name: Optional[str] = None
    capability_type: str
    capability_name: str
    requester_type: str
    requester_user_id: Optional[str] = None
    requester_api_key_id: Optional[str] = None
    requester_group_id: str
    requester_display: Optional[str] = None
    call_arguments: Optional[Dict[str, Any]] = None
    request_metadata: Optional[Dict[str, Any]] = None
    status: str
    #: pending | approved | rejected | expired | unknown — derived from status;
    #: `approved` covers `consumed`, `expired` covers a pending row past its deadline.
    decision: Optional[str] = None
    #: True once the one-time approval token has been redeemed (status consumed).
    token_used: Optional[bool] = None
    timeout_seconds: int
    expires_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_note: Optional[str] = None
    created_at: Optional[str] = None
    originating_trace_root_id: Optional[str] = Field(
        None,
        description=(
            "The trace root of the call that was held. Holds raised by one "
            "call have the same value; null for a hold the gateway raised "
            "outside a traced call."
        ),
    )
    originating_trace_root_kind: Optional[str] = Field(
        None,
        description="What kind of call the trace root is, as the gateway labelled it; null with the root.",
    )


class ToolApprovalListResponse(BaseModel):
    """Paginated list of approval requests"""
    items: List[ToolApprovalSummary]
    total: int
    page: int
    page_size: int
    status_filter: Optional[str] = None


class ToolApprovalDetailResponse(BaseModel):
    """Single approval request detail response"""
    approval: ToolApprovalDetail


class ToolApprovalResolveRequest(BaseModel):
    """Request body for approve/reject actions"""
    note: Optional[str] = Field(None, max_length=1000, description="Optional note explaining the decision")


class ToolApprovalResolveResponse(BaseModel):
    """Response after approving or rejecting"""
    id: str
    status: str
    resolved_by: str
    resolved_at: str
    approval_token: Optional[str] = None
    message: str


class ToolApprovalCountResponse(BaseModel):
    """Pending approval count for badge display"""
    pending_count: int


class NotificationItem(BaseModel):
    """Single notification"""
    id: str
    notification_type: str
    title: str
    body: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    is_read: bool
    read_at: Optional[str] = None
    created_at: Optional[str] = None


class NotificationListResponse(BaseModel):
    """Paginated notification list"""
    items: List[NotificationItem]
    total: int
    page: int
    page_size: int


class NotificationUnreadCountResponse(BaseModel):
    """Unread count for badge display"""
    unread_count: int


class NotificationMarkReadResponse(BaseModel):
    """Response after marking notification(s) as read"""
    message: str
    count: int = 0

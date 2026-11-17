# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tool Approval Request Model

Tracks pending approvals for MCP tool calls that require human-in-the-loop
approval before execution. Created when a user or agent attempts to call
a tool with access='approval_required'.

Lifecycle:
  pending → approved  (group member or admin approves)
  pending → rejected  (group member or admin rejects)
  pending → expired   (timeout elapsed without approval)
"""

from sqlalchemy import Column, String, Integer, DateTime, Text, Index, JSON
from sqlalchemy.sql import func
from typing import Dict, Any, Optional

from app.database import Base
from app.constants import approvals as approval_status


class ToolApprovalRequest(Base):
    """
    Tool Approval Request — records a pending approval for an MCP tool call.

    Created by the Rust proxy when a tool with access='approval_required' is called.
    Group members and admins can approve or reject via Admin UI or Portal.
    """
    __tablename__ = "tool_approval_requests"

    # ==================== Primary Key ====================
    id = Column(String(64), primary_key=True, comment="ULID")

    # ==================== Multi-tenancy ====================
    tenant_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Tenant this request belongs to"
    )

    # ==================== What needs approval ====================
    server_config_id = Column(
        String(255),
        nullable=True,
        comment="Server config that owns the capability (MCP only; NULL for A2A/skill approvals)"
    )

    server_name = Column(
        String(500),
        nullable=True,
        comment="Server name at time of request (snapshot for display)"
    )

    capability_type = Column(
        String(50),
        nullable=False,
        comment="Capability type: tool, resource, prompt, resource_template"
    )

    capability_name = Column(
        String(500),
        nullable=False,
        comment="Tool name, resource URI, prompt name, etc."
    )

    # ==================== Who requested ====================
    requester_type = Column(
        String(20),
        nullable=False,
        comment="Requester type: 'portal_user', 'api_key', or 'agent'"
    )

    requester_user_id = Column(
        String(255),
        nullable=True,
        comment="Gateway user ID (for portal_user requests)"
    )

    requester_api_key_id = Column(
        String(255),
        nullable=True,
        comment="API key ID (for api_key requests)"
    )

    requester_group_id = Column(
        String(255),
        nullable=False,
        comment="Resource group the requester belongs to"
    )

    requester_display = Column(
        String(500),
        nullable=True,
        comment="Human-readable requester identifier (snapshot)"
    )

    # ==================== Call context ====================
    call_arguments = Column(
        JSON,
        nullable=True,
        comment="Tool call arguments for reviewer inspection"
    )

    request_metadata = Column(
        JSON,
        nullable=True,
        comment="Extra context: IP address, conversation_id, agent_id, etc."
    )

    # ==================== Lifecycle ====================
    status = Column(
        String(20),
        nullable=False,
        default="pending",
        comment="Request status: pending, approved, consumed (approved, token redeemed), rejected, expired"
    )

    approval_token = Column(
        String(128),
        nullable=True,
        comment="Crypto-random token issued on approval for re-submission"
    )

    token_used = Column(
        String(1),
        nullable=False,
        default="N",
        server_default="N",
        comment="Y if the approval token has been consumed (one-time use)"
    )

    # ==================== Timeout ====================
    timeout_seconds = Column(
        Integer,
        nullable=False,
        comment="Approval timeout from capability config or tenant default"
    )

    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="When this request expires (created_at + timeout_seconds)"
    )

    # ==================== Resolution ====================
    resolved_by = Column(
        String(255),
        nullable=True,
        comment="User ID who approved or rejected"
    )

    resolved_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the request was approved/rejected"
    )

    resolution_note = Column(
        Text,
        nullable=True,
        comment="Optional note from the approver/rejector"
    )

    # ==================== Call chain ====================
    # The decision audit row inherits these
    # so the trace tree stays connected from "approval required" → "approved".
    originating_trace_root_id = Column(
        String(64),
        nullable=True,
        comment="Trace root_task_id from the originating request"
    )

    originating_parent_task_id = Column(
        String(64),
        nullable=True,
        comment="current_task_id of the mcp.rs node that minted the approval"
    )

    originating_trace_root_kind = Column(
        String(64),
        nullable=True,
        comment="Root-kind label from the originating request"
    )

    # ==================== Timestamps ====================
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    # ==================== Constraints & Indexes ====================
    __table_args__ = (
        # Fast lookup of pending requests for a tenant
        Index('ix_tar_pending',
              'tenant_id', 'status', 'expires_at'),

        # Lookup by approval token (for proxy validation)
        Index('ix_tar_token',
              'approval_token'),

        # Group-scoped listing
        Index('ix_tar_group',
              'tenant_id', 'requester_group_id', 'status'),

        # User-scoped listing (portal inbox)
        Index('ix_tar_requester',
              'tenant_id', 'requester_user_id', 'created_at'),

        # A run's holds, by its root. Read on every run finalization
        # (awaiting_human_ms) and by the gateway's live run clock.
        Index('ix_tar_trace_root',
              'tenant_id', 'originating_trace_root_id'),

        {
            'comment': 'MCP tool approval requests for human-in-the-loop governance'
        }
    )

    def __repr__(self) -> str:
        return (
            f"<ToolApprovalRequest(id='{self.id}', tool='{self.capability_name}', "
            f"status='{self.status}')>"
        )

    # No `is_pending` / `is_approved` / `is_expired` here: the status is read
    # through `app.constants.approvals` (`decision`, `is_decided`, `is_lapsed`),
    # the one vocabulary both planes share. The three properties that stood
    # here compared single literals and had no callers.

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "server_config_id": self.server_config_id,
            "server_name": self.server_name,
            "capability_type": self.capability_type,
            "capability_name": self.capability_name,
            "requester_type": self.requester_type,
            "requester_user_id": self.requester_user_id,
            "requester_api_key_id": self.requester_api_key_id,
            "requester_group_id": self.requester_group_id,
            "requester_display": self.requester_display,
            "call_arguments": self.call_arguments,
            "request_metadata": self.request_metadata,
            "status": self.status,
            "decision": approval_status.decision(self.status, self.expires_at),
            "token_used": approval_status.token_used(self.status),
            "timeout_seconds": self.timeout_seconds,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_note": self.resolution_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "originating_trace_root_id": self.originating_trace_root_id,
            "originating_parent_task_id": self.originating_parent_task_id,
            "originating_trace_root_kind": self.originating_trace_root_kind,
        }

    def to_summary(self) -> Dict[str, Any]:
        """Compact summary for listing views"""
        return {
            "id": self.id,
            "server_name": self.server_name,
            "capability_type": self.capability_type,
            "capability_name": self.capability_name,
            "requester_type": self.requester_type,
            "requester_display": self.requester_display,
            "status": self.status,
            "decision": approval_status.decision(self.status, self.expires_at),
            "token_used": approval_status.token_used(self.status),
            "timeout_seconds": self.timeout_seconds,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

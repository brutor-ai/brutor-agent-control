# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Execution Model
Audit trail for every skill execution through the gateway.

Design decisions:
- SET NULL on foreign keys (not CASCADE) because execution history must
  survive if a skill, binding, group, or user is later deleted.
- parent_execution_id enables skill composition tracking — when skill A
  triggers skill B, both executions link via this field and share a
  correlation_id.
- execution_context JSON captures the full resolved state at execution time
  (which specific servers, models, and credentials were used), making audit
  and replay possible even if bindings change later.
- request_id correlates with ProxyLog, ProxyMetricEntry, and UsageLog
  entries generated during this execution.

Execution lifecycle:
  PENDING → RUNNING → COMPLETED | FAILED | CANCELLED | TIMED_OUT

Approval lifecycle (when requires_approval=True on binding):
  NOT_REQUIRED | PENDING → APPROVED | REJECTED | EXPIRED
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text,
    Index, ForeignKey, JSON, Enum as SQLEnum
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.constants.agent_skill import ExecutionStatus, ApprovalStatus


class AgentSkillExecution(Base):
    """
    Skill execution audit record

    One row per skill execution attempt, including pending approvals and
    failed/cancelled executions. Provides full traceability from request
    through resource resolution to outcome.
    """
    __tablename__ = "agent_skill_executions"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: ase-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this execution belongs to"
    )

    # =========================================================================
    # EXECUTION REFERENCES (SET NULL — history survives deletion)
    # =========================================================================

    skill_id = Column(
        String(255),
        ForeignKey("agent_skills.id", ondelete="SET NULL"),
        nullable=True,
        comment="Skill that was executed (NULL if skill was deleted)"
    )

    group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment="Resource group context (NULL if group was deleted)"
    )

    user_id = Column(
        Integer,
        ForeignKey("end_users.id", ondelete="SET NULL"),
        nullable=True,
        comment="End user who triggered execution (NULL if user was deleted)"
    )

    # =========================================================================
    # DENORMALIZED IDENTIFIERS (survive reference deletion)
    # =========================================================================

    skill_name = Column(
        String(64),
        nullable=True,
        comment="Skill name at execution time (denormalized for history)"
    )

    skill_version = Column(
        String(50),
        nullable=True,
        comment="Skill version at execution time (denormalized for history)"
    )

    group_name = Column(
        String(255),
        nullable=True,
        comment="Group name at execution time (denormalized for history)"
    )

    username = Column(
        String(255),
        nullable=True,
        comment="Username at execution time (denormalized for history)"
    )

    # =========================================================================
    # CORRELATION
    # =========================================================================

    request_id = Column(
        String(255),
        nullable=False,
        comment="Correlates with proxy_logs, proxy_metrics, usage_logs"
    )

    correlation_id = Column(
        String(255),
        nullable=True,
        comment="Shared across chained skill executions (same workflow)"
    )

    parent_execution_id = Column(
        String(255),
        ForeignKey("agent_skill_executions.id", ondelete="SET NULL"),
        nullable=True,
        comment="Parent execution when this skill was triggered by another skill"
    )

    # =========================================================================
    # EXECUTION STATUS
    # =========================================================================

    execution_status = Column(
        SQLEnum(ExecutionStatus),
        nullable=False,
        default=ExecutionStatus.PENDING,
        comment="Execution lifecycle: pending → running → completed | failed | cancelled | timed_out"
    )

    started_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When execution was initiated (may precede actual start if pending approval)"
    )

    running_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When execution actually began running (after approval if required)"
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When execution finished (completed, failed, cancelled, or timed out)"
    )

    duration_ms = Column(
        Integer,
        nullable=True,
        comment="Execution duration in milliseconds (running_at to completed_at)"
    )

    # =========================================================================
    # RESOURCES CONSUMED
    # =========================================================================

    mcp_servers_used = Column(
        JSON,
        nullable=True,
        comment="""MCP servers accessed during execution:
        [{"server_config_id": "sc-01J...", "server_name": "Snowflake",
          "tools_called": ["query", "list_tables"], "call_count": 3}]"""
    )

    llm_models_used = Column(
        JSON,
        nullable=True,
        comment="""LLM models used during execution:
        [{"model_id": "llm-01J...", "model_name": "gpt-4o",
          "prompt_tokens": 1200, "completion_tokens": 800}]"""
    )

    total_tokens = Column(
        Integer,
        nullable=True,
        comment="Total tokens consumed across all LLM calls"
    )

    total_mcp_calls = Column(
        Integer,
        nullable=True,
        comment="Total MCP tool/resource/prompt calls made"
    )

    estimated_cost_usd = Column(
        Float,
        nullable=True,
        comment="Estimated total cost in USD"
    )

    # =========================================================================
    # APPROVAL WORKFLOW
    # =========================================================================

    approval_status = Column(
        SQLEnum(ApprovalStatus),
        nullable=False,
        default=ApprovalStatus.NOT_REQUIRED,
        comment="Approval lifecycle: not_required | pending → approved | rejected | expired"
    )

    approved_by = Column(
        String(255),
        nullable=True,
        comment="Admin who approved/rejected the execution"
    )

    approved_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When approval decision was made"
    )

    approval_notes = Column(
        Text,
        nullable=True,
        comment="Reviewer notes on approval decision"
    )

    approval_expires_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the approval request expires (auto-reject after this time)"
    )

    # =========================================================================
    # ERROR HANDLING
    # =========================================================================

    error_type = Column(
        String(100),
        nullable=True,
        comment="Error classification (e.g. 'authorization', 'timeout', 'mcp_error', 'llm_error')"
    )

    error_message = Column(
        Text,
        nullable=True,
        comment="Human-readable error description"
    )

    error_details = Column(
        JSON,
        nullable=True,
        comment="Structured error details (stack trace, server responses, etc.)"
    )

    # =========================================================================
    # EXECUTION CONTEXT SNAPSHOT
    # =========================================================================

    execution_context = Column(
        JSON,
        nullable=True,
        comment="""Full resolved context at execution time for audit and replay:
        {
            "input_params": {...},
            "resolved_mcp_servers": {"query_data": "sc-01J..."},
            "resolved_llm_models": {"summarize": "llm-01J..."},
            "resolved_oauth_client": "oac-01J...",
            "binding_config": {...},
            "group_skill_limits": {...},
            "user_permissions": ["skill:execute"]
        }"""
    )

    # =========================================================================
    # AUDIT
    # =========================================================================

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    skill = relationship(
        "AgentSkill",
        back_populates="executions",
        foreign_keys=[skill_id]
    )

    group = relationship("ResourceGroup")

    user = relationship("EndUser")

    # Self-referential: parent/child executions for skill composition
    parent_execution = relationship(
        "AgentSkillExecution",
        remote_side="AgentSkillExecution.id",
        back_populates="child_executions",
        foreign_keys=[parent_execution_id]
    )

    child_executions = relationship(
        "AgentSkillExecution",
        back_populates="parent_execution",
        foreign_keys=[parent_execution_id],
        lazy="dynamic"
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # Time-series queries: "Get executions for a skill in the last 24 hours"
        Index('ix_skill_exec_tenant_skill_started',
              'tenant_id', 'skill_id', 'started_at'),

        # User activity: "Get all executions by a user"
        Index('ix_skill_exec_tenant_user_started',
              'tenant_id', 'user_id', 'started_at'),

        # Group activity: "Get all executions in a group"
        Index('ix_skill_exec_tenant_group_started',
              'tenant_id', 'group_id', 'started_at'),

        # Cross-system correlation: "Find all logs for this request"
        Index('ix_skill_exec_request_id', 'request_id'),

        # Workflow correlation: "Find all executions in a chained workflow"
        Index('ix_skill_exec_tenant_correlation', 'tenant_id', 'correlation_id'),

        # Skill composition: "Find child executions of a parent"
        Index('ix_skill_exec_parent', 'parent_execution_id'),

        # Approval queue: "Get all pending approvals for tenant"
        Index('ix_skill_exec_tenant_approval',
              'tenant_id', 'approval_status', 'started_at'),

        # Status monitoring: "Get all running/failed executions"
        Index('ix_skill_exec_tenant_status',
              'tenant_id', 'execution_status', 'started_at'),

        # Chronological audit
        Index('ix_skill_exec_tenant_created', 'tenant_id', 'created_at'),

        {
            'comment': 'Skill execution audit trail with approval workflow and correlation'
        }
    )

    # =========================================================================
    # LIFECYCLE METHODS
    # =========================================================================

    def start_running(self) -> None:
        """Transition from PENDING to RUNNING"""
        if self.execution_status != ExecutionStatus.PENDING:
            raise ValueError(f"Cannot start execution in '{self.execution_status.value}' status")
        self.execution_status = ExecutionStatus.RUNNING
        self.running_at = datetime.now(timezone.utc)

    def complete(
        self,
        total_tokens: Optional[int] = None,
        total_mcp_calls: Optional[int] = None,
        estimated_cost_usd: Optional[float] = None,
        mcp_servers_used: Optional[List[Dict]] = None,
        llm_models_used: Optional[List[Dict]] = None,
    ) -> None:
        """Mark execution as successfully completed"""
        now = datetime.now(timezone.utc)
        self.execution_status = ExecutionStatus.COMPLETED
        self.completed_at = now
        if self.running_at:
            self.duration_ms = int((now - self.running_at).total_seconds() * 1000)
        self.total_tokens = total_tokens
        self.total_mcp_calls = total_mcp_calls
        self.estimated_cost_usd = estimated_cost_usd
        if mcp_servers_used is not None:
            self.mcp_servers_used = mcp_servers_used
        if llm_models_used is not None:
            self.llm_models_used = llm_models_used

    def fail(
        self,
        error_type: str,
        error_message: str,
        error_details: Optional[Dict] = None,
    ) -> None:
        """Mark execution as failed"""
        now = datetime.now(timezone.utc)
        self.execution_status = ExecutionStatus.FAILED
        self.completed_at = now
        if self.running_at:
            self.duration_ms = int((now - self.running_at).total_seconds() * 1000)
        self.error_type = error_type
        self.error_message = error_message
        self.error_details = error_details

    def cancel(self, reason: str = "Cancelled by user") -> None:
        """Mark execution as cancelled"""
        self.execution_status = ExecutionStatus.CANCELLED
        self.completed_at = datetime.now(timezone.utc)
        self.error_message = reason

    def timeout(self) -> None:
        """Mark execution as timed out"""
        now = datetime.now(timezone.utc)
        self.execution_status = ExecutionStatus.TIMED_OUT
        self.completed_at = now
        if self.running_at:
            self.duration_ms = int((now - self.running_at).total_seconds() * 1000)
        self.error_type = "timeout"
        self.error_message = "Execution exceeded time limit"

    # =========================================================================
    # APPROVAL METHODS
    # =========================================================================

    def request_approval(self, expires_at: Optional[datetime] = None) -> None:
        """Set execution to pending approval"""
        self.approval_status = ApprovalStatus.PENDING
        self.approval_expires_at = expires_at

    def approve(self, approved_by: str, notes: Optional[str] = None) -> None:
        """Approve execution"""
        if self.approval_status != ApprovalStatus.PENDING:
            raise ValueError(f"Cannot approve execution in '{self.approval_status.value}' status")
        self.approval_status = ApprovalStatus.APPROVED
        self.approved_by = approved_by
        self.approved_at = datetime.now(timezone.utc)
        self.approval_notes = notes

    def reject(self, rejected_by: str, notes: Optional[str] = None) -> None:
        """Reject execution"""
        if self.approval_status != ApprovalStatus.PENDING:
            raise ValueError(f"Cannot reject execution in '{self.approval_status.value}' status")
        self.approval_status = ApprovalStatus.REJECTED
        self.approved_by = rejected_by
        self.approved_at = datetime.now(timezone.utc)
        self.approval_notes = notes
        self.execution_status = ExecutionStatus.CANCELLED
        self.completed_at = datetime.now(timezone.utc)
        self.error_message = f"Approval rejected: {notes}" if notes else "Approval rejected"

    def is_approval_expired(self) -> bool:
        """Check if approval window has elapsed"""
        if self.approval_status != ApprovalStatus.PENDING:
            return False
        if not self.approval_expires_at:
            return False
        now = datetime.now(timezone.utc)
        expires = self.approval_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now > expires

    # =========================================================================
    # QUERY HELPERS
    # =========================================================================

    def is_terminal(self) -> bool:
        """Check if execution is in a terminal state"""
        return self.execution_status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMED_OUT,
        )

    def is_successful(self) -> bool:
        """Check if execution completed successfully"""
        return self.execution_status == ExecutionStatus.COMPLETED

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self, include_context: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses

        Args:
            include_context: Whether to include the full execution_context snapshot
        """
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "user_id": self.user_id,
            "username": self.username,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "parent_execution_id": self.parent_execution_id,
            "execution_status": (
                self.execution_status.value
                if isinstance(self.execution_status, ExecutionStatus)
                else self.execution_status
            ),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "running_at": self.running_at.isoformat() if self.running_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "mcp_servers_used": self.mcp_servers_used,
            "llm_models_used": self.llm_models_used,
            "total_tokens": self.total_tokens,
            "total_mcp_calls": self.total_mcp_calls,
            "estimated_cost_usd": self.estimated_cost_usd,
            "approval_status": (
                self.approval_status.value
                if isinstance(self.approval_status, ApprovalStatus)
                else self.approval_status
            ),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "approval_notes": self.approval_notes,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

        if include_context:
            data["execution_context"] = self.execution_context
            data["error_details"] = self.error_details

        return data

    def to_summary_dict(self) -> Dict[str, Any]:
        """Compact summary for listing and dashboard views"""
        return {
            "id": self.id,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "group_name": self.group_name,
            "username": self.username,
            "execution_status": (
                self.execution_status.value
                if isinstance(self.execution_status, ExecutionStatus)
                else self.execution_status
            ),
            "approval_status": (
                self.approval_status.value
                if isinstance(self.approval_status, ApprovalStatus)
                else self.approval_status
            ),
            "duration_ms": self.duration_ms,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<AgentSkillExecution(id='{self.id}', skill='{self.skill_name}', "
            f"status='{self.execution_status}', approval='{self.approval_status}')>"
        )

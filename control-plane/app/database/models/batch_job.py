# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Batch Job Model

Tracks batch processing jobs submitted through the unified batch API.
The Rust proxy creates and updates these records; the Python gateway
provides admin read/monitoring endpoints.

Design decisions:
- SET NULL on optional FKs (resource_group, user, llm_model) so batch
  history survives if the referenced entity is later deleted.
- CASCADE on tenant_id — batch jobs belong to a tenant.
- Denormalized names (resource_group_name, username, llm_model_name)
  are snapshotted at creation time for historical accuracy.
- Provider-specific fields (provider_batch_id, provider_input_file_id,
  etc.) are generic strings that map to whatever each provider uses.
  Provider-specific data beyond these goes into custom_metadata JSON.

Lifecycle:
  CREATED → VALIDATING → IN_PROGRESS → COMPLETED | PARTIALLY_COMPLETED | FAILED
                                      → CANCELLING → CANCELLED
                                      → EXPIRED
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text,
    Index, ForeignKey, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.constants.batch import BatchJobStatus


class BatchJob(Base):
    """
    Batch processing job record.

    One row per batch submission. Contains aggregate counts, cost tracking,
    and provider correlation IDs. Individual requests are in BatchRequest.
    """
    __tablename__ = "batch_jobs"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: btj-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this batch job belongs to"
    )

    # =========================================================================
    # RESOURCE GROUP (for cost attribution)
    # =========================================================================

    resource_group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="SET NULL"),
        nullable=True,
        comment="Resource group for usage/cost attribution (NULL if group was deleted)"
    )

    resource_group_name = Column(
        String(255),
        nullable=True,
        comment="Group name at creation time (denormalized for history)"
    )

    # =========================================================================
    # USER CONTEXT
    # =========================================================================

    user_id = Column(
        Integer,
        ForeignKey("end_users.id", ondelete="SET NULL"),
        nullable=True,
        comment="End user who submitted the batch (NULL if user was deleted)"
    )

    username = Column(
        String(255),
        nullable=True,
        comment="Username at submission time (denormalized for history)"
    )

    api_key_id = Column(
        String(255),
        nullable=True,
        comment="API key used to submit the batch"
    )

    # The agent whose own workload token submitted the batch: such a job has neither an API key nor an end user. No foreign
    # key, like `api_key_id`: the job's history outlives the agent.
    agent_id = Column(
        String(255),
        nullable=True,
        comment="Agent identity whose workload token submitted the batch"
    )

    # =========================================================================
    # LLM MODEL REFERENCE
    # =========================================================================

    llm_model_id = Column(
        String(255),
        ForeignKey("llm_model.id", ondelete="SET NULL"),
        nullable=True,
        comment="Gateway LLM model reference (NULL if model was deleted)"
    )

    llm_model_name = Column(
        String(255),
        nullable=True,
        comment="Model name at submission time (denormalized for history)"
    )

    provider = Column(
        String(100),
        nullable=False,
        comment="LLM provider: anthropic, openai, google, xai, etc."
    )

    model = Column(
        String(255),
        nullable=False,
        comment="Model identifier sent to the provider (e.g. gpt-4o, claude-sonnet-4-20250514)"
    )

    # =========================================================================
    # BATCH ENDPOINT
    # =========================================================================

    endpoint = Column(
        String(255),
        nullable=False,
        default="/v1/chat/completions",
        comment="Target endpoint for batch requests (e.g. /v1/chat/completions, /v1/messages)"
    )

    completion_window = Column(
        String(50),
        nullable=False,
        default="24h",
        comment="Requested completion window (e.g. 24h)"
    )

    # =========================================================================
    # PROVIDER CORRELATION
    # =========================================================================

    provider_batch_id = Column(
        String(500),
        nullable=True,
        comment="Provider's batch ID for polling (e.g. Anthropic message_batch_id, OpenAI batch_id)"
    )

    provider_input_file_id = Column(
        String(500),
        nullable=True,
        comment="Provider's input file ID (OpenAI/Google — not used by Anthropic)"
    )

    provider_output_file_id = Column(
        String(500),
        nullable=True,
        comment="Provider's output/results file ID"
    )

    provider_error_file_id = Column(
        String(500),
        nullable=True,
        comment="Provider's error file ID (OpenAI-specific)"
    )

    # =========================================================================
    # BATCH METADATA
    # =========================================================================

    description = Column(
        Text,
        nullable=True,
        comment="User-provided description of this batch job"
    )

    custom_metadata = Column(
        JSON,
        nullable=True,
        comment="Arbitrary user-provided metadata (key-value pairs)"
    )

    # =========================================================================
    # STATUS & LIFECYCLE
    # =========================================================================

    status = Column(
        String(50),
        nullable=False,
        default=BatchJobStatus.CREATED.value,
        comment="Batch lifecycle: created → validating → in_progress → completed | failed | cancelled | expired"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the batch job record was created"
    )

    submitted_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the batch was submitted to the provider"
    )

    validating_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the provider began validating the batch"
    )

    in_progress_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the provider began processing requests"
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the batch finished (completed, failed, cancelled, or expired)"
    )

    expires_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Provider-imposed deadline for batch completion (e.g. 24h from submission)"
    )

    cancelled_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When a cancel was requested"
    )

    cancelled_by = Column(
        String(255),
        nullable=True,
        comment="Who requested the cancellation (admin username or 'api_key:<id>')"
    )

    # =========================================================================
    # RUN — written by the Rust core at submit
    # =========================================================================
    #
    # The run the submitting request was a step of. The core's background
    # result fetcher writes an output-guardrail block row for an item long
    # after the request is gone; these place that row on the run (and under
    # the submit step) it belongs to. NULL when the core recorded no run.

    root_task_id = Column(
        String(255),
        nullable=True,
        comment="Run (root_task_id) the submitting request belonged to",
    )

    submit_task_id = Column(
        String(255),
        nullable=True,
        comment="Step (current_task_id) of the submitting request",
    )

    delegation_depth = Column(
        Integer,
        nullable=True,
        comment="Delegation depth of the submitting request in its run",
    )

    # =========================================================================
    # REQUEST COUNTS
    # =========================================================================

    total_requests = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total number of requests in this batch"
    )

    completed_requests = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of requests that succeeded"
    )

    failed_requests = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Number of requests that failed"
    )

    # =========================================================================
    # TOKEN & COST TRACKING
    # =========================================================================

    total_input_tokens = Column(
        Integer,
        nullable=True,
        comment="Total input/prompt tokens across all requests"
    )

    total_output_tokens = Column(
        Integer,
        nullable=True,
        comment="Total output/completion tokens across all requests"
    )

    estimated_cost_usd = Column(
        Float,
        nullable=True,
        comment="Estimated total cost in USD (at batch pricing, typically 50% off)"
    )

    # =========================================================================
    # ERROR HANDLING
    # =========================================================================

    error_type = Column(
        String(100),
        nullable=True,
        comment="Error classification (e.g. 'validation_error', 'provider_error', 'timeout')"
    )

    error_message = Column(
        Text,
        nullable=True,
        comment="Human-readable error description"
    )

    error_details = Column(
        JSON,
        nullable=True,
        comment="Structured error details from the provider"
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    requests = relationship(
        "BatchRequest",
        back_populates="batch_job",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    resource_group = relationship("ResourceGroup")
    user = relationship("EndUser")
    llm_model = relationship("LLModel")

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # Admin dashboard: "Get all batch jobs for a tenant by status"
        Index('ix_batch_job_tenant_status',
              'tenant_id', 'status', 'created_at'),

        # Chronological listing
        Index('ix_batch_job_tenant_created',
              'tenant_id', 'created_at'),

        # Provider analysis: "How many batches per provider?"
        Index('ix_batch_job_tenant_provider',
              'tenant_id', 'provider', 'created_at'),

        # Cost attribution: "What's the batch spend for this group?"
        Index('ix_batch_job_tenant_group',
              'tenant_id', 'resource_group_id', 'created_at'),

        # User batch listing: "Show me my batches"
        Index('ix_batch_job_tenant_user',
              'tenant_id', 'user_id', 'created_at'),

        # Agent batch listing: "Which batches did this agent submit?"
        Index('ix_batch_job_tenant_agent',
              'tenant_id', 'agent_id', 'created_at'),

        # Model usage: "Which models are used in batches?"
        Index('ix_batch_job_tenant_model',
              'tenant_id', 'llm_model_id', 'created_at'),

        # Provider correlation: "Find our record for this provider batch ID"
        Index('ix_batch_job_provider_batch_id', 'provider_batch_id'),

        {
            'comment': 'Batch processing jobs with provider-agnostic lifecycle tracking'
        }
    )

    # =========================================================================
    # LIFECYCLE METHODS
    # =========================================================================

    def mark_submitted(self) -> None:
        """Record when the batch was submitted to the provider"""
        self.submitted_at = datetime.now(timezone.utc)

    def mark_validating(self, provider_batch_id: str) -> None:
        """Provider is validating the batch"""
        self.status = BatchJobStatus.VALIDATING.value
        self.validating_at = datetime.now(timezone.utc)
        self.provider_batch_id = provider_batch_id

    def mark_in_progress(self) -> None:
        """Provider has started processing requests"""
        self.status = BatchJobStatus.IN_PROGRESS.value
        self.in_progress_at = datetime.now(timezone.utc)

    def mark_completed(
        self,
        completed_requests: int = 0,
        failed_requests: int = 0,
        total_input_tokens: Optional[int] = None,
        total_output_tokens: Optional[int] = None,
        estimated_cost_usd: Optional[float] = None,
    ) -> None:
        """Mark batch as completed (all requests processed)"""
        now = datetime.now(timezone.utc)
        self.completed_at = now
        self.completed_requests = completed_requests
        self.failed_requests = failed_requests
        self.total_input_tokens = total_input_tokens
        self.total_output_tokens = total_output_tokens
        self.estimated_cost_usd = estimated_cost_usd

        if failed_requests > 0 and completed_requests > 0:
            self.status = BatchJobStatus.PARTIALLY_COMPLETED.value
        elif failed_requests > 0 and completed_requests == 0:
            self.status = BatchJobStatus.FAILED.value
        else:
            self.status = BatchJobStatus.COMPLETED.value

    def mark_failed(
        self,
        error_type: str,
        error_message: str,
        error_details: Optional[Dict] = None,
    ) -> None:
        """Mark batch as failed"""
        self.status = BatchJobStatus.FAILED.value
        self.completed_at = datetime.now(timezone.utc)
        self.error_type = error_type
        self.error_message = error_message
        self.error_details = error_details

    def mark_cancelling(self, cancelled_by: str) -> None:
        """Cancel requested, waiting for provider confirmation"""
        self.status = BatchJobStatus.CANCELLING.value
        self.cancelled_at = datetime.now(timezone.utc)
        self.cancelled_by = cancelled_by

    def mark_cancelled(self) -> None:
        """Provider confirmed cancellation"""
        self.status = BatchJobStatus.CANCELLED.value
        self.completed_at = datetime.now(timezone.utc)

    def mark_expired(self) -> None:
        """Provider expired the batch (completion window exceeded)"""
        self.status = BatchJobStatus.EXPIRED.value
        self.completed_at = datetime.now(timezone.utc)

    # =========================================================================
    # QUERY HELPERS
    # =========================================================================

    @property
    def is_terminal(self) -> bool:
        """Check if batch is in a terminal state"""
        return self.status in (
            BatchJobStatus.COMPLETED,
            BatchJobStatus.PARTIALLY_COMPLETED,
            BatchJobStatus.FAILED,
            BatchJobStatus.CANCELLED,
            BatchJobStatus.EXPIRED,
        )

    @property
    def is_active(self) -> bool:
        """Check if batch is still being processed"""
        return self.status in (
            BatchJobStatus.CREATED,
            BatchJobStatus.VALIDATING,
            BatchJobStatus.IN_PROGRESS,
            BatchJobStatus.CANCELLING,
        )

    @property
    def progress_percent(self) -> Optional[float]:
        """Calculate completion percentage"""
        if self.total_requests == 0:
            return None
        return round(
            (self.completed_requests + self.failed_requests) / self.total_requests * 100, 1
        )

    @property
    def duration_seconds(self) -> Optional[float]:
        """Calculate batch duration from submission to completion"""
        start = self.submitted_at or self.created_at
        end = self.completed_at
        if not start or not end:
            return None
        return (end - start).total_seconds()

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self, include_details: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses.

        Args:
            include_details: Whether to include provider IDs, error details, and metadata
        """
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "resource_group_id": self.resource_group_id,
            "resource_group_name": self.resource_group_name,
            "user_id": self.user_id,
            "username": self.username,
            "agent_id": self.agent_id,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "completion_window": self.completion_window,
            "status": (
                self.status.value
                if isinstance(self.status, BatchJobStatus)
                else self.status
            ),
            "description": self.description,
            "total_requests": self.total_requests,
            "completed_requests": self.completed_requests,
            "failed_requests": self.failed_requests,
            "progress_percent": self.progress_percent,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
        }

        if include_details:
            data.update({
                "llm_model_id": self.llm_model_id,
                "llm_model_name": self.llm_model_name,
                "api_key_id": self.api_key_id,
                "provider_batch_id": self.provider_batch_id,
                "provider_input_file_id": self.provider_input_file_id,
                "provider_output_file_id": self.provider_output_file_id,
                "provider_error_file_id": self.provider_error_file_id,
                "custom_metadata": self.custom_metadata,
                "validating_at": self.validating_at.isoformat() if self.validating_at else None,
                "in_progress_at": self.in_progress_at.isoformat() if self.in_progress_at else None,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
                "cancelled_by": self.cancelled_by,
                "error_details": self.error_details,
            })

        return data

    def to_summary_dict(self) -> Dict[str, Any]:
        """Compact summary for listing and dashboard views"""
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "status": (
                self.status.value
                if isinstance(self.status, BatchJobStatus)
                else self.status
            ),
            "description": self.description,
            "total_requests": self.total_requests,
            "completed_requests": self.completed_requests,
            "failed_requests": self.failed_requests,
            "progress_percent": self.progress_percent,
            "estimated_cost_usd": self.estimated_cost_usd,
            "username": self.username,
            "agent_id": self.agent_id,
            "resource_group_name": self.resource_group_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<BatchJob(id='{self.id}', provider='{self.provider}', "
            f"model='{self.model}', status='{self.status}', "
            f"requests={self.total_requests})>"
        )

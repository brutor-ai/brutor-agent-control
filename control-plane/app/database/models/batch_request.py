# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Batch Request Model

Individual request within a batch job. Each row represents one
LLM request that was part of a batch submission.

The Rust proxy creates these records when results are retrieved
from the provider. The Python gateway provides admin query endpoints.

Design decisions:
- CASCADE on batch_job_id — requests are deleted with their parent job.
- request_body and response_body are optional JSON columns that can be
  large. The proxy has a configuration option for whether to persist these.
- UniqueConstraint on (batch_job_id, custom_id) ensures no duplicate
  custom_ids within a single batch.
"""

from datetime import datetime, timezone
from typing import Dict, Any, Optional

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text,
    Index, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.constants.batch import BatchRequestStatus


class BatchRequest(Base):
    """
    Individual request within a batch job.

    One row per request in the batch. Tracks status, tokens, cost,
    and optionally stores the full request/response bodies.
    """
    __tablename__ = "batch_requests"

    # =========================================================================
    # PRIMARY KEY & REFERENCES
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: btr-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant (denormalized from batch_job for query efficiency)"
    )

    batch_job_id = Column(
        String(255),
        ForeignKey("batch_jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent batch job"
    )

    # =========================================================================
    # REQUEST IDENTITY
    # =========================================================================

    custom_id = Column(
        String(500),
        nullable=False,
        comment="User-provided ID for correlating results back to input (required by all providers)"
    )

    request_index = Column(
        Integer,
        nullable=True,
        comment="Zero-based position in the batch (order of submission)"
    )

    # =========================================================================
    # STATUS
    # =========================================================================

    status = Column(
        String(50),
        nullable=False,
        default=BatchRequestStatus.PENDING.value,
        comment="Request lifecycle: pending → succeeded | failed | cancelled | expired"
    )

    # =========================================================================
    # REQUEST CONTENT (optional — can be large)
    # =========================================================================

    request_body = Column(
        JSON,
        nullable=True,
        comment="Original request payload (optional, for audit — can be large)"
    )

    model_requested = Column(
        String(255),
        nullable=True,
        comment="Model specified in this individual request (may differ from batch default)"
    )

    # =========================================================================
    # RESPONSE CONTENT
    # =========================================================================

    response_status_code = Column(
        Integer,
        nullable=True,
        comment="HTTP status code from the provider for this request"
    )

    response_body = Column(
        JSON,
        nullable=True,
        comment="Provider response payload (optional, for retrieval — can be large)"
    )

    # =========================================================================
    # TOKEN & COST TRACKING
    # =========================================================================

    input_tokens = Column(
        Integer,
        nullable=True,
        comment="Input/prompt tokens for this request"
    )

    output_tokens = Column(
        Integer,
        nullable=True,
        comment="Output/completion tokens for this request"
    )

    estimated_cost_usd = Column(
        Float,
        nullable=True,
        comment="Estimated cost for this request in USD (at batch pricing)"
    )

    # =========================================================================
    # ERROR HANDLING
    # =========================================================================

    error_type = Column(
        String(100),
        nullable=True,
        comment="Error classification (e.g. 'invalid_request', 'rate_limit', 'model_error')"
    )

    error_message = Column(
        Text,
        nullable=True,
        comment="Human-readable error description"
    )

    # =========================================================================
    # TIMESTAMPS
    # =========================================================================

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this request record was created"
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the provider returned a result for this request"
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    batch_job = relationship(
        "BatchJob",
        back_populates="requests"
    )

    # =========================================================================
    # INDEXES & CONSTRAINTS
    # =========================================================================

    __table_args__ = (
        # Query by status within a batch: "Get all failed requests"
        Index('ix_batch_req_job_status',
              'batch_job_id', 'status'),

        # Tenant-scoped listing
        Index('ix_batch_req_tenant_job',
              'tenant_id', 'batch_job_id', 'created_at'),

        # Custom ID lookup: "Find result for my request"
        Index('ix_batch_req_custom_id',
              'tenant_id', 'batch_job_id', 'custom_id'),

        # Ensure unique custom_ids within a batch
        UniqueConstraint('batch_job_id', 'custom_id',
                         name='uq_batch_req_job_custom_id'),

        {
            'comment': 'Individual requests within batch processing jobs'
        }
    )

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self, include_bodies: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses.

        Args:
            include_bodies: Whether to include request_body and response_body
                           (can be large — omitted by default in list views)
        """
        data = {
            "id": self.id,
            "batch_job_id": self.batch_job_id,
            "custom_id": self.custom_id,
            "request_index": self.request_index,
            "status": (
                self.status.value
                if isinstance(self.status, BatchRequestStatus)
                else self.status
            ),
            "model_requested": self.model_requested,
            "response_status_code": self.response_status_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

        if include_bodies:
            data["request_body"] = self.request_body
            data["response_body"] = self.response_body

        return data

    def to_summary_dict(self) -> Dict[str, Any]:
        """Compact summary for listing views"""
        return {
            "id": self.id,
            "custom_id": self.custom_id,
            "status": (
                self.status.value
                if isinstance(self.status, BatchRequestStatus)
                else self.status
            ),
            "response_status_code": self.response_status_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error_message": self.error_message,
        }

    def __repr__(self) -> str:
        return (
            f"<BatchRequest(id='{self.id}', custom_id='{self.custom_id}', "
            f"status='{self.status}')>"
        )

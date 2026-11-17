# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Video generation job.

Async pipeline state for the queue-based video providers
(fal.ai Luma Dream Machine, Kling, Runway, Replicate). Owned by
the Rust gateway-core proxy at runtime; this ORM model exists so
admin endpoints can list/inspect jobs and so the seed-init
pipeline imports the table.

Lifecycle:

    pending     → row written by the submit endpoint
    submitted   → upstream queue accepted; provider_request_id set
    succeeded   → output_file_id populated (Files API row exists)
    failed      → error string populated
    cancelled   → operator / user cancelled

The poller drives the row through these states and is in the Rust
proxy (not in this Python control plane).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from app.database import Base


class VideoGenerationJob(Base):
    """Async video-generation job tracking row."""

    __tablename__ = "video_generation_jobs"

    id = Column(
        String(64),
        primary_key=True,
        comment=(
            "Stable, ULID-based job id surfaced as "
            "brutor_video_job_id on the wire."
        ),
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    model_id = Column(
        String(100),
        ForeignKey("llm_model.id", ondelete="SET NULL"),
        nullable=True,
    )

    actor_id = Column(
        String(255),
        nullable=True,
        comment="User who submitted the job.",
    )

    prompt = Column(Text, nullable=False)

    resolution = Column(
        String(40),
        nullable=True,
        comment="Resolution requested (e.g. '720p').",
    )

    duration_seconds = Column(
        Integer,
        nullable=True,
        comment="Clip duration requested.",
    )

    chat_id = Column(
        String(64),
        nullable=True,
        comment="Optional chat tag for 'delete this chat' fanout.",
    )

    status = Column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        comment=(
            "Lifecycle: pending | submitted | succeeded | "
            "failed | cancelled."
        ),
    )

    provider_request_id = Column(
        String(255),
        nullable=True,
        comment=(
            "Upstream queue id (Fal request_id, Replicate "
            "prediction id, etc.). NULL until submitted."
        ),
    )

    output_file_id = Column(
        String(64),
        ForeignKey("media_files.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "media_files row holding the generated clip. "
            "Set by the poller on success."
        ),
    )

    error = Column(
        Text,
        nullable=True,
        comment="Error string when status='failed'.",
    )

    cost_usd = Column(
        Float,
        nullable=True,
        comment="Computed when the duration is known.",
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Set every time the poller updates the row.",
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_video_jobs_tenant_status",
            "tenant_id",
            "status",
        ),
        Index(
            "ix_video_jobs_chat",
            "tenant_id",
            "chat_id",
        ),
        # Poller pulls work-to-do.
        Index(
            "ix_video_jobs_poller",
            "status",
            "updated_at",
        ),
        {
            "comment": (
                "Async video-generation pipeline state. Submit "
                "writes pending; poller drives to terminal."
            )
        },
    )

    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "model_id": self.model_id,
            "actor_id": self.actor_id,
            "prompt": self.prompt,
            "resolution": self.resolution,
            "duration_seconds": self.duration_seconds,
            "chat_id": self.chat_id,
            "status": self.status,
            "provider_request_id": self.provider_request_id,
            "output_file_id": self.output_file_id,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }

    def __repr__(self) -> str:
        return (
            f"<VideoGenerationJob(id={self.id}, tenant={self.tenant_id}, "
            f"status={self.status})>"
        )

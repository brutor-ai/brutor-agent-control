# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
System Alert Model
Persistent alerts for downstream service failures, resource-health
transitions, infrastructure thresholds, and internal errors.

Distinct from `usage_alerts` (quota-scoped, indexed by
metric/threshold/period). Here dedup is keyed on
`tenant|category|source|error_code` and re-raises bump
`occurrence_count` instead of inserting new rows — matching how
industry alerting systems group alert instances.

Lifecycle:
  active        → acknowledged (admin clicks "investigating")
  active        → resolved     (auto-resolver or admin)
  acknowledged  → resolved     (admin)
"""

from typing import Any, Dict, Optional

from sqlalchemy import (
    text,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base


# ---------------------------------------------------------------------------
# Allowed values — kept in sync with migration a002_system_alerts.py and the
# Rust-side enums in src/services/system_alerts.rs.
# ---------------------------------------------------------------------------

SEVERITIES = ("critical", "high", "medium", "low")

CATEGORIES = (
    "downstream_provider",     # OpenAI/Anthropic/etc returned 5xx or timed out
    "downstream_idp",          # OAuth IdP unavailable / token refresh failed
    "downstream_cache",        # Redis / semantic cache service outage
    "downstream_vector_db",    # Qdrant unavailable
    "resource_health",         # LLM model or MCP server health flipped
    "infrastructure_threshold",# CPU/memory/error-rate threshold crossed
    "internal_error",          # A bug in our own code path — distinct from a
                               # downstream failure. Always logged at ERROR.
    # The first category that is about a *governed
    # workload* rather than the plumbing under it. Every category above
    # asks "is the infrastructure up"; this one asks "is the AI System
    # still doing anything at all", which nothing else in the platform
    # could ask, because every other signal is a function of traffic that
    # arrived.
    "ai_system_liveness",      # An AI System went silent, stalled, collapsed
                               # in volume, or started failing outright.
    # The rest of the workload-facing taxonomy. These
    # complete what `ai_system_liveness` opened: alerts about a governed
    # system's *behaviour* rather than the infrastructure under it.
    "ai_system_behaviour",     # Trajectory / outcome mix moved off baseline.
    "ai_system_cost",          # The same work started costing more.
    "ai_system_dependency",    # A tool or model underneath the system changed.
)

STATUSES = ("active", "acknowledged", "resolved")


class SystemAlert(Base):
    """System alert — one row per distinct failure instance (deduped by dedup_key)."""

    __tablename__ = "system_alerts"

    # ==================== Identity ====================
    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'sysalert-'")

    # NULL = platform-wide alert (not tenant-scoped). Used for e.g. Redis
    # outage that affects every tenant at once.
    tenant_id = Column(String(255), nullable=True, index=True)

    # ==================== Classification ====================
    severity = Column(String(16), nullable=False)
    category = Column(String(32), nullable=False)
    source = Column(
        String(255),
        nullable=False,
        comment="Machine-readable source, e.g. 'llm.openai', 'mcp.server.<id>'",
    )

    # ==================== Affected resource (denormalized for UI) ====================
    resource_type = Column(String(32), nullable=True)
    resource_id = Column(String(255), nullable=True)
    resource_name = Column(String(255), nullable=True)

    # ==================== Error detail ====================
    error_code = Column(String(64), nullable=True)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    context = Column(JSONB, nullable=True)

    # ==================== Lifecycle ====================
    status = Column(String(20), nullable=False, default="active", server_default="active")

    first_seen_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_seen_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    occurrence_count = Column(Integer, nullable=False, default=1, server_default="1")

    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_by = Column(String(255), nullable=True)

    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(String(255), nullable=True)
    resolution_note = Column(Text, nullable=True)

    # ==================== Dedup ====================
    dedup_key = Column(String(512), nullable=False)

    # ==================== Constraints & indexes ====================
    __table_args__ = (
        CheckConstraint(
            "severity IN ('critical','high','medium','low')",
            name="ck_system_alerts_severity",
        ),
        # Derived from CATEGORIES rather than restated. A hard-coded list
        # here silently diverges from the migrations: fresh installs build
        # this table with `create_all` (never the migration chain), so a
        # stale literal means a category the migrations added is rejected
        # on exactly the deployments that skipped them — and it fails in a
        # background alert-raiser, where nobody is watching.
        CheckConstraint(
            "category IN (" + ",".join(f"'{c}'" for c in CATEGORIES) + ")",
            name="ck_system_alerts_category",
        ),
        CheckConstraint(
            "status IN ('active','acknowledged','resolved')",
            name="ck_system_alerts_status",
        ),
        # Partial unique index: at most one row per dedup_key while
        # status IN ('active','acknowledged'). Resolved rows are
        # retained for history; a new occurrence after resolve can
        # create a new active row.
        Index(
            "uq_system_alerts_active_dedup",
            "dedup_key",
            unique=True,
            postgresql_where="status IN ('active','acknowledged')",
        ),
        Index("ix_system_alerts_tenant_status", "tenant_id", "status"),
        Index("ix_system_alerts_category_status", "category", "status"),
        Index("ix_system_alerts_status_last_seen", "status", "last_seen_at"),
        {
            "comment": (
                "Persistent alerts for downstream failures, resource "
                "health transitions, and internal errors"
            )
        },
    )

    # ==================== Helpers ====================
    def __repr__(self) -> str:
        return (
            f"<SystemAlert(id={self.id!r}, tenant={self.tenant_id!r}, "
            f"category={self.category!r}, source={self.source!r}, "
            f"severity={self.severity!r}, status={self.status!r}, "
            f"occurrences={self.occurrence_count})>"
        )

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_downstream(self) -> bool:
        return self.category.startswith("downstream_")

    @property
    def is_internal(self) -> bool:
        return self.category == "internal_error"

    @staticmethod
    def compute_dedup_key(
        tenant_id: Optional[str],
        category: str,
        source: str,
        error_code: Optional[str],
    ) -> str:
        """Deterministic dedup key. Must match the Rust-side implementation."""
        return "|".join(
            [
                tenant_id or "global",
                category,
                source,
                error_code or "none",
            ]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "severity": self.severity,
            "category": self.category,
            "source": self.source,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "error_code": self.error_code,
            "title": self.title,
            "message": self.message,
            "context": self.context,
            "status": self.status,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "occurrence_count": self.occurrence_count,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolution_note": self.resolution_note,
            "dedup_key": self.dedup_key,
        }

    def to_summary(self) -> Dict[str, Any]:
        """Compact payload for dashboard cards."""
        return {
            "id": self.id,
            "severity": self.severity,
            "category": self.category,
            "source": self.source,
            "resource_name": self.resource_name,
            "title": self.title,
            "status": self.status,
            "occurrence_count": self.occurrence_count,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }

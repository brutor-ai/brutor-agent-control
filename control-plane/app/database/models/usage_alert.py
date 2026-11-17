# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Usage Alert Model
Tracks threshold crossings and hard stops for usage limits.

Alerts are deduplicated per (tenant, resource_type, group, metric, threshold, period)
so the same warning isn't fired repeatedly within a billing period.

Lifecycle:
  active → acknowledged (admin clicks "got it" in Admin UI)
  active → resolved     (period rolls over, or usage drops below threshold)
"""

from sqlalchemy import Column, String, Integer, BigInteger, Float, DateTime, Index, UniqueConstraint, Text
from sqlalchemy.sql import func
from typing import Dict, Any

from app.database import Base


class UsageAlert(Base):
    """
    Usage Alert — records when a usage threshold is crossed.

    Written asynchronously after quota checks detect a threshold crossing.
    The unique constraint prevents duplicate alerts for the same threshold
    in the same billing period.
    """
    __tablename__ = "usage_alerts"

    # ==================== Primary Key ====================
    id = Column(String(64), primary_key=True, comment="ULID or UUID")

    # ==================== Multi-tenancy ====================
    tenant_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Tenant this alert belongs to"
    )

    # ==================== What was hit ====================
    resource_type = Column(
        String(20),
        nullable=False,
        comment="Resource type: 'llm' or 'mcp'"
    )

    group_id = Column(
        String(255),
        nullable=False,
        comment="Resource Group where the limit was hit"
    )

    group_name = Column(
        String(255),
        nullable=True,
        comment="Group name at time of alert (snapshot)"
    )

    # ==================== Alert Details ====================
    alert_type = Column(
        String(20),
        nullable=False,
        comment="Alert severity: 'warning', 'hard_stop', 'resumed'"
    )

    metric = Column(
        String(50),
        nullable=False,
        comment="Which metric triggered: 'daily_tokens', 'monthly_budget', 'daily_requests', 'daily_mcp_calls', etc."
    )

    threshold_pct = Column(
        Integer,
        nullable=False,
        comment="Threshold percentage that was crossed (e.g., 80, 90, 100)"
    )

    current_value = Column(
        BigInteger,
        nullable=False,
        comment="Usage value when alert fired (tokens, request count, etc.)"
    )

    current_value_float = Column(
        Float,
        nullable=True,
        comment="Usage value for cost metrics (USD). Use instead of current_value for budget alerts."
    )

    limit_value = Column(
        BigInteger,
        nullable=False,
        comment="The configured limit value"
    )

    limit_value_float = Column(
        Float,
        nullable=True,
        comment="The configured limit for cost metrics (USD)"
    )

    utilization_pct = Column(
        Float,
        nullable=False,
        comment="Actual utilization percentage when alert fired"
    )

    # ==================== Period ====================
    period = Column(
        String(20),
        nullable=False,
        comment="Limit period: 'daily' or 'monthly'"
    )

    period_start = Column(
        DateTime(timezone=True),
        nullable=False,
        comment="Start of the billing period (midnight UTC for daily, 1st of month for monthly)"
    )

    # ==================== Notification State ====================
    status = Column(
        String(20),
        nullable=False,
        default="active",
        comment="Alert lifecycle: 'active', 'acknowledged', 'resolved'"
    )

    notified_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When external notification was sent (email, webhook, etc.)"
    )

    acknowledged_by = Column(
        String(255),
        nullable=True,
        comment="Admin user ID who acknowledged the alert"
    )

    acknowledged_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the alert was acknowledged"
    )

    resolved_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the alert was auto-resolved (period rollover or usage dropped)"
    )

    resolution_note = Column(
        Text,
        nullable=True,
        comment="How the alert was resolved: 'period_expired', 'manual', 'limit_increased'"
    )

    # ==================== Context ====================
    triggered_by_request_id = Column(
        String(255),
        nullable=True,
        comment="The request_id that triggered this alert"
    )

    message = Column(
        Text,
        nullable=True,
        comment="Human-readable alert message"
    )

    # ==================== Timestamps ====================
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    # ==================== Constraints & Indexes ====================
    __table_args__ = (
        # DEDUPLICATION: One alert per threshold per metric per period
        # This is the KEY constraint — prevents firing "80% daily tokens"
        # hundreds of times in the same day
        UniqueConstraint(
            'tenant_id', 'resource_type', 'group_id',
            'metric', 'threshold_pct', 'period_start',
            name='uq_usage_alert_per_period'
        ),

        # Dashboard: "Show all active alerts for this tenant"
        Index('ix_usage_alerts_active',
              'tenant_id', 'status'),

        # Group view: "Show alerts for this group"
        Index('ix_usage_alerts_group',
              'tenant_id', 'group_id', 'created_at'),

        # Cleanup: "Resolve expired daily alerts"
        Index('ix_usage_alerts_period',
              'status', 'period', 'period_start'),

        # Alert type filter: "Show all hard stops"
        Index('ix_usage_alerts_type',
              'tenant_id', 'alert_type', 'created_at'),

        {
            'comment': 'Usage threshold crossing alerts with deduplication per billing period'
        }
    )

    def __repr__(self) -> str:
        return (
            f"<UsageAlert(group='{self.group_id}', metric='{self.metric}', "
            f"threshold={self.threshold_pct}%, type='{self.alert_type}', "
            f"status='{self.status}')>"
        )

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_hard_stop(self) -> bool:
        return self.alert_type == "hard_stop"

    @property
    def is_warning(self) -> bool:
        return self.alert_type == "warning"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "resource_type": self.resource_type,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "alert_type": self.alert_type,
            "metric": self.metric,
            "threshold_pct": self.threshold_pct,
            "current_value": self.current_value,
            "current_value_float": self.current_value_float,
            "limit_value": self.limit_value,
            "limit_value_float": self.limit_value_float,
            "utilization_pct": self.utilization_pct,
            "period": self.period,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "status": self.status,
            "message": self.message,
            "notified_at": self.notified_at.isoformat() if self.notified_at else None,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_note": self.resolution_note,
            "triggered_by_request_id": self.triggered_by_request_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }

    def to_summary(self) -> Dict[str, Any]:
        """Compact summary for dashboard cards"""
        return {
            "id": self.id,
            "alert_type": self.alert_type,
            "metric": self.metric,
            "group_name": self.group_name or self.group_id,
            "resource_type": self.resource_type,
            "utilization_pct": self.utilization_pct,
            "threshold_pct": self.threshold_pct,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
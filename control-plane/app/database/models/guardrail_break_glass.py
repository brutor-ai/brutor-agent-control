# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guardrail break-glass.

The operator escape hatch in the guardrail degradation ladder. When a detector
provider is genuinely down and availability outranks the control, someone
declares it and guardrails degrade to log-and-allow for the duration.

This is the same outcome a hardcoded fail-open would produce. The difference —
and the entire reason it exists as a record rather than a code default — is
that a name is attached, a reason is required, and **it expires on its own**.

Every competing gateway fails open because a fail-closed error is loud and
attributable while a fail-open one is invisible, so defaults drift toward the
option with no observable cost. A break-glass row restores the cost: it is
visible, owned, time-boxed, and it shows up in the assurance report as a window
where checks did not run.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.sql import func

from app.database import Base

#: Longest a single break-glass may run before it must be renewed deliberately.
#: Eight hours is one on-call shift: long enough to cover an incident, short
#: enough that it cannot quietly outlive the person who declared it.
MAX_BREAK_GLASS_HOURS = 8

#: Default when the caller does not specify — deliberately much shorter than
#: the maximum, so the common case is a small, renewable window.
DEFAULT_BREAK_GLASS_MINUTES = 60


class GuardrailBreakGlass(Base):
    """One declared guardrail outage window for a tenant."""

    __tablename__ = "guardrail_break_glass"

    id = Column(String(255), primary_key=True)  # gbg-<ULID>
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason = Column(
        Text,
        nullable=False,
        comment=(
            "Why the operator accepted log-and-allow. Required: an "
            "unexplained break-glass is indistinguishable from a "
            "misconfiguration when someone reviews it later."
        ),
    )
    activated_by = Column(String(255), nullable=False)
    activated_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        comment=(
            "Hard stop. NOT NULL deliberately — a break-glass that never "
            "expires is a permanent policy change nobody reviewed."
        ),
    )
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
    deactivated_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_guardrail_break_glass_tenant", "tenant_id"),
        # The runtime asks "is break-glass live for this tenant right now?" on
        # a hot path; the partial index matches exactly that question.
        Index(
            "ix_guardrail_break_glass_tenant_active",
            "tenant_id",
            "expires_at",
            postgresql_where=Column("deactivated_at").is_(None),
        ),
    )

    @staticmethod
    def _aware(dt: Optional[datetime]) -> Optional[datetime]:
        """Normalize for comparison: naive (SQLite) → UTC."""
        if dt is None:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """Live right now.

        Expiry is evaluated at read time rather than by a sweeper, so a
        forgotten background job can never leave a tenant unguarded. The row
        stays as history; it just stops being in force.
        """
        now = now or datetime.now(timezone.utc)
        if self.deactivated_at is not None:
            return False
        expires = self._aware(self.expires_at)
        return expires is not None and expires > now

    @property
    def remaining_seconds(self) -> int:
        expires = self._aware(self.expires_at)
        if expires is None:
            return 0
        return max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))

    @staticmethod
    def clamp_duration(minutes: Optional[int]) -> int:
        """Bound the requested window to `MAX_BREAK_GLASS_HOURS`.

        Clamped rather than rejected: an operator mid-incident typing 1440
        should get a valid 8-hour window and a note, not a validation error
        while the pager is going off.
        """
        requested = minutes or DEFAULT_BREAK_GLASS_MINUTES
        if requested < 1:
            requested = DEFAULT_BREAK_GLASS_MINUTES
        return min(requested, MAX_BREAK_GLASS_HOURS * 60)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "reason": self.reason,
            "activated_by": self.activated_by,
            "activated_at": (
                self.activated_at.isoformat() if self.activated_at else None
            ),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "deactivated_at": (
                self.deactivated_at.isoformat() if self.deactivated_at else None
            ),
            "deactivated_by": self.deactivated_by,
            "is_active": self.is_active(),
            "remaining_seconds": self.remaining_seconds,
        }

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Notification Model

Portal inbox notifications for users. Supports multiple notification types
including tool approval requests, approval resolutions, and system events.

Notifications are per-user and per-tenant. They can be marked as read
and auto-expire after a configurable period.
"""

import sqlalchemy as sa
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, Index, JSON
from sqlalchemy.sql import func
from typing import Dict, Any

from app.database import Base


class Notification(Base):
    """
    Notification — portal inbox entry for a user.

    Created by various subsystems (tool approvals, usage alerts, etc.)
    and consumed by the portal notification inbox.
    """
    __tablename__ = "notifications"

    # ==================== Primary Key ====================
    id = Column(String(64), primary_key=True, comment="ULID")

    # ==================== Multi-tenancy ====================
    tenant_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Tenant this notification belongs to"
    )

    # ==================== Target user ====================
    user_id = Column(
        Integer,
        nullable=False,
        comment="Target user (gateway end_users.id)"
    )

    # ==================== Notification content ====================
    notification_type = Column(
        String(50),
        nullable=False,
        comment="Type: tool_approval_request, tool_approval_resolved, usage_alert, system"
    )

    title = Column(
        String(500),
        nullable=False,
        comment="Short notification title"
    )

    body = Column(
        Text,
        nullable=True,
        comment="Longer notification body (markdown supported)"
    )

    notification_metadata = Column(
        'metadata',
        JSON,
        nullable=True,
        comment="Structured data: approval_request_id, tool_name, group_id, etc."
    )

    # ==================== Read state ====================
    is_read = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa.false(),
        comment="Whether the user has read this notification"
    )

    read_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the notification was read"
    )

    # ==================== Timestamps ====================
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    expires_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Auto-cleanup: notifications older than this can be pruned"
    )

    # ==================== Constraints & Indexes ====================
    __table_args__ = (
        # Inbox query: unread notifications for a user
        Index('ix_notif_user_unread',
              'tenant_id', 'user_id', 'is_read', 'created_at'),

        # Type-based listing
        Index('ix_notif_type',
              'tenant_id', 'notification_type', 'created_at'),

        # Expiry cleanup
        Index('ix_notif_expires',
              'expires_at'),

        {
            'comment': 'Portal inbox notifications for users'
        }
    )

    def __repr__(self) -> str:
        return (
            f"<Notification(id='{self.id}', type='{self.notification_type}', "
            f"user={self.user_id}, read={self.is_read})>"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "notification_type": self.notification_type,
            "title": self.title,
            "body": self.body,
            "metadata": self.notification_metadata,
            "is_read": self.is_read,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

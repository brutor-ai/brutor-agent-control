# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance notification channels.

Three tables, one contract:

* ``notification_channels`` — where alerts go. ``config`` is ENCRYPTED
  with the platform ENCRYPTION_KEY (a Slack incoming-webhook URL is a
  credential: anyone holding it can post as you). Channel health is
  tracked here (``consecutive_failures``): a dead alert channel is a
  silent-failure mode and gets surfaced as an assurance-inbox item.

* ``notification_subscriptions`` — which inbox events reach which
  channel, filtered by severity floor / categories / AI system, with a
  per-subscription coalescing window so a recurring finding is one
  message per window, not a storm.

* ``notification_deliveries`` — the audit trail AND the dispatcher's
  bookkeeping. One row per (subscription, inbox event) decision —
  ``sent`` / ``failed`` / ``coalesced`` / ``skipped`` — so the tailer
  never reconsiders an event and an auditor can answer "was anyone
  told, and when?".

The dispatcher TAILS ``assurance_inbox_events`` (which both the control
plane and the Rust core write): alerts are a projection of the inbox and
can never disagree with it. Delivery is fail-open by construction — it
runs in its own loop and can never block enforcement or item creation.
"""

from sqlalchemy import (
    text,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

CHANNEL_KIND_SLACK = "slack_webhook"
CHANNEL_KIND_WEBHOOK = "webhook"
CHANNEL_KINDS = (CHANNEL_KIND_SLACK, CHANNEL_KIND_WEBHOOK)

EVENT_CLASS_CREATED = "inbox_item_created"
EVENT_CLASS_REOCCURRED = "inbox_item_reoccurred"
# Reserved — kept in the vocabulary now so subscriptions
# created against it survive the upgrade.
EVENT_CLASS_DIGEST = "digest_weekly"
EVENT_CLASSES = (EVENT_CLASS_CREATED, EVENT_CLASS_REOCCURRED, EVENT_CLASS_DIGEST)

DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
DELIVERY_COALESCED = "coalesced"
DELIVERY_SKIPPED = "skipped"
# The row exists but the send has not been attempted yet. Written by a
# claim-before-deliver insert: `uq_ndel_subscription_event` makes the
# insert, not a preceding SELECT, the thing that decides who sends. A row
# left in this state means the process died mid-delivery — which is the
# honest record, and the reason this is a status rather than a nullable
# flag on 'sent'.
DELIVERY_PENDING = "pending"
DELIVERY_STATUSES = (
    DELIVERY_SENT,
    DELIVERY_FAILED,
    DELIVERY_COALESCED,
    DELIVERY_SKIPPED,
    DELIVERY_PENDING,
)

DEFAULT_COALESCE_SECONDS = 900
MAX_DELIVERY_ATTEMPTS = 5
# Consecutive failures before the channel itself becomes an inbox finding.
DEAD_CHANNEL_THRESHOLD = 5


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'nchan-'")
    tenant_id = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    kind = Column(String(32), nullable=False)

    # ENCRYPTED JSON (Fernet/envelope via EncryptionService), never plaintext:
    #   slack_webhook: {"webhook_url": "..."}
    #   webhook:       {"url": "...", "secret": "..."}  (secret signs HMAC)
    config_encrypted = Column(Text, nullable=False)

    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    created_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('slack_webhook','webhook')", name="ck_nchan_kind"
        ),
    )

    def to_dict(self) -> dict:
        """Config is NEVER serialized — masked summaries are produced at the
        endpoint from the decrypted config, not from the row."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "consecutive_failures": self.consecutive_failures,
            "last_success_at": _iso(self.last_success_at),
            "last_failure_at": _iso(self.last_failure_at),
            "last_error": self.last_error,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
        }


class NotificationSubscription(Base):
    __tablename__ = "notification_subscriptions"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'nsub-'")
    tenant_id = Column(String(255), nullable=False, index=True)
    channel_id = Column(
        String(64),
        ForeignKey("notification_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_class = Column(String(32), nullable=False)
    # Minimum severity that triggers a message: info | warning | critical.
    severity_floor = Column(String(16), nullable=False, default="warning", server_default="warning")
    # JSON list of inbox categories; empty/NULL = all categories.
    categories = Column(JSONB, nullable=True)
    # Restrict to one AI System; NULL = whole estate.
    ai_system_id = Column(String(255), nullable=True)
    # Per-fingerprint coalescing window (seconds).
    coalesce_seconds = Column(
        Integer, nullable=False, default=DEFAULT_COALESCE_SECONDS,
        server_default="900",
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    created_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "event_class IN ('inbox_item_created','inbox_item_reoccurred',"
            "'digest_weekly')",
            name="ck_nsub_event_class",
        ),
        CheckConstraint(
            "severity_floor IN ('info','warning','critical')",
            name="ck_nsub_severity_floor",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "channel_id": self.channel_id,
            "event_class": self.event_class,
            "severity_floor": self.severity_floor,
            "categories": self.categories,
            "ai_system_id": self.ai_system_id,
            "coalesce_seconds": self.coalesce_seconds,
            "enabled": self.enabled,
            "created_by": self.created_by,
            "created_at": _iso(self.created_at),
        }


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'ndel-'")
    tenant_id = Column(String(255), nullable=False, index=True)
    subscription_id = Column(
        String(64),
        ForeignKey("notification_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id = Column(String(64), nullable=False)
    # The inbox event that triggered this decision — the dedup key of the
    # tailer: one decision row per (subscription, event), whatever it was.
    inbox_event_id = Column(String(64), nullable=False)
    inbox_item_id = Column(String(64), nullable=False)
    fingerprint = Column(String(512), nullable=False)

    status = Column(String(16), nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('sent','failed','coalesced','skipped','pending')",
            name="ck_ndel_status",
        ),
        Index(
            "uq_ndel_subscription_event",
            "subscription_id",
            "inbox_event_id",
            unique=True,
        ),
        # Coalescing lookup: latest outcome per (subscription, fingerprint).
        Index("ix_ndel_coalesce", "subscription_id", "fingerprint", "created_at"),
        Index("ix_ndel_retry", "status", "next_attempt_at"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "subscription_id": self.subscription_id,
            "channel_id": self.channel_id,
            "inbox_event_id": self.inbox_event_id,
            "inbox_item_id": self.inbox_item_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "attempts": self.attempts,
            "next_attempt_at": _iso(self.next_attempt_at),
            "last_error": self.last_error,
            "sent_at": _iso(self.sent_at),
            "created_at": _iso(self.created_at),
        }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None

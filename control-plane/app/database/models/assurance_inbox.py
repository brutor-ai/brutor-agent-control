# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance Inbox — the single per-tenant triage queue of
things a human should look at, populated from signals the platform already
produces (guardrail blocks, drift, contract breaches, health transitions,
liveness misses, run failures, budget stops, approval timeouts).

Two tables, one contract:

* ``assurance_inbox_items`` — the CURRENT state of each finding. Deduped by
  ``(tenant_id, fingerprint)`` over non-resolved items (partial unique
  index): a recurring signal bumps ``occurrence_count`` on the open item
  rather than raising a second one. Resolving and then re-occurring starts
  a FRESH item (linked via the event trail), so "we keep dismissing this"
  stays visible.

* ``assurance_inbox_events`` — the append-only trail. Dispositions are
  EVIDENCE for the Assurance Report's "human review" section, so they are
  never edited in place: changing your mind is a `reopened` event plus a
  new resolution, and history keeps both.

Distinct from ``system_alerts`` (platform infrastructure health — "is the
platform sick?") — the Inbox answers "does this AI system's *behavior*
need review?". Items may reference a system_alert id in their evidence.

Review debt — an OPEN critical item older than the review SLA — feeds the
AI System health dimension ``review`` (worst-of compatible boolean-ish
signal; deliberately not a weighted score).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

# ── Vocabulary ──────────────────────────────────────────────────────────

INBOX_CATEGORIES = (
    "guardrail_block",
    "policy_denial",
    "contract_violation",
    "drift",
    "health_transition",
    "liveness",
    "run_failure",
    "budget_stop",
    "approval_timeout",
    "check_breach",
    # A notification channel that keeps failing is itself a
    # silent-failure finding.
    "notification_failure",
    # A check that cannot run (budget exhausted, no judge model)
    # is itself a finding: a silently-stopped check must never read as
    # "nothing flagged".
    "check_stalled",
    # A tool call named a record outside its run's data scope.
    "scope_violation",
    # The evidence programme's own findings: a head no
    # witness countersigned, governed rows with no sealed record, a judge
    # with no human calibration, a calibration queue nobody grades, and an
    # incident reporting deadline approaching or missed.
    "evidence_unwitnessed",
    "capture_gap",
    "judge_uncalibrated",
    "calibration_stalled",
    "incident_deadline",
    # Composition checks: declarations vs grants vs what
    # the ledger observed (undeclared dependency, unaccepted caller,
    # grant/edge mismatch, role mismatch, stale contract pin, …).
    "composition",
    # Agent implementation checks: an unapproved release, a
    # declared name that does not match, a run with no release when one is
    # required, and the same release behaving like different software.
    "agent",
)

INBOX_SEVERITIES = ("info", "warning", "critical")

INBOX_STATE_OPEN = "open"
INBOX_STATE_ACKNOWLEDGED = "acknowledged"
INBOX_STATE_RESOLVED = "resolved"
INBOX_STATES = (INBOX_STATE_OPEN, INBOX_STATE_ACKNOWLEDGED, INBOX_STATE_RESOLVED)

DISPOSITION_TRUE_POSITIVE = "true_positive"
DISPOSITION_FALSE_POSITIVE = "false_positive"
DISPOSITION_EXPECTED = "expected"
DISPOSITION_FIXED = "fixed"
INBOX_DISPOSITIONS = (
    DISPOSITION_TRUE_POSITIVE,
    DISPOSITION_FALSE_POSITIVE,
    DISPOSITION_EXPECTED,
    DISPOSITION_FIXED,
)

EVENT_CREATED = "created"
EVENT_REOCCURRED = "reoccurred"
EVENT_ACKNOWLEDGED = "acknowledged"
EVENT_ASSIGNED = "assigned"
EVENT_RESOLVED = "resolved"
EVENT_REOPENED = "reopened"
INBOX_EVENTS = (
    EVENT_CREATED,
    EVENT_REOCCURRED,
    EVENT_ACKNOWLEDGED,
    EVENT_ASSIGNED,
    EVENT_RESOLVED,
    EVENT_REOPENED,
)

# Default review SLA — an open critical item older than this marks the
# owning system's `review` health dimension degraded. Per-tenant override
# lives in tenant config, not here.
DEFAULT_REVIEW_SLA_DAYS = 7


class AssuranceInboxItem(Base):
    """One finding awaiting (or having received) human review."""

    __tablename__ = "assurance_inbox_items"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'inbox-'")

    tenant_id = Column(String(255), nullable=False, index=True)

    # RG id with group_type='ai_system'. NULL = estate-level
    # finding not attributable to a single system.
    ai_system_id = Column(String(255), nullable=True, index=True)

    category = Column(String(32), nullable=False)
    severity = Column(String(16), nullable=False)

    # Dedup key. Convention: "<category>:<stable discriminators>", e.g.
    # "guardrail_block:<system>:<config_id>:<check>". The partial unique
    # index below makes the upsert race-safe.
    fingerprint = Column(String(512), nullable=False)

    title = Column(String(500), nullable=False)

    # POINTERS ONLY, never copied content: {run_id?, action_id?,
    # proxy_log_id?, guardrail_config_id?, drift_event_id?, contract_id?,
    # contract_hash?, system_alert_id?, approval_request_id?, ...}
    evidence = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'"))

    occurrence_count = Column(Integer, nullable=False, default=1, server_default="1")
    first_seen_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    state = Column(String(16), nullable=False, default=INBOX_STATE_OPEN, server_default=INBOX_STATE_OPEN)

    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_by = Column(String(255), nullable=True)
    assignee_admin = Column(String(255), nullable=True)

    disposition = Column(String(32), nullable=True)
    resolution_note = Column(Text, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by = Column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "severity IN ('info','warning','critical')",
            name="ck_inbox_severity",
        ),
        CheckConstraint(
            "state IN ('open','acknowledged','resolved')",
            name="ck_inbox_state",
        ),
        CheckConstraint(
            "disposition IS NULL OR disposition IN "
            "('true_positive','false_positive','expected','fixed')",
            name="ck_inbox_disposition",
        ),
        # The dedup upsert target lives in the migration as a PARTIAL
        # unique index (state <> 'resolved') — SQLAlchemy can't express it
        # portably here, and create_all fresh-DB installs get it from
        # `Index(..., postgresql_where=...)` below.
        Index(
            "uq_inbox_open_fingerprint",
            "tenant_id",
            "fingerprint",
            unique=True,
            postgresql_where=text("state != 'resolved'"),
            # The tests' in-memory SQLite honors partial indexes too — but
            # only via its own dialect kwarg; without it the index degrades
            # to a FULL unique and resolved+fresh items collide.
            sqlite_where=text("state != 'resolved'"),
        ),
        Index("ix_inbox_triage", "tenant_id", "state", "severity", "last_seen_at"),
        Index("ix_inbox_system", "tenant_id", "ai_system_id", "state"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "category": self.category,
            "severity": self.severity,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "evidence": self.evidence or {},
            "occurrence_count": self.occurrence_count,
            "first_seen_at": _iso(self.first_seen_at),
            "last_seen_at": _iso(self.last_seen_at),
            "state": self.state,
            "acknowledged_at": _iso(self.acknowledged_at),
            "acknowledged_by": self.acknowledged_by,
            "assignee_admin": self.assignee_admin,
            "disposition": self.disposition,
            "resolution_note": self.resolution_note,
            "resolved_at": _iso(self.resolved_at),
            "resolved_by": self.resolved_by,
        }


class AssuranceInboxEvent(Base):
    """Append-only trail: who did what to an item, when. Never updated."""

    __tablename__ = "assurance_inbox_events"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'inboxev-'")
    item_id = Column(
        String(64),
        ForeignKey("assurance_inbox_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = Column(String(255), nullable=False, index=True)
    at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    admin = Column(
        String(255),
        nullable=True,
        comment="NULL for machine events (created/reoccurred)",
    )
    event = Column(String(32), nullable=False)
    detail = Column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "event IN ('created','reoccurred','acknowledged','assigned',"
            "'resolved','reopened')",
            name="ck_inbox_event",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "item_id": self.item_id,
            "at": _iso(self.at),
            "admin": self.admin,
            "event": self.event,
            "detail": self.detail,
        }


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Assurance Inbox service.

One write path (`raise_item`) shared by every CP-side emitter, a triage
state machine whose every transition writes an append-only
``assurance_inbox_events`` row, and the two read aggregations the rest of
assurance consumes:

* ``review_score`` — the 7th health dimension. NULL when the system has
  never had an inbox item (unmeasured, never a free pass); 1.0 when there
  is no review debt; 0.5 when an OPEN critical item is older than the
  review SLA (degraded under worst-of).
* ``human_review_section`` — the Assurance Report block: raised/reviewed/
  open counts, disposition mix, median time-to-disposition, SLA status.

Emitters NEVER fail their caller: `raise_item` swallows and logs its own
errors — a broken inbox must not break drift response or health capture.
"""

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.assurance_inbox import (
    DEFAULT_REVIEW_SLA_DAYS,
    EVENT_ACKNOWLEDGED,
    EVENT_ASSIGNED,
    EVENT_CREATED,
    EVENT_REOCCURRED,
    EVENT_REOPENED,
    EVENT_RESOLVED,
    INBOX_CATEGORIES,
    INBOX_DISPOSITIONS,
    INBOX_SEVERITIES,
    INBOX_STATE_ACKNOWLEDGED,
    INBOX_STATE_OPEN,
    INBOX_STATE_RESOLVED,
    AssuranceInboxEvent,
    AssuranceInboxItem,
)

_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# Re-occurrence events are throttled: the occurrence_count on the item is
# the precise record; one trail event per hour per item is plenty for the
# human-facing history.
_REOCCURRENCE_EVENT_MIN_INTERVAL = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{ULID()}"


class AssuranceInboxService:
    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("assurance-inbox")

    # ── Write path (emitters) ───────────────────────────────────────────

    def raise_item(
        self,
        db: Session,
        *,
        tenant_id: str,
        category: str,
        severity: str,
        fingerprint: str,
        title: str,
        ai_system_id: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Optional[AssuranceInboxItem]:
        """Dedup-upsert a finding. NEVER raises — emitters must not fail
        their host loop over inbox trouble. Flushes but does not commit;
        the caller's transaction owns the commit."""
        try:
            assert category in INBOX_CATEGORIES, f"unknown category {category}"
            assert severity in INBOX_SEVERITIES, f"unknown severity {severity}"

            item = (
                db.query(AssuranceInboxItem)
                .filter(
                    AssuranceInboxItem.tenant_id == tenant_id,
                    AssuranceInboxItem.fingerprint == fingerprint,
                    AssuranceInboxItem.state != INBOX_STATE_RESOLVED,
                )
                .with_for_update()
                .first()
            )
            now = _now()
            if item is not None:
                item.occurrence_count = (item.occurrence_count or 1) + 1
                item.last_seen_at = now
                if _SEVERITY_RANK[severity] > _SEVERITY_RANK.get(item.severity, 0):
                    item.severity = severity
                if evidence:
                    merged = dict(item.evidence or {})
                    merged.update(evidence)
                    item.evidence = merged
                last_event = (
                    db.query(AssuranceInboxEvent)
                    .filter(AssuranceInboxEvent.item_id == item.id)
                    .order_by(AssuranceInboxEvent.at.desc())
                    .first()
                )
                throttled = (
                    last_event is not None
                    and last_event.event == EVENT_REOCCURRED
                    and last_event.at is not None
                    and (now - _aware(last_event.at))
                    < _REOCCURRENCE_EVENT_MIN_INTERVAL
                )
                if not throttled:
                    self._event(db, item, EVENT_REOCCURRED, admin=None)
                db.flush()
                return item

            # Fresh item. If a RESOLVED item with this fingerprint exists,
            # link it so "we keep resolving this" stays visible.
            previous = (
                db.query(AssuranceInboxItem.id)
                .filter(
                    AssuranceInboxItem.tenant_id == tenant_id,
                    AssuranceInboxItem.fingerprint == fingerprint,
                    AssuranceInboxItem.state == INBOX_STATE_RESOLVED,
                )
                .order_by(AssuranceInboxItem.resolved_at.desc())
                .first()
            )
            item = AssuranceInboxItem(
                id=_new_id("inbox"),
                tenant_id=tenant_id,
                ai_system_id=ai_system_id,
                category=category,
                severity=severity,
                fingerprint=fingerprint,
                title=title[:500],
                evidence=evidence or {},
                occurrence_count=1,
                first_seen_at=now,
                last_seen_at=now,
                state=INBOX_STATE_OPEN,
            )
            db.add(item)
            # Flush the item BEFORE its first event: the two mappers carry
            # no relationship(), so the unit of work will not order the
            # inserts for us, and the event's FK needs the row to exist.
            db.flush()
            detail = {"previous_item_id": previous[0]} if previous else None
            self._event(db, item, EVENT_CREATED, admin=None, detail=detail)
            db.flush()
            return item
        except Exception as e:  # noqa: BLE001 — deliberate: emitters never fail
            self.logger.error(
                "Failed to raise assurance inbox item",
                context={
                    "tenant_id": tenant_id,
                    "category": category,
                    "fingerprint": fingerprint,
                    "error": str(e),
                },
            )
            return None

    # ── Triage state machine ────────────────────────────────────────────

    def acknowledge(
        self, db: Session, item: AssuranceInboxItem, *, admin: str
    ) -> AssuranceInboxItem:
        if item.state == INBOX_STATE_RESOLVED:
            raise ValueError("Cannot acknowledge a resolved item — reopen it first")
        item.state = INBOX_STATE_ACKNOWLEDGED
        item.acknowledged_at = _now()
        item.acknowledged_by = admin
        self._event(db, item, EVENT_ACKNOWLEDGED, admin=admin)
        return item

    def assign(
        self, db: Session, item: AssuranceInboxItem, *, admin: str, assignee: str
    ) -> AssuranceInboxItem:
        if item.state == INBOX_STATE_RESOLVED:
            raise ValueError("Cannot assign a resolved item — reopen it first")
        item.assignee_admin = assignee
        self._event(db, item, EVENT_ASSIGNED, admin=admin, detail={"assignee": assignee})
        return item

    def resolve(
        self,
        db: Session,
        item: AssuranceInboxItem,
        *,
        admin: str,
        disposition: str,
        note: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> AssuranceInboxItem:
        """`detail` adds provenance to the resolved event (for example the
        drift resolution that made this disposition); it never replaces the
        disposition recorded there."""
        if disposition not in INBOX_DISPOSITIONS:
            raise ValueError(f"Unknown disposition '{disposition}'")
        if not (note or "").strip():
            raise ValueError("A resolution note is required — dispositions are evidence")
        if item.state == INBOX_STATE_RESOLVED:
            raise ValueError("Item is already resolved")
        item.state = INBOX_STATE_RESOLVED
        item.disposition = disposition
        item.resolution_note = note.strip()
        item.resolved_at = _now()
        item.resolved_by = admin
        self._event(
            db, item, EVENT_RESOLVED, admin=admin,
            detail={**(detail or {}), "disposition": disposition},
        )
        return item

    def reopen(
        self, db: Session, item: AssuranceInboxItem, *, admin: str, reason: str
    ) -> AssuranceInboxItem:
        """Reopening does NOT erase the prior disposition from the trail —
        the resolved event stays; the item simply becomes open again."""
        if item.state != INBOX_STATE_RESOLVED:
            raise ValueError("Only resolved items can be reopened")
        if not (reason or "").strip():
            raise ValueError("A reason is required to reopen")
        item.state = INBOX_STATE_OPEN
        item.disposition = None
        item.resolution_note = None
        item.resolved_at = None
        item.resolved_by = None
        self._event(db, item, EVENT_REOPENED, admin=admin, detail={"reason": reason})
        return item

    def _event(
        self,
        db: Session,
        item: AssuranceInboxItem,
        event: str,
        *,
        admin: Optional[str],
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        db.add(
            AssuranceInboxEvent(
                id=_new_id("inboxev"),
                item_id=item.id,
                tenant_id=item.tenant_id,
                at=_now(),
                admin=admin,
                event=event,
                detail=detail,
            )
        )

    # ── Read aggregations ───────────────────────────────────────────────

    def review_score(
        self,
        db: Session,
        *,
        tenant_id: str,
        ai_system_id: str,
        sla_days: int = DEFAULT_REVIEW_SLA_DAYS,
    ) -> Optional[float]:
        """Health dimension input (worst-of compatible).

        NULL  → this system has never had an inbox item: unmeasured.
        1.0   → items exist (now or historically), no review debt.
        0.5   → an OPEN critical item is older than the SLA: degraded.
        """
        base = db.query(func.count(AssuranceInboxItem.id)).filter(
            AssuranceInboxItem.tenant_id == tenant_id,
            AssuranceInboxItem.ai_system_id == ai_system_id,
        )
        if (base.scalar() or 0) == 0:
            return None
        cutoff = _now() - timedelta(days=sla_days)
        overdue = (
            db.query(func.count(AssuranceInboxItem.id))
            .filter(
                AssuranceInboxItem.tenant_id == tenant_id,
                AssuranceInboxItem.ai_system_id == ai_system_id,
                AssuranceInboxItem.state != INBOX_STATE_RESOLVED,
                AssuranceInboxItem.severity == "critical",
                AssuranceInboxItem.first_seen_at < cutoff,
            )
            .scalar()
            or 0
        )
        return 0.5 if overdue > 0 else 1.0

    def human_review_section(
        self,
        db: Session,
        *,
        tenant_id: str,
        ai_system_id: Optional[str],
        period_hours: int,
        sla_days: int = DEFAULT_REVIEW_SLA_DAYS,
    ) -> Dict[str, Any]:
        """The Assurance Report's 'human review' block. Pure read."""
        since = _now() - timedelta(hours=period_hours)
        q = db.query(AssuranceInboxItem).filter(
            AssuranceInboxItem.tenant_id == tenant_id
        )
        if ai_system_id is not None:
            q = q.filter(AssuranceInboxItem.ai_system_id == ai_system_id)

        raised = q.filter(AssuranceInboxItem.first_seen_at >= since).count()
        resolved_rows = q.filter(
            AssuranceInboxItem.state == INBOX_STATE_RESOLVED,
            AssuranceInboxItem.resolved_at >= since,
        ).all()

        open_by_severity: Dict[str, int] = {s: 0 for s in ("info", "warning", "critical")}
        open_rows = q.filter(AssuranceInboxItem.state != INBOX_STATE_RESOLVED).all()
        for row in open_rows:
            open_by_severity[row.severity] = open_by_severity.get(row.severity, 0) + 1

        dispositions: Dict[str, int] = {}
        ttd_hours = []
        for row in resolved_rows:
            if row.disposition:
                dispositions[row.disposition] = dispositions.get(row.disposition, 0) + 1
            if row.resolved_at and row.first_seen_at:
                delta = _aware(row.resolved_at) - _aware(row.first_seen_at)
                ttd_hours.append(delta.total_seconds() / 3600.0)

        cutoff = _now() - timedelta(days=sla_days)
        overdue_critical = sum(
            1
            for row in open_rows
            if row.severity == "critical" and _aware(row.first_seen_at) < cutoff
        )

        return {
            "raised": raised,
            "resolved": len(resolved_rows),
            "open": len(open_rows),
            "open_by_severity": open_by_severity,
            "dispositions": dispositions,
            "median_hours_to_disposition": (
                round(median(ttd_hours), 1) if ttd_hours else None
            ),
            "review_sla_days": sla_days,
            "overdue_critical": overdue_critical,
            "sla_met": overdue_critical == 0,
        }

    def counts(self, db: Session, *, tenant_id: str) -> Dict[str, Any]:
        """Mission Control badge numbers."""
        rows = (
            db.query(
                AssuranceInboxItem.state,
                AssuranceInboxItem.severity,
                func.count(AssuranceInboxItem.id),
            )
            .filter(AssuranceInboxItem.tenant_id == tenant_id)
            .group_by(AssuranceInboxItem.state, AssuranceInboxItem.severity)
            .all()
        )
        out: Dict[str, Any] = {
            "open": 0,
            "acknowledged": 0,
            "resolved": 0,
            "open_critical": 0,
            "open_warning": 0,
            "open_info": 0,
        }
        for state, severity, count in rows:
            out[state] = out.get(state, 0) + count
            if state != INBOX_STATE_RESOLVED and severity in ("critical", "warning", "info"):
                out[f"open_{severity}"] += count
        return out


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

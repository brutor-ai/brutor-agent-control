# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Drift resolution the detector honours

Closing a drift finding used to change only its status and note, so the
same samples re-fired the same finding and a response policy overrode a
human restore with no new evidence. A close now says WHICH fact it records,
and each kind has one defined effect on every signal that reads the samples:

| kind              | effect                                                        |
|-------------------|---------------------------------------------------------------|
| `accepted_change` | baseline epoch at the change point (else detection time)      |
| `cause_fixed`     | incident window [change point, now] (bounds editable)         |
| `false_positive`  | baseline epoch at the DETECTION time, kind `false_positive`:  |
|                   | the evidence the detector misread is not read again           |
| `auto_cleared`    | none — the control plane closed it because it cleared         |

Epochs are keyed (system, metric, basis, caller): the finding's own; a
finding with no caller (the aggregate baseline) writes caller 'all', which
covers every caller of the metric.

## One closer

`close_finding` is the ONLY code that moves a drift finding to a closed
status (static guard: `tests/test_drift_resolution.py::
test_only_the_closer_closes_findings`; runtime guard: the ORM refuses to
flush a closed finding without a kind). Automatic closers call
`auto_clear`. Human closes go through `resolve_finding`, which also writes
the effect and seals the declaration after the commit —
best-effort, never silent: a failed seal leaves `record_id` NULL and raises
a capture-gap item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from app.constants.drift_resolution import (
    CAUSE_OTHER,
    EPOCH_ALL_BUCKETS,
    EPOCH_ALL_CALLERS,
    HUMAN_RESOLUTION_KINDS,
    INCIDENT_CAUSE_CATEGORIES,
    INCIDENT_RESOURCE_TYPES,
    KIND_ACCEPTED_CHANGE,
    KIND_AUTO_CLEARED,
    KIND_CAUSE_FIXED,
    KIND_FALSE_POSITIVE,
    RESOLUTION_KINDS,
)
from app.database.models.ai_system_drift import (
    BASIS_RUNS,
    DRIFT_FALSE_POSITIVE,
    DRIFT_RESOLVED,
    AISystemDriftEvent,
)
from app.database.models.ai_system_drift_resolution import (
    WINDOW_LINK_ATTACHED,
    WINDOW_LINK_DECLARED,
    AISystemBaselineEpoch,
    AISystemIncidentWindow,
    AISystemIncidentWindowFinding,
)
from app.services.compliance.sealing import SealOutcome
from app.services.drift_inbox_link import disposition_inbox_item

MAX_NOTE = 2000


class ResolutionError(ValueError):
    """A resolution the request cannot make (422)."""


class ResolutionConflict(ValueError):
    """The finding is already closed (409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# The one closer
# ---------------------------------------------------------------------------


def close_finding(
    event: AISystemDriftEvent,
    *,
    kind: str,
    by: Optional[str],
    note: Optional[str] = None,
    at: Optional[datetime] = None,
) -> None:
    """Close `event` with `kind`. THE only place a finding is closed.
    Flushes nothing; the caller owns the transaction."""
    if kind not in RESOLUTION_KINDS:
        raise ResolutionError(f"kind must be one of {list(RESOLUTION_KINDS)}, got {kind!r}")
    event.status = DRIFT_FALSE_POSITIVE if kind == KIND_FALSE_POSITIVE else DRIFT_RESOLVED
    event.resolution_kind = kind
    event.resolved_at = at or _now()
    event.resolved_by = by
    if note:
        event.resolution_note = note


def auto_clear(
    event: AISystemDriftEvent, *, by: str, note: str, at: Optional[datetime] = None
) -> None:
    """The control plane closes a finding whose condition cleared on its
    own. `auto_cleared` has no effect on detection."""
    close_finding(event, kind=KIND_AUTO_CLEARED, by=by, note=note, at=at)


# ---------------------------------------------------------------------------
# Human resolutions
# ---------------------------------------------------------------------------


@dataclass
class ResolutionOutcome:
    event: AISystemDriftEvent
    baseline_epoch: Optional[AISystemBaselineEpoch] = None
    incident_window: Optional[AISystemIncidentWindow] = None
    #: cause_fixed: True when the finding attached to an existing window.
    attached: bool = False
    seal: Optional[SealOutcome] = None


def evidence_start(event: AISystemDriftEvent) -> datetime:
    """Where a finding's evidence begins: its change point, else when it was
    detected."""
    return _aware(event.change_point_at or event.detected_at) or _now()


def _epoch_for(
    db: Session, event: AISystemDriftEvent, *, kind: str, starts_at: datetime,
    by: Optional[str], reason: Optional[str],
) -> AISystemBaselineEpoch:
    """Get-or-create: a second resolution at the same point is the same fact."""
    caller = event.caller_system_id or EPOCH_ALL_CALLERS
    basis = event.basis or BASIS_RUNS
    existing = (
        db.query(AISystemBaselineEpoch)
        .filter(
            AISystemBaselineEpoch.tenant_id == event.tenant_id,
            AISystemBaselineEpoch.ai_system_id == event.ai_system_id,
            AISystemBaselineEpoch.metric_key == event.metric_key,
            AISystemBaselineEpoch.basis == basis,
            AISystemBaselineEpoch.caller_system_id == caller,
            AISystemBaselineEpoch.bucket == EPOCH_ALL_BUCKETS,
            AISystemBaselineEpoch.starts_at == starts_at,
        )
        .first()
    )
    if existing is not None:
        return existing
    epoch = AISystemBaselineEpoch(
        id=f"epoch-{ULID()}",
        tenant_id=event.tenant_id,
        ai_system_id=event.ai_system_id,
        metric_key=event.metric_key,
        basis=basis,
        caller_system_id=caller,
        starts_at=starts_at,
        kind=kind,
        drift_event_id=event.id,
        reason=reason,
        declared_by=by,
    )
    db.add(epoch)
    return epoch


def validate_window_fields(
    *,
    starts_at: Optional[datetime],
    ends_at: Optional[datetime],
    cause_category: Optional[str],
    reason: Optional[str],
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> None:
    if starts_at is None or ends_at is None:
        raise ResolutionError("an incident window needs starts_at and ends_at")
    if _aware(ends_at) <= _aware(starts_at):
        raise ResolutionError("ends_at must be after starts_at")
    if cause_category not in INCIDENT_CAUSE_CATEGORIES:
        raise ResolutionError(
            f"cause_category must be one of {list(INCIDENT_CAUSE_CATEGORIES)}"
        )
    if not (reason or "").strip():
        raise ResolutionError("an incident window needs a reason")
    if resource_type is not None and resource_type not in INCIDENT_RESOURCE_TYPES:
        raise ResolutionError(
            f"resource_type must be one of {list(INCIDENT_RESOURCE_TYPES)}"
        )
    if (resource_type is None) != (resource_id is None):
        raise ResolutionError("resource_type and resource_id go together")


def resolve_finding(
    db: Session,
    event: AISystemDriftEvent,
    *,
    kind: str,
    by: Optional[str],
    note: str,
    window_starts_at: Optional[datetime] = None,
    window_ends_at: Optional[datetime] = None,
    cause_category: Optional[str] = None,
    tenant_wide: bool = False,
    incident_window_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ResolutionOutcome:
    """Close a finding with a human kind, write its effect, commit, seal.

    `incident_window_id` (cause_fixed only, instead of window bounds):
    attach the finding to an EXISTING window of the tenant whose scope
    covers the finding's system — N identical findings of one incident close
    against one window rather than declaring N identical windows.

    Raises ResolutionError (bad kind / window), ResolutionConflict (already
    closed)."""
    if kind not in HUMAN_RESOLUTION_KINDS:
        raise ResolutionError(
            f"kind must be one of {list(HUMAN_RESOLUTION_KINDS)} (auto_cleared is the "
            "control plane's own)"
        )
    note = (note or "").strip()
    if not note:
        raise ResolutionError("a resolution needs a note")
    if not event.is_open:
        raise ResolutionConflict(
            f"drift finding {event.id!r} is already closed ({event.resolution_kind or event.status})"
        )
    if kind != KIND_CAUSE_FIXED and (
        window_starts_at or window_ends_at or cause_category or tenant_wide or incident_window_id
    ):
        raise ResolutionError(
            "window bounds, cause_category and incident_window_id apply to cause_fixed only"
        )
    if incident_window_id and (window_starts_at or window_ends_at or cause_category or tenant_wide):
        raise ResolutionError(
            "incident_window_id attaches to an existing window; it excludes the window fields"
        )
    now = _aware(now) or _now()

    outcome = ResolutionOutcome(event=event)
    if kind == KIND_ACCEPTED_CHANGE:
        outcome.baseline_epoch = _epoch_for(
            db, event, kind=KIND_ACCEPTED_CHANGE, starts_at=evidence_start(event), by=by,
            reason=note,
        )
    elif kind == KIND_FALSE_POSITIVE:
        outcome.baseline_epoch = _epoch_for(
            db, event, kind=KIND_FALSE_POSITIVE,
            starts_at=_aware(event.detected_at) or now, by=by, reason=note,
        )
    elif incident_window_id:
        window = (
            db.query(AISystemIncidentWindow)
            .filter(
                AISystemIncidentWindow.id == incident_window_id,
                AISystemIncidentWindow.tenant_id == event.tenant_id,
            )
            .first()
        )
        if window is None:
            raise ResolutionError(f"incident window {incident_window_id!r} not found")
        if window.ai_system_id not in (None, event.ai_system_id):
            raise ResolutionError(
                "the incident window's scope does not cover this finding's AI System"
            )
        outcome.incident_window = window
        outcome.attached = True
        db.add(AISystemIncidentWindowFinding(
            incident_window_id=window.id, drift_event_id=event.id, tenant_id=event.tenant_id,
            role=WINDOW_LINK_ATTACHED, attached_by=by,
        ))
    else:
        starts = _aware(window_starts_at) or evidence_start(event)
        ends = _aware(window_ends_at) or now
        category = cause_category or CAUSE_OTHER
        validate_window_fields(
            starts_at=starts, ends_at=ends, cause_category=category, reason=note,
        )
        window = AISystemIncidentWindow(
            id=f"iw-{ULID()}",
            tenant_id=event.tenant_id,
            ai_system_id=None if tenant_wide else event.ai_system_id,
            starts_at=starts,
            ends_at=ends,
            cause_category=category,
            reason=note,
            drift_event_id=event.id,
            declared_by=by,
        )
        db.add(window)
        db.flush()
        db.add(AISystemIncidentWindowFinding(
            incident_window_id=window.id, drift_event_id=event.id, tenant_id=event.tenant_id,
            role=WINDOW_LINK_DECLARED, attached_by=by,
        ))
        outcome.incident_window = window

    close_finding(event, kind=kind, by=by, note=note, at=now)
    # One decision, recorded once: the finding's Inbox item takes the same
    # disposition, note and person, in this transaction.
    disposition_inbox_item(db, event, kind=kind, by=by, note=note)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ResolutionConflict(f"could not record the resolution: {exc.orig}") from exc

    effect = outcome.baseline_epoch or outcome.incident_window
    outcome.seal = _seal_effect(db, event, effect, attached=outcome.attached)
    db.refresh(event)
    return outcome


def _seal_effect(db: Session, event: AISystemDriftEvent, effect: Any, *, attached: bool) -> SealOutcome:
    from app.services.compliance.sealing_hooks import seal_drift_resolution

    outcome = seal_drift_resolution(db, event, effect, attached=attached)
    if outcome.sealed:
        # The effect keeps the record that DECLARED it; an attachment's own
        # record lives on its link row.
        if not attached and effect.record_id is None:
            effect.record_id = outcome.record_id
            effect.sealed_at = _now()
        if isinstance(effect, AISystemIncidentWindow):
            link = db.get(AISystemIncidentWindowFinding, (effect.id, event.id))
            if link is not None:
                link.record_id = outcome.record_id
                link.sealed_at = _now()
    db.commit()
    db.refresh(effect)
    return outcome


# ---------------------------------------------------------------------------
# Incident windows declared directly
# ---------------------------------------------------------------------------


def _seal_window(db: Session, window: AISystemIncidentWindow, change: str) -> SealOutcome:
    from app.services.compliance.sealing_hooks import seal_incident_window

    outcome = seal_incident_window(db, window, change)
    if outcome.sealed:
        window.record_id = outcome.record_id
        window.sealed_at = _now()
    else:
        # The row changed; the old record no longer describes it.
        window.record_id = None
        window.sealed_at = None
    db.commit()
    db.refresh(window)
    return outcome


def declare_window(
    db: Session,
    *,
    tenant_id: str,
    ai_system_id: Optional[str],
    starts_at: datetime,
    ends_at: datetime,
    cause_category: str,
    reason: str,
    by: Optional[str],
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
) -> tuple:
    """Declare a window (a planned maintenance, an outage nobody resolved a
    finding for). Returns (window, seal outcome)."""
    validate_window_fields(
        starts_at=starts_at, ends_at=ends_at, cause_category=cause_category, reason=reason,
        resource_type=resource_type, resource_id=resource_id,
    )
    window = AISystemIncidentWindow(
        id=f"iw-{ULID()}",
        tenant_id=tenant_id,
        ai_system_id=ai_system_id,
        resource_type=resource_type,
        resource_id=resource_id,
        starts_at=_aware(starts_at),
        ends_at=_aware(ends_at),
        cause_category=cause_category,
        reason=reason.strip(),
        declared_by=by,
    )
    db.add(window)
    db.commit()
    from app.services.compliance.sealing_hooks import WINDOW_DECLARED

    return window, _seal_window(db, window, WINDOW_DECLARED)


_EDITABLE = ("starts_at", "ends_at", "cause_category", "reason", "resource_type", "resource_id")


def update_window(
    db: Session, window: AISystemIncidentWindow, *, changes: Dict[str, Any], by: Optional[str]
) -> tuple:
    """Edit a window; every edit is a new sealed declaration. Returns
    (window, seal outcome)."""
    unknown = set(changes) - set(_EDITABLE)
    if unknown:
        raise ResolutionError(f"not editable: {sorted(unknown)}")
    merged = {k: getattr(window, k) for k in _EDITABLE}
    merged.update(changes)
    if isinstance(merged.get("reason"), str):
        merged["reason"] = merged["reason"].strip()
    validate_window_fields(**merged)
    for k, v in changes.items():
        setattr(window, k, _aware(v) if isinstance(v, datetime) else merged[k])
    window.updated_by = by
    window.updated_at = _now()
    db.commit()
    from app.services.compliance.sealing_hooks import WINDOW_UPDATED

    return window, _seal_window(db, window, WINDOW_UPDATED)


def withdraw_window(db: Session, window: AISystemIncidentWindow) -> SealOutcome:
    """Delete a window. The withdrawal is sealed first (the record describes
    the window being withdrawn); the samples count again from now on."""
    from app.services.compliance.sealing_hooks import WINDOW_WITHDRAWN, seal_incident_window

    outcome = seal_incident_window(db, window, WINDOW_WITHDRAWN)
    db.delete(window)
    db.commit()
    return outcome


__all__ = [
    "MAX_NOTE",
    "ResolutionConflict",
    "ResolutionError",
    "ResolutionOutcome",
    "auto_clear",
    "close_finding",
    "declare_window",
    "evidence_start",
    "resolve_finding",
    "update_window",
    "validate_window_fields",
    "withdraw_window",
]

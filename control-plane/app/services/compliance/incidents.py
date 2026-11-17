# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The incident register

One incident, several frameworks: a model misbehaving in a credit
decision can be an EU AI Act Art 73 serious incident, a DORA major
ICT-related incident and a GDPR personal-data breach at once, each with
its own clock. The register keeps one row and computes every clock from
the frameworks' own `deadline_rules` (catalogs in
`app/services/compliance/catalogs/`) and the incident's classification.

## Deadlines: shown, never enforced

`deadline_rules` is `{name: timedelta}` per framework. Two shapes exist
and are read the same way everywhere (`select_rules`):

* **Alternatives** (EU AI Act `standard | death | widespread`, GDPR,
  HIPAA, Colorado): the incident's classification picks one rule when it
  names one, else `standard` applies.
* **Stages** (DORA `initial → intermediate → final`, NIS2 `early_warning →
  notification → final`): the framework's `deadline_stages` say what each
  stage's clock runs from — awareness, the classification as major /
  significant (`classified_at`), or the SUBMISSION of an earlier stage
  (`stage_submissions`, recorded with `record_submission`). DORA's initial
  notification is due min(classified + 4 h, aware + 24 h); before
  classification is recorded only the 24 h bound is shown and the row says
  it is awaiting classification. A stage whose anchor stage has not been
  submitted has no due time yet (`waiting`). Each stage is judged by its
  own submission time; `reported_at` counts as the submission of the
  framework's first stage when that stage has no recorded submission.
  Closing an incident does not satisfy an unsubmitted stage
  (`closed_unsubmitted`). The `deadline_rules` of a staged framework are
  only earliest bounds and never used as due times.

Nothing blocks on a deadline: a missed one is a fact on the page and an
`incident_deadline` inbox item, not a refusal.

## Every state change is a record

Create, update, report, stage submissions, close and notes each append a
`compliance_incident_events` row and seal an `ai.brutor.incident` record
(`context.state` = the event, `context.incident_id`). The description and
every other free-text field enter only as the payload digest; each record
references the previous one (`ai.brutor.incident`, purpose
`previous_state`), so the sealed history is a chain an auditor can walk.
A seal failure never loses the change — the event is saved with
`record_id = NULL` and the response says `sealed: false`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.ai_system_run import AISystemRun
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.compliance_incident import (
    INCIDENT_EVENT_CLOSED,
    INCIDENT_EVENT_CREATED,
    INCIDENT_EVENT_NOTE,
    INCIDENT_EVENT_REPORTED,
    INCIDENT_EVENT_SUBMITTED,
    INCIDENT_EVENT_UPDATED,
    INCIDENT_STATUS_CLOSED,
    INCIDENT_STATUS_OPEN,
    INCIDENT_STATUS_REPORTED,
    ComplianceIncident,
    ComplianceIncidentEvent,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import sealing
from app.services.compliance import offering
from app.services.compliance.catalogs.base import ANCHOR_AWARE, ANCHOR_CLASSIFIED, Framework

SEVERITIES = ("low", "medium", "high", "critical")

#: An unmet deadline this close (or past) raises an `incident_deadline` item.
DEADLINE_WARNING_WINDOW = timedelta(hours=24)

#: Deadline statuses (per framework × rule).
DL_OPEN = "open"
DL_DUE_SOON = "due_soon"
DL_OVERDUE = "overdue"
DL_REPORTED_ON_TIME = "reported_on_time"
DL_REPORTED_LATE = "reported_late"
DL_CLOSED = "closed"
#: Staged regimes: the stage's anchor stage has not been submitted, so its
#: clock has not started (no due time).
DL_WAITING = "waiting"
#: Staged regimes: the incident was closed without this stage submitted.
DL_CLOSED_UNSUBMITTED = "closed_unsubmitted"

#: `waiting_for` on a deadline row whose due time is bounded only because
#: the classification time is not recorded yet.
WAITING_FOR_CLASSIFICATION = "classification"

STAGE_REPORT = "report"
STAGE_FOLLOW_UP = "follow_up"


class IncidentError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return _aware(dt).isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


def select_rules(rules: Mapping[str, timedelta], classification: Optional[str]) -> Dict[str, timedelta]:
    """The rules that apply to an incident of `classification` (module doc)."""
    if not rules:
        return {}
    if classification and classification in rules:
        return {classification: rules[classification]}
    if "standard" in rules:
        return {"standard": rules["standard"]}
    return dict(rules)


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str) and value:
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def framework_submissions(
    catalog: Framework, stage_submissions: Optional[Mapping[str, Any]], reported_at: Optional[datetime]
) -> Dict[str, datetime]:
    """`{stage: submitted_at}` for one staged framework. `reported_at`
    counts as the submission of the first stage when that stage has no
    recorded submission (incidents reported before stages were recorded)."""
    raw = (stage_submissions or {}).get(catalog.id) or {}
    out: Dict[str, datetime] = {}
    if isinstance(raw, dict):
        for name, value in raw.items():
            at = _parse_time(value)
            if at is not None and catalog.deadline_stage(name) is not None:
                out[name] = at
    first = catalog.deadline_stages[0].name if catalog.deadline_stages else None
    reported = _aware(reported_at)
    if first is not None and first not in out and reported is not None:
        out[first] = reported
    return out


def stage_schedule(
    catalog: Framework,
    *,
    became_aware_at: datetime,
    classified_at: Optional[datetime],
    submissions: Mapping[str, datetime],
) -> List[Dict[str, Any]]:
    """Each stage of a staged framework with its anchor and due time:
    `[{name, anchor, anchor_at, due, waiting_for}]` (`due` None = waiting
    for the anchor stage's submission)."""
    aware = _aware(became_aware_at)
    classified = _aware(classified_at)
    out: List[Dict[str, Any]] = []
    for st in catalog.deadline_stages:
        anchor_at: Optional[datetime]
        waiting_for: Optional[str] = None
        if st.anchor == ANCHOR_AWARE:
            anchor_at = aware
        elif st.anchor == ANCHOR_CLASSIFIED:
            anchor_at = classified
            if anchor_at is None:
                waiting_for = WAITING_FOR_CLASSIFICATION
        else:
            anchor_at = submissions.get(st.anchor_stage or "")
            if anchor_at is None:
                waiting_for = st.anchor_stage
        due = st.due_from(anchor_at) if anchor_at is not None else None
        if st.cap_after_aware is not None:
            cap = aware + st.cap_after_aware
            due = cap if due is None else min(due, cap)
        out.append({"name": st.name, "anchor": st.anchor, "anchor_at": anchor_at, "due": due,
                    "waiting_for": waiting_for})
    return out


def compute_deadlines(
    frameworks: Iterable[str],
    classification: Optional[str],
    became_aware_at: datetime,
    *,
    classified_at: Optional[datetime] = None,
    stage_submissions: Optional[Mapping[str, Any]] = None,
    reported_at: Optional[datetime] = None,
) -> Dict[str, Dict[str, str]]:
    """`{framework_id: {rule: ISO due time}}` — frameworks without deadline
    rules map to `{}` (in scope, no statutory clock). A staged framework
    lists only the stages whose clock has started (a stage waiting for an
    earlier submission has no due time yet); recompute whenever an anchor
    (classification time, a submission) changes."""
    aware = _aware(became_aware_at)
    out: Dict[str, Dict[str, str]] = {}
    for fid in frameworks:
        catalog = offering.catalog(fid)
        if catalog is not None and catalog.staged:
            subs = framework_submissions(catalog, stage_submissions, reported_at)
            out[fid] = {
                row["name"]: row["due"].isoformat()
                for row in stage_schedule(catalog, became_aware_at=aware, classified_at=classified_at,
                                          submissions=subs)
                if row["due"] is not None
            }
            continue
        rules = select_rules(catalog.deadline_rules if catalog else {}, classification)
        out[fid] = {
            name: (aware + delta).isoformat()
            for name, delta in sorted(rules.items(), key=lambda kv: kv[1])
        }
    return out


def incident_deadlines(incident: ComplianceIncident) -> Dict[str, Dict[str, str]]:
    """`compute_deadlines` for a stored incident (every anchor it records)."""
    return compute_deadlines(
        incident.frameworks or [], incident.classification, incident.became_aware_at,
        classified_at=incident.classified_at, stage_submissions=incident.stage_submissions,
        reported_at=incident.reported_at,
    )


def _clock_status(due: datetime, now: datetime) -> str:
    if now > due:
        return DL_OVERDUE
    if due - now <= DEADLINE_WARNING_WINDOW:
        return DL_DUE_SOON
    return DL_OPEN


def _hours(due: Optional[datetime], now: datetime) -> Optional[float]:
    return round((due - now).total_seconds() / 3600, 1) if due is not None else None


def staged_status(
    catalog: Framework,
    *,
    became_aware_at: datetime,
    classified_at: Optional[datetime],
    stage_submissions: Optional[Mapping[str, Any]],
    reported_at: Optional[datetime],
    closed: bool,
    now: datetime,
) -> List[Dict[str, Any]]:
    """Every stage of one staged framework with its status (pure; shared by
    `deadline_status` and the `incidents.*` resolvers)."""
    subs = framework_submissions(catalog, stage_submissions, reported_at)
    rows: List[Dict[str, Any]] = []
    schedule = stage_schedule(catalog, became_aware_at=became_aware_at, classified_at=classified_at,
                              submissions=subs)
    for idx, st in enumerate(schedule):
        due: Optional[datetime] = st["due"]
        submitted = subs.get(st["name"])
        if submitted is not None and due is not None:
            status = DL_REPORTED_ON_TIME if submitted <= due else DL_REPORTED_LATE
        elif closed:
            status = DL_CLOSED_UNSUBMITTED
        elif due is None:
            status = DL_WAITING
        else:
            status = _clock_status(due, now)
        rows.append({
            "framework_id": catalog.id,
            "rule": st["name"],
            "stage": STAGE_REPORT if idx == 0 else STAGE_FOLLOW_UP,
            "due_at": due.isoformat() if due is not None else None,
            "status": status,
            "hours_remaining": _hours(due, now),
            "anchor": st["anchor"],
            "anchor_at": _iso(st["anchor_at"]),
            "waiting_for": st["waiting_for"],
            "submitted_at": _iso(submitted),
            "staged": True,
        })
    return rows


def deadline_status(incident: ComplianceIncident, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every deadline on the incident with its status, earliest first
    (stages still waiting for their anchor last)."""
    now = now or _now()
    reported_at = _aware(incident.reported_at)
    closed = incident.status == INCIDENT_STATUS_CLOSED
    rows: List[Dict[str, Any]] = []
    for fid, rules in sorted((incident.deadlines or {}).items()):
        catalog = offering.catalog(fid)
        if catalog is not None and catalog.staged:
            # Due times come from the stages and the incident's anchors, not
            # the stored snapshot: rows stored before stages were recorded
            # measured every stage from awareness.
            rows.extend(staged_status(
                catalog, became_aware_at=incident.became_aware_at, classified_at=incident.classified_at,
                stage_submissions=incident.stage_submissions, reported_at=reported_at, closed=closed,
                now=now,
            ))
            continue
        ordered = sorted(
            ((name, datetime.fromisoformat(due)) for name, due in (rules or {}).items()),
            key=lambda kv: kv[1],
        )
        for idx, (name, due) in enumerate(ordered):
            due = _aware(due)
            stage = STAGE_REPORT if idx == 0 else STAGE_FOLLOW_UP
            if stage == STAGE_REPORT and reported_at is not None:
                status = DL_REPORTED_ON_TIME if reported_at <= due else DL_REPORTED_LATE
            elif stage == STAGE_FOLLOW_UP and closed:
                status = DL_CLOSED
            else:
                status = _clock_status(due, now)
            rows.append({
                "framework_id": fid,
                "rule": name,
                "stage": stage,
                "due_at": due.isoformat(),
                "status": status,
                "hours_remaining": _hours(due, now),
                "anchor": ANCHOR_AWARE,
                "anchor_at": _iso(incident.became_aware_at),
                "waiting_for": None,
                "submitted_at": _iso(reported_at) if stage == STAGE_REPORT else None,
                "staged": False,
            })
    rows.sort(key=lambda r: (r["due_at"] is None, r["due_at"] or ""))
    return rows


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_frameworks(frameworks: Iterable[str]) -> List[str]:
    out: List[str] = []
    for fid in frameworks:
        known = offering.any_catalog(fid)
        if known is None:
            raise IncidentError(f"Unknown framework {fid!r}")
        reason = offering.unavailable_reason(known)
        if reason is not None:
            raise IncidentError(reason, 409)
        if fid not in out:
            out.append(fid)
    if not out:
        raise IncidentError("An incident needs at least one framework in scope")
    return out


def _validate_system(db: Session, tenant_id: str, ai_system_id: Optional[str]) -> Optional[str]:
    if not ai_system_id:
        return None
    row = (
        db.query(ResourceGroup.id)
        .filter(
            ResourceGroup.id == ai_system_id,
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
        .first()
    )
    if row is None:
        raise IncidentError(f"AI System {ai_system_id!r} not found", 404)
    return ai_system_id


def _validate_links(db: Session, tenant_id: str, model: Any, ids: Optional[Iterable[str]], what: str) -> List[str]:
    wanted = [i for i in dict.fromkeys(ids or []) if i]
    if not wanted:
        return []
    found = {
        r[0]
        for r in db.query(model.id).filter(model.tenant_id == tenant_id, model.id.in_(wanted)).all()
    }
    missing = [i for i in wanted if i not in found]
    if missing:
        raise IncidentError(f"Unknown {what}: {missing[:5]}")
    return wanted


def _validate_severity(severity: str) -> str:
    if severity not in SEVERITIES:
        raise IncidentError(f"severity must be one of {list(SEVERITIES)}")
    return severity


def _validate_classified_at(value: Optional[datetime], became_aware_at: datetime) -> Optional[datetime]:
    at = _aware(value)
    if at is None:
        return None
    if at < _aware(became_aware_at):
        raise IncidentError("classified_at cannot precede became_aware_at")
    if at > _now() + timedelta(minutes=5):
        raise IncidentError("classified_at cannot be in the future")
    return at


def _validate_classification(classification: str) -> str:
    value = (classification or "").strip()
    if not value or len(value) > 64 or any(ch.isspace() for ch in value):
        raise IncidentError("classification must be a short identifier (e.g. serious_incident, death)")
    return value


# ---------------------------------------------------------------------------
# State changes
# ---------------------------------------------------------------------------


@dataclass
class Change:
    incident: ComplianceIncident
    event: ComplianceIncidentEvent
    seal: sealing.SealOutcome


def _payload(incident: ComplianceIncident, event: str, detail: Mapping[str, Any]) -> Dict[str, Any]:
    """Everything the record's payload digest covers (never stored in the
    record)."""
    return {
        "incident_id": incident.id,
        "event": event,
        "title": incident.title,
        "description": incident.description or "",
        "classification": incident.classification,
        "severity": incident.severity,
        "ai_system_id": incident.ai_system_id,
        "became_aware_at": _iso(incident.became_aware_at),
        "classified_at": _iso(incident.classified_at),
        "frameworks": list(incident.frameworks or []),
        "deadlines": incident.deadlines or {},
        "status": incident.status,
        "reported_at": _iso(incident.reported_at),
        "authority": incident.authority,
        "external_reference": incident.external_reference,
        "linked_run_ids": list(incident.linked_run_ids or []),
        "linked_inbox_item_ids": list(incident.linked_inbox_item_ids or []),
        "stage_submissions": incident.stage_submissions or {},
        "detail": dict(detail),
    }


def _record_change(
    db: Session,
    incident: ComplianceIncident,
    event: str,
    *,
    actor: Optional[str],
    detail: Optional[Mapping[str, Any]] = None,
) -> Change:
    detail = dict(detail or {})
    row = ComplianceIncidentEvent(
        id=f"ince-{ULID()}".lower(),
        incident_id=incident.id,
        tenant_id=incident.tenant_id,
        event=event,
        at=_now(),
        actor_id=actor,
        detail=detail,
    )
    db.add(row)
    db.flush()
    outcome = sealing.seal(
        incident.tenant_id,
        sealing.RECORD_INCIDENT,
        ai_system_id=incident.ai_system_id,
        context={
            "incident_id": incident.id,
            "state": event,
            "tags": sorted(incident.frameworks or []),
        },
        payload=_payload(incident, event, detail),
        references=sealing.references(
            sealing.reference(sealing.RECORD_INCIDENT, incident.last_record_id, "previous_state")
        ),
    )
    if outcome.sealed:
        row.record_id = outcome.record_id
        incident.last_record_id = outcome.record_id
    db.flush()
    return Change(incident=incident, event=row, seal=outcome)


def create_incident(
    db: Session,
    tenant_id: str,
    *,
    title: str,
    description: str,
    classification: str,
    severity: str,
    became_aware_at: datetime,
    frameworks: Iterable[str],
    ai_system_id: Optional[str] = None,
    linked_run_ids: Optional[Iterable[str]] = None,
    linked_inbox_item_ids: Optional[Iterable[str]] = None,
    classified_at: Optional[datetime] = None,
    actor: Optional[str] = None,
) -> Change:
    fws = _validate_frameworks(frameworks)
    classification = _validate_classification(classification)
    aware = _aware(became_aware_at)
    if aware > _now() + timedelta(minutes=5):
        raise IncidentError("became_aware_at cannot be in the future")
    classified = _validate_classified_at(classified_at, aware)
    incident = ComplianceIncident(
        id=f"inc-{ULID()}".lower(),
        tenant_id=tenant_id,
        ai_system_id=_validate_system(db, tenant_id, ai_system_id),
        title=title.strip(),
        description=description or "",
        classification=classification,
        severity=_validate_severity(severity),
        became_aware_at=aware,
        classified_at=classified,
        frameworks=fws,
        deadlines=compute_deadlines(fws, classification, aware, classified_at=classified),
        stage_submissions={},
        status=INCIDENT_STATUS_OPEN,
        linked_run_ids=_validate_links(db, tenant_id, AISystemRun, linked_run_ids, "run ids"),
        linked_inbox_item_ids=_validate_links(
            db, tenant_id, AssuranceInboxItem, linked_inbox_item_ids, "inbox item ids"
        ),
        created_by=actor,
    )
    db.add(incident)
    db.flush()
    return _record_change(db, incident, INCIDENT_EVENT_CREATED, actor=actor)


#: Fields PATCH may change, with how each is validated.
_PATCHABLE = (
    "title", "description", "classification", "severity", "became_aware_at", "classified_at", "frameworks",
    "ai_system_id", "linked_run_ids", "linked_inbox_item_ids", "authority", "external_reference",
)


def update_incident(
    db: Session, incident: ComplianceIncident, changes: Mapping[str, Any], *, actor: Optional[str]
) -> Optional[Change]:
    """Apply a partial update. Returns None when nothing changed (no event,
    no record). Deadlines are recomputed when a clock input changed."""
    if incident.status == INCIDENT_STATUS_CLOSED:
        raise IncidentError("A closed incident cannot be changed; add a note instead", 409)
    tenant = incident.tenant_id
    applied: Dict[str, Any] = {}
    new_values: Dict[str, Any] = {}
    for name in _PATCHABLE:
        if name not in changes:
            continue
        value = changes[name]
        if name == "title":
            value = (value or "").strip()
            if not value:
                raise IncidentError("title cannot be empty")
        elif name == "description":
            value = value or ""
        elif name == "classification":
            value = _validate_classification(value)
        elif name == "severity":
            value = _validate_severity(value)
        elif name == "became_aware_at":
            value = _aware(value)
            if value is None:
                raise IncidentError("became_aware_at cannot be cleared")
        elif name == "classified_at":
            value = _aware(value)
        elif name == "frameworks":
            value = _validate_frameworks(value or [])
        elif name == "ai_system_id":
            value = _validate_system(db, tenant, value)
        elif name == "linked_run_ids":
            value = _validate_links(db, tenant, AISystemRun, value, "run ids")
        elif name == "linked_inbox_item_ids":
            value = _validate_links(db, tenant, AssuranceInboxItem, value, "inbox item ids")
        current = getattr(incident, name)
        if _aware(current) == value if isinstance(current, datetime) else current == value:
            continue
        # Free text never enters the event detail either — only which field moved.
        applied[name] = None if name == "description" else _jsonable(value)
        new_values[name] = value
    if not applied:
        return None
    if {"became_aware_at", "classified_at", "frameworks"} & applied.keys():
        # Validate the clock inputs together, before anything is changed.
        after = SimpleNamespace(**{
            k: new_values.get(k, getattr(incident, k))
            for k in ("became_aware_at", "classified_at", "frameworks", "stage_submissions", "reported_at")
        })
        _validate_classified_at(after.classified_at, after.became_aware_at)
        _validate_submission_order(after)
    for name, value in new_values.items():
        setattr(incident, name, value)
    if {"classification", "became_aware_at", "classified_at", "frameworks"} & applied.keys():
        incident.deadlines = incident_deadlines(incident)
    incident.updated_at = _now()
    return _record_change(db, incident, INCIDENT_EVENT_UPDATED, actor=actor, detail={"changed": applied})


def report_incident(
    db: Session,
    incident: ComplianceIncident,
    *,
    authority: str,
    reported_at: Optional[datetime] = None,
    external_reference: Optional[str] = None,
    actor: Optional[str] = None,
) -> Change:
    if incident.status == INCIDENT_STATUS_CLOSED:
        raise IncidentError("A closed incident cannot be reported", 409)
    if not (authority or "").strip():
        raise IncidentError("authority is required (who the incident was reported to)")
    at = _aware(reported_at) or _now()
    if at < _aware(incident.became_aware_at):
        raise IncidentError("reported_at cannot precede became_aware_at")
    incident.status = INCIDENT_STATUS_REPORTED
    incident.reported_at = at
    incident.authority = authority.strip()
    if external_reference is not None:
        incident.external_reference = external_reference.strip() or None
    incident.deadlines = incident_deadlines(incident)
    incident.updated_at = _now()
    detail = {
        "reported_at": at.isoformat(),
        "deadlines": [
            {k: r[k] for k in ("framework_id", "rule", "status")}
            for r in deadline_status(incident)
            if r["stage"] == STAGE_REPORT
        ],
    }
    return _record_change(db, incident, INCIDENT_EVENT_REPORTED, actor=actor, detail=detail)


def _anchor_time(catalog: Framework, stage_name: str, incident: Any,
                 subs: Mapping[str, datetime]) -> Optional[datetime]:
    """The earliest a submission of `stage_name` can be: its anchor's time
    (awareness; classification when recorded; the anchor stage's
    submission). None = the anchor stage has not been submitted."""
    st = catalog.deadline_stage(stage_name)
    assert st is not None
    if st.anchor_stage is not None:
        return subs.get(st.anchor_stage)
    if st.anchor == ANCHOR_CLASSIFIED and incident.classified_at is not None:
        return _aware(incident.classified_at)
    return _aware(incident.became_aware_at)


def _validate_submission_order(incident: Any) -> None:
    """Every explicitly recorded submission still follows its anchor (after
    an anchor time moved). `incident` is the row or a proposed state of it.
    The `reported_at` fallback is not checked: it is one time shared by
    every framework in scope, recorded before the staged clocks existed."""
    for fid in incident.frameworks or []:
        catalog = offering.catalog(fid)
        if catalog is None or not catalog.staged:
            continue
        subs = framework_submissions(catalog, incident.stage_submissions, incident.reported_at)
        explicit = (incident.stage_submissions or {}).get(fid) or {}
        for name, at in subs.items():
            if name not in explicit:
                continue
            anchor = _anchor_time(catalog, name, incident, subs)
            if anchor is not None and at < anchor:
                raise IncidentError(
                    f"{fid} {name} was submitted at {at.isoformat()}, before its anchor "
                    f"({anchor.isoformat()})"
                )


def record_submission(
    db: Session,
    incident: ComplianceIncident,
    *,
    framework_id: str,
    stage: str,
    submitted_at: Optional[datetime] = None,
    actor: Optional[str] = None,
) -> Change:
    """Record that one stage of a staged regime (DORA initial /
    intermediate / final, NIS2 early warning / notification / final) was
    submitted. Starts the clock of the stage anchored on it."""
    if incident.status == INCIDENT_STATUS_CLOSED:
        raise IncidentError("A closed incident cannot record a submission", 409)
    if framework_id not in (incident.frameworks or []):
        raise IncidentError(f"Framework {framework_id!r} is not in scope for this incident")
    catalog = offering.catalog(framework_id)
    if catalog is None or not catalog.staged:
        raise IncidentError(
            f"{framework_id!r} has no staged reports; record the report with /report instead"
        )
    if catalog.deadline_stage(stage) is None:
        raise IncidentError(
            f"Unknown {framework_id} stage {stage!r}; one of {[s.name for s in catalog.deadline_stages]}"
        )
    recorded = ((incident.stage_submissions or {}).get(framework_id) or {})
    if stage in recorded:
        raise IncidentError(f"{framework_id} {stage} submission is already recorded", 409)
    at = _aware(submitted_at) or _now()
    if at > _now() + timedelta(minutes=5):
        raise IncidentError("submitted_at cannot be in the future")
    subs = framework_submissions(catalog, incident.stage_submissions, incident.reported_at)
    st = catalog.deadline_stage(stage)
    assert st is not None
    anchor = _anchor_time(catalog, stage, incident, subs)
    if anchor is None:
        raise IncidentError(
            f"{framework_id} {stage} is counted from the {st.anchor_stage} submission, which is not "
            "recorded yet", 409,
        )
    if at < anchor:
        raise IncidentError(f"submitted_at cannot precede its anchor ({st.anchor}: {anchor.isoformat()})")
    allsubs = {k: dict(v) for k, v in (incident.stage_submissions or {}).items() if isinstance(v, dict)}
    allsubs.setdefault(framework_id, {})[stage] = at.isoformat()
    incident.stage_submissions = allsubs
    incident.deadlines = incident_deadlines(incident)
    incident.updated_at = _now()
    row = next(
        (r for r in deadline_status(incident) if r["framework_id"] == framework_id and r["rule"] == stage),
        None,
    )
    detail = {
        "framework_id": framework_id,
        "stage": stage,
        "submitted_at": at.isoformat(),
        "due_at": row["due_at"] if row else None,
        "status": row["status"] if row else None,
    }
    return _record_change(db, incident, INCIDENT_EVENT_SUBMITTED, actor=actor, detail=detail)


def close_incident(
    db: Session, incident: ComplianceIncident, *, note: Optional[str] = None, actor: Optional[str] = None
) -> Change:
    if incident.status == INCIDENT_STATUS_CLOSED:
        raise IncidentError("Incident is already closed", 409)
    incident.status = INCIDENT_STATUS_CLOSED
    incident.updated_at = _now()
    # The note is free text: it is covered by the payload digest, and kept
    # on the CP-side event row where the console shows it.
    return _record_change(
        db, incident, INCIDENT_EVENT_CLOSED, actor=actor, detail={"note": note} if note else {}
    )


def add_note(db: Session, incident: ComplianceIncident, *, note: str, actor: Optional[str] = None) -> Change:
    if not (note or "").strip():
        raise IncidentError("note cannot be empty")
    return _record_change(db, incident, INCIDENT_EVENT_NOTE, actor=actor, detail={"note": note.strip()})


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Inbox: approaching / missed deadlines
# ---------------------------------------------------------------------------


def scan_deadlines(db: Session, *, now: Optional[datetime] = None, inbox=None) -> int:
    """Raise an `incident_deadline` item for every unsatisfied deadline due
    within 24 h or already past. Dedup per (incident, framework, rule) —
    the inbox bumps the open item rather than raising a second. Returns
    the number of items raised or bumped. Flushes; the caller commits."""
    from app.services.assurance_inbox import AssuranceInboxService

    now = now or _now()
    inbox = inbox or AssuranceInboxService()
    raised = 0
    rows = (
        db.query(ComplianceIncident)
        .filter(ComplianceIncident.status.in_((INCIDENT_STATUS_OPEN, INCIDENT_STATUS_REPORTED)))
        .all()
    )
    for incident in rows:
        for dl in deadline_status(incident, now=now):
            if dl["status"] not in (DL_DUE_SOON, DL_OVERDUE):
                continue
            overdue = dl["status"] == DL_OVERDUE
            item = inbox.raise_item(
                db,
                tenant_id=incident.tenant_id,
                category="incident_deadline",
                severity="critical" if overdue else "warning",
                fingerprint=f"incident_deadline:{incident.id}:{dl['framework_id']}:{dl['rule']}",
                title=(
                    f"Incident '{incident.title}': {dl['framework_id']} {dl['rule']} deadline "
                    + ("passed" if overdue else "due within 24 h")
                    + f" ({dl['due_at']})"
                ),
                ai_system_id=incident.ai_system_id,
                evidence={
                    "incident_id": incident.id,
                    "framework_id": dl["framework_id"],
                    "rule": dl["rule"],
                    "due_at": dl["due_at"],
                    "status": dl["status"],
                },
            )
            if item is None:
                continue
            raised += 1
            linked = list(incident.linked_inbox_item_ids or [])
            if item.id not in linked:
                incident.linked_inbox_item_ids = linked + [item.id]
    db.flush()
    return raised


def incident_view(incident: ComplianceIncident, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    data = incident.to_dict()
    data["deadline_status"] = deadline_status(incident, now=now)
    data["deadlines_enforced"] = False
    return data


__all__ = [
    "Change",
    "DEADLINE_WARNING_WINDOW",
    "DL_CLOSED_UNSUBMITTED",
    "DL_WAITING",
    "IncidentError",
    "SEVERITIES",
    "add_note",
    "close_incident",
    "compute_deadlines",
    "create_incident",
    "deadline_status",
    "framework_submissions",
    "incident_deadlines",
    "incident_view",
    "record_submission",
    "report_incident",
    "scan_deadlines",
    "select_rules",
    "stage_schedule",
    "staged_status",
    "update_incident",
]

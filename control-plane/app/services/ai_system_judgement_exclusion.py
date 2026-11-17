# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# ONE exclusion predicate (one fact, one computation)

A human resolution is a fact the detector must honour. Incident windows
and baseline epochs (`app/database/models/ai_system_drift_resolution.py`)
say which samples no longer count; this module is the only place the
control plane decides whether one does. Every signal that GRADES samples
reads it (guard: `tests/test_judgement_exclusion_single_predicate.py`):

  * envelope terms              — `ai_system_envelope`
  * health rate components      — `ai_system_assurance` (run facts)
  * service signals             — `ai_system_service_signals`
  * the down rule's latest-N    — `ai_system_liveness_state`,
                                  `ai_system_composition`
  * connection lines            — `ai_system_connection_status`
  * dependency fallbacks and external-edge boundary rates
                                — `ai_system_dependency_health`,
                                  `ai_system_composition` (A2A rows)
  * oversight (decided / raised approval holds)
                                — `ai_system_oversight` (health),
                                  `compliance/resolvers/oversight`
                                  (`oversight.exercised`)

**Performance, never facts**: every predicate
takes `purpose=PURPOSE_RATE`. Declared-vs-observed facts — conformance, the
composition checks (undeclared dependency, unaccepted caller, role
mismatch, grant/edge mismatch), the agent checks — see every sample and
never call this module; the guard requires that too.

## The rule (pinned: `app/constants/drift_resolution.py`, mirrored by core)

A sample is excluded when its timestamp lies in ``[starts_at, ends_at)`` of
an incident window scoped to one of its systems or to the whole tenant
(``ai_system_id IS NULL``), or — for a keyed metric — before the latest
baseline epoch of (system, metric, basis) on its caller or on ``'all'``.

Sample timestamps and systems: a run — ``started_at`` / ``ai_system_id``; a
run segment — ``first_action_at`` / ``segment_system_id`` and
``root_system_id``; a proxy_logs row — through its run (a run is excluded
whole, never half); an approval hold — ``created_at`` / the nearest AI
System at-or-above its ``requester_group_id`` (`judged_holds`).

**A window only excuses what it covers**: a hold
raised inside a declared window is not judged; a hold raised OUTSIDE every
window that lapsed because nobody decided it in time (overnight, say) is a
configuration / operations matter and always counts. There is no other
excuse for a lapsed hold — no grace rule, no automatic pardon.

## Excluded is not hidden

Nothing here deletes or filters evidence. Runs inside a window are still
recorded, sealed and exportable; `exclusion_summary` is what the assurance
report prints (every window, who declared it and why, how many runs it
excluded, the excluded hours) so coverage says *unmeasured* rather than
quietly reading 100 %.

## Findings

`finding_exclusion` answers the response evaluator's question — is this
finding's evidence (its change point, else its detection time) inside a
window, or at/before an epoch on its metric? At-or-before, not before: an
`accepted_change` epoch starts AT the finding's change point, and a
re-detection of that same shift carries the same change point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import exists, func, not_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.constants.drift_resolution import EPOCH_ALL_BUCKETS, EPOCH_ALL_CALLERS
from app.database.models.ai_system_drift_resolution import (
    AISystemBaselineEpoch,
    AISystemIncidentWindow,
)

_W = AISystemIncidentWindow
_E = AISystemBaselineEpoch


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Incident windows — SQL
# ---------------------------------------------------------------------------


#: Incident windows excuse PERFORMANCE, never FACTS.
#: The only purpose the exclusion serves: a rate / ratio / score / baseline /
#: drift / down-rule / connection-line judgement over samples. Declared-vs-
#: observed FACTS (an undeclared caller, a model used outside the contract,
#: an undeclared dependency, an unaccepted caller, a grant/edge mismatch)
#: see every sample and never call this module — an undeclared caller seen
#: only during an outage is still an undeclared caller. Every predicate
#: below takes `purpose=PURPOSE_RATE` so each call site states which kind of
#: judgement it makes; the static guard requires it, and requires the fact
#: modules not to call the predicate at all.
PURPOSE_RATE = "rate"


def _check_purpose(purpose: str) -> None:
    if purpose != PURPOSE_RATE:
        raise ValueError(
            "the judgement exclusion applies to rate judgements only "
            f"(purpose={PURPOSE_RATE!r}); facts are never excluded, got {purpose!r}"
        )


def in_incident_window(tenant_col: Any, ts_col: Any, *system_cols: Any) -> ColumnElement:
    """TRUE when the sample (`ts_col`, owned by `system_cols`) lies inside an
    incident window of one of its systems or of the whole tenant. A
    correlated EXISTS, so it composes with any query or aggregate."""
    scope = [_W.ai_system_id.is_(None)] + [_W.ai_system_id == c for c in system_cols]
    return exists().where(
        _W.tenant_id == tenant_col,
        or_(*scope),
        ts_col >= _W.starts_at,
        ts_col < _W.ends_at,
    )


def judged(tenant_col: Any, ts_col: Any, *system_cols: Any, purpose: str) -> ColumnElement:
    """THE window predicate: TRUE when the sample still counts for a rate."""
    _check_purpose(purpose)
    return not_(in_incident_window(tenant_col, ts_col, *system_cols))


def judged_run(run: Any, *, purpose: str) -> ColumnElement:
    """`judged` for an `ai_system_runs` row (or an alias of it)."""
    return judged(run.tenant_id, run.started_at, run.ai_system_id, purpose=purpose)


def judged_segment(seg: Any, *, purpose: str) -> ColumnElement:
    """`judged` for an `ai_system_run_segments` row: a window on the service
    or on the run's root covers it."""
    return judged(
        seg.tenant_id, seg.first_action_at, seg.segment_system_id, seg.root_system_id,
        purpose=purpose,
    )


def judged_log(log: Any, tenant_id: str, *, purpose: str) -> ColumnElement:
    """`judged` for a `proxy_logs` row of `tenant_id`, THROUGH ITS RUN (a run is
    excluded whole, never half): excluded when the run it belongs to
    (tenant, root_task_id) started inside a window of the run's system or a
    tenant-wide one. A row that belongs to no run is judged.

    The subquery must not reference the log row. Correlated, it multiplies the
    plan's estimated cost by the row count, which trips Postgres's JIT: over a
    second of compilation per call on a large tenant. Uncorrelated, Postgres
    computes the excluded runs once and hashes them. The caller filters the log
    rows to `tenant_id`."""
    from app.database.models.ai_system_run import AISystemRun

    _check_purpose(purpose)
    excluded = select(AISystemRun.root_task_id).where(
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.root_task_id.isnot(None),
        in_incident_window(AISystemRun.tenant_id, AISystemRun.started_at, AISystemRun.ai_system_id),
    )
    return or_(log.root_task_id.is_(None), log.root_task_id.notin_(excluded))


def judged_sql(tenant: str, ts: str, *systems: str, purpose: str) -> str:
    """The same predicate as SQL text, for `text()` queries. Arguments are
    SQL expressions (column references / bind names), never values."""
    _check_purpose(purpose)
    scope = " OR ".join(["iw.ai_system_id IS NULL"] + [f"iw.ai_system_id = {s}" for s in systems])
    return (
        "NOT EXISTS (SELECT 1 FROM ai_system_incident_windows iw "
        f"WHERE iw.tenant_id = {tenant} AND ({scope}) "
        f"AND {ts} >= iw.starts_at AND {ts} < iw.ends_at)"
    )


# ---------------------------------------------------------------------------
# Approval holds — oversight is a RATE (holds decided / holds raised)
# ---------------------------------------------------------------------------


def judged_holds(
    db: Session,
    tenant_id: str,
    holds: Sequence[Tuple[Optional[str], Optional[datetime]]],
    *,
    purpose: str,
) -> List[bool]:
    """THE window predicate for approval holds, one flag per hold: True when
    the hold still counts for the oversight rate.

    `holds` are ``(requester_group_id, created_at)`` pairs. A hold belongs
    to the nearest AI System at-or-above its requester group (the ledger's
    attribution, `ai_system_composition.nearest_ai_system_map`) and is
    excluded when it was RAISED inside an incident window of that system or
    a tenant-wide one — judged by when it was raised, never by when it
    lapsed. A hold raised outside every window counts, however it ended:
    a window only excuses what it covers."""
    _check_purpose(purpose)
    if not holds:
        return []
    stamps = [_aware(ts) for _, ts in holds if ts is not None]
    if not stamps:
        return [True] * len(holds)
    # One cheap probe first: no window overlaps the holds' span, nothing to
    # resolve (the common case — skips the group walk).
    in_span = windows_for(
        db, tenant_id, None, since=min(stamps), until=max(stamps) + timedelta(microseconds=1)
    )
    if not in_span:
        return [True] * len(holds)
    from app.services.ai_system_composition import nearest_ai_system_map

    system_of = nearest_ai_system_map(db, tenant_id)
    spans: Dict[Optional[str], List[Span]] = {}
    for w in in_span:
        spans.setdefault(w.ai_system_id, []).append(Span(w.starts_at, w.ends_at))
    tenant_wide = spans.get(None, [])
    out: List[bool] = []
    for group_id, ts in holds:
        if ts is None:
            out.append(True)
            continue
        system = system_of.get(group_id) if group_id else None
        scoped = tenant_wide + (spans.get(system, []) if system else [])
        out.append(not sample_excluded(ts, windows=scoped))
    return out


# ---------------------------------------------------------------------------
# Baseline epochs
# ---------------------------------------------------------------------------


def epoch_floor(
    db: Session,
    tenant_id: str,
    ai_system_id: str,
    metric_key: str,
    *,
    basis: str = "runs",
    caller_system_id: Optional[str] = None,
) -> Optional[datetime]:
    """The latest epoch start for (system, metric, basis) on this caller or
    on 'all' — samples before it are excluded. None: no epoch.

    Only bucket-'all' epochs: every metric the control plane grades
    is over all runs; an epoch on one outcome bucket (`catalogue_change`,
    the outcome catalogue) re-learns only that bucket's baseline in the core."""
    callers = [EPOCH_ALL_CALLERS]
    if caller_system_id and caller_system_id != EPOCH_ALL_CALLERS:
        callers.append(caller_system_id)
    value = (
        db.query(func.max(_E.starts_at))
        .filter(
            _E.tenant_id == tenant_id,
            _E.ai_system_id == ai_system_id,
            _E.metric_key == metric_key,
            _E.basis == basis,
            _E.caller_system_id.in_(callers),
            _E.bucket == EPOCH_ALL_BUCKETS,
        )
        .scalar()
    )
    return _aware(value)


def epoch_floors(
    db: Session, tenant_id: str, ai_system_id: str, metric_keys: Iterable[str], *, basis: str = "runs"
) -> Dict[str, datetime]:
    """{metric_key: floor} for the aggregate caller ('all') of each key that
    has an epoch — one query for a whole envelope."""
    keys = list(metric_keys)
    if not keys:
        return {}
    rows = (
        db.query(_E.metric_key, func.max(_E.starts_at))
        .filter(
            _E.tenant_id == tenant_id,
            _E.ai_system_id == ai_system_id,
            _E.metric_key.in_(keys),
            _E.basis == basis,
            _E.caller_system_id == EPOCH_ALL_CALLERS,
            _E.bucket == EPOCH_ALL_BUCKETS,
        )
        .group_by(_E.metric_key)
        .all()
    )
    return {k: _aware(v) for k, v in rows if v is not None}


def since_with_floor(since: Optional[datetime], floor: Optional[datetime]) -> Optional[datetime]:
    """The effective lower bound of a graded window: the later of the
    period start and the epoch floor (samples at/after the floor count)."""
    since, floor = _aware(since), _aware(floor)
    if floor is None:
        return since
    if since is None:
        return floor
    return max(since, floor)


# ---------------------------------------------------------------------------
# The pure predicate (Python side — findings, tests, the report)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    starts_at: datetime
    ends_at: datetime

    def contains(self, ts: datetime) -> bool:
        ts = _aware(ts)
        return _aware(self.starts_at) <= ts < _aware(self.ends_at)


def sample_excluded(
    ts: datetime, *, windows: Sequence[Span] = (), floor: Optional[datetime] = None
) -> bool:
    """THE rule for one sample, in Python. `windows` are those in scope for
    the sample's systems (or tenant-wide); `floor` is its epoch floor."""
    ts = _aware(ts)
    if floor is not None and ts < _aware(floor):
        return True
    return any(w.contains(ts) for w in windows)


def windows_for(
    db: Session,
    tenant_id: str,
    system_ids: Optional[Sequence[Optional[str]]],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> List[AISystemIncidentWindow]:
    """Incident windows scoped to any of `system_ids` or tenant-wide
    (`system_ids=None`: every window in the tenant) that overlap
    [since, until) (unbounded when None), oldest first."""
    q = db.query(_W).filter(_W.tenant_id == tenant_id)
    if system_ids is not None:
        ids = [s for s in system_ids if s]
        scope = [_W.ai_system_id.is_(None)] + ([_W.ai_system_id.in_(ids)] if ids else [])
        q = q.filter(or_(*scope))
    if since is not None:
        q = q.filter(_W.ends_at > since)
    if until is not None:
        q = q.filter(_W.starts_at < until)
    return q.order_by(_W.starts_at.asc(), _W.id.asc()).all()


def window_findings(db: Session, window_ids: Sequence[str]) -> Dict[str, List[str]]:
    """{window id: [drift event ids it closed]} — the finding that declared
    it first, then those attached, by time."""
    from app.database.models.ai_system_drift_resolution import AISystemIncidentWindowFinding as L

    out: Dict[str, List[str]] = {w: [] for w in window_ids}
    if not window_ids:
        return out
    for wid, eid in (
        db.query(L.incident_window_id, L.drift_event_id)
        .filter(L.incident_window_id.in_(list(window_ids)))
        .order_by(L.incident_window_id, L.role.desc(), L.attached_at, L.drift_event_id)
    ):
        out[wid].append(eid)
    return out


def window_run_count(db: Session, window: Any, ai_system_id: Optional[str] = None) -> int:
    """Runs whose start lies in the window: of `ai_system_id` when given,
    else of the window's system, else (tenant-wide) of every system."""
    from app.database.models.ai_system_run import AISystemRun

    q = db.query(func.count(AISystemRun.id)).filter(
        AISystemRun.tenant_id == window.tenant_id,
        AISystemRun.is_replay.is_(False),
        AISystemRun.started_at >= window.starts_at,
        AISystemRun.started_at < window.ends_at,
    )
    system = ai_system_id or window.ai_system_id
    if system is not None:
        q = q.filter(AISystemRun.ai_system_id == system)
    return int(q.scalar() or 0)


def epoch_covers(
    epoch: Any, metric_key: str, basis: Optional[str], caller_system_id: Optional[str]
) -> bool:
    """Whether `epoch` is one of the epochs `epoch_floor` reads for (metric,
    basis, caller): same metric and basis, caller the same or 'all', bucket
    'all'."""
    callers = {EPOCH_ALL_CALLERS, caller_system_id or EPOCH_ALL_CALLERS}
    return (
        epoch.metric_key == metric_key
        and (epoch.basis or "runs") == (basis or "runs")
        and (epoch.caller_system_id or EPOCH_ALL_CALLERS) in callers
        and (getattr(epoch, "bucket", None) or EPOCH_ALL_BUCKETS) == EPOCH_ALL_BUCKETS
    )


def evidence_exclusion(
    db: Session,
    tenant_id: str,
    ai_system_id: str,
    metric_key: str,
    at: Optional[datetime],
    *,
    basis: Optional[str] = None,
    caller_system_id: Optional[str] = None,
    windows: bool = True,
) -> Optional[Dict[str, Any]]:
    """Why evidence of `metric_key` on a system at time `at` no longer
    counts, or None: `at` lies inside an incident window of the system (or
    tenant-wide), or at/before the latest epoch on its (metric, basis,
    caller | 'all'). Findings and the control plane's own condition
    emitters (dependency, agent) ask this before acting or re-opening.

    `windows=False` for a FACT finding (an agent check): a window excuses
    performance, never facts, so only the finding's own epoch — a human's
    `accepted_change` / `false_positive` on that very metric — settles it."""
    at = _aware(at)
    if at is None:
        return None
    floor = epoch_floor(
        db, tenant_id, ai_system_id, metric_key,
        basis=basis or "runs", caller_system_id=caller_system_id,
    )
    if floor is not None and at <= floor:
        return {"reason": "before_epoch", "evidence_at": at.isoformat(), "epoch_starts_at": floor.isoformat()}
    if not windows:
        return None
    for w in windows_for(db, tenant_id, [ai_system_id], since=None, until=None):
        if Span(w.starts_at, w.ends_at).contains(at):
            return {
                "reason": "in_incident_window",
                "evidence_at": at.isoformat(),
                "incident_window_id": w.id,
            }
    return None


#: Drift classes whose findings are FACTS (declared vs observed), not rates:
#: an incident window never excuses them; only their own epoch does.
FACT_DRIFT_CLASSES = frozenset({"agent", "conformance"})


def finding_exclusion(db: Session, event: Any) -> Optional[Dict[str, Any]]:
    """Why a drift finding's evidence no longer counts, or None. Evidence
    time = the change point, else the detection time. A fact finding
    (`FACT_DRIFT_CLASSES`) is never excused by a window."""
    return evidence_exclusion(
        db,
        event.tenant_id,
        event.ai_system_id,
        event.metric_key,
        getattr(event, "change_point_at", None) or getattr(event, "detected_at", None),
        basis=getattr(event, "basis", None),
        caller_system_id=getattr(event, "caller_system_id", None),
        windows=getattr(event, "drift_class", None) not in FACT_DRIFT_CLASSES,
    )


# ---------------------------------------------------------------------------
# What the report prints
# ---------------------------------------------------------------------------


def _union_hours(spans: List[Tuple[datetime, datetime]]) -> float:
    total = 0.0
    cur_start: Optional[datetime] = None
    cur_end: Optional[datetime] = None
    for s, e in sorted(spans):
        if cur_end is None or s > cur_end:
            if cur_end is not None:
                total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = s, e
        elif e > cur_end:
            cur_end = e
    if cur_end is not None:
        total += (cur_end - cur_start).total_seconds()
    return round(total / 3600.0, 3)


def measurement_changes(epochs: List[Any]) -> List[Dict[str, Any]]:
    """The platform measurement changes behind `epochs` (kind
    `measurement_change`), one per registry entry, newest first: which of
    this system's metrics re-learn from when, and why."""
    from app.constants.drift_resolution import KIND_MEASUREMENT_CHANGE
    from app.constants.measurement_changes import REGISTRY

    versions = {m.id: m.introduced_in_version for m in REGISTRY}
    grouped: Dict[str, Dict[str, Any]] = {}
    for e in epochs:
        if e.kind != KIND_MEASUREMENT_CHANGE:
            continue
        key = e.measurement_change_id or e.id
        entry = grouped.setdefault(key, {
            "id": key,
            "metric_keys": [],
            "bases": [],
            "starts_at": _aware(e.starts_at).isoformat() if e.starts_at else None,
            "reason": e.reason or "",
            "introduced_in_version": versions.get(key),
        })
        if e.metric_key not in entry["metric_keys"]:
            entry["metric_keys"].append(e.metric_key)
        if e.basis not in entry["bases"]:
            entry["bases"].append(e.basis)
    out = list(grouped.values())
    for entry in out:
        entry["metric_keys"].sort()
        entry["bases"].sort()
    out.sort(key=lambda d: (d["starts_at"] or "", d["id"]), reverse=True)
    return out


def exclusion_summary(
    db: Session,
    tenant_id: str,
    ai_system_id: str,
    *,
    since: datetime,
    until: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Every incident window overlapping the period (with who declared it,
    why, and how many of the system's runs it excluded), every baseline
    epoch of the system, and the excluded period in hours and runs."""
    from app.database.models.ai_system_run import AISystemRun

    until = _aware(until) or datetime.now(timezone.utc)
    since = _aware(since)
    windows = windows_for(db, tenant_id, [ai_system_id], since=since, until=until)
    base_runs = db.query(func.count(AISystemRun.id)).filter(
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id == ai_system_id,
        AISystemRun.is_replay.is_(False),
    )
    out_windows: List[Dict[str, Any]] = []
    clipped: List[Tuple[datetime, datetime]] = []
    closed = window_findings(db, [w.id for w in windows])
    for w in windows:
        d = w.to_dict()
        # Each window once, with every finding it closed.
        d["drift_event_ids"] = closed.get(w.id, [])
        # The runs of THIS system the window excluded (a tenant-wide
        # window excludes every system's; the report is about this one).
        d["runs_excluded"] = window_run_count(db, w, ai_system_id)
        out_windows.append(d)
        clipped.append((max(_aware(w.starts_at), since), min(_aware(w.ends_at), until)))
    excluded_runs = int(
        base_runs.filter(
            AISystemRun.started_at >= since,
            not_(judged_run(AISystemRun, purpose=PURPOSE_RATE)),
        ).scalar()
        or 0
    )
    epochs = (
        db.query(_E)
        .filter(_E.tenant_id == tenant_id, _E.ai_system_id == ai_system_id)
        .order_by(_E.starts_at.desc(), _E.id.asc())
        .all()
    )
    return {
        "incident_windows": out_windows,
        "baseline_epochs": [e.to_dict() for e in epochs],
        # 1.9 — platform measurement changes: one entry per
        # registry change that started epochs on this system, with its reason.
        "measurement_changes": measurement_changes(epochs),
        "excluded_hours": _union_hours([(s, e) for s, e in clipped if e > s]),
        "excluded_runs": excluded_runs,
    }


__all__ = [
    "FACT_DRIFT_CLASSES",
    "PURPOSE_RATE",
    "Span",
    "epoch_covers",
    "epoch_floor",
    "epoch_floors",
    "evidence_exclusion",
    "exclusion_summary",
    "finding_exclusion",
    "in_incident_window",
    "judged",
    "judged_holds",
    "judged_log",
    "judged_run",
    "judged_segment",
    "judged_sql",
    "measurement_changes",
    "sample_excluded",
    "since_with_floor",
    "window_findings",
    "window_run_count",
    "windows_for",
]

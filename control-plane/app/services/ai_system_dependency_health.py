# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Dependency health in the root's verdict

A root's six signals already cover the delegated work inside its runs. What
they cannot say is whether the systems it DEPENDS on are fit to be depended
on. Dependency health is a new input to the worst-of verdict:

  * a **critical** shared-service dependency that is not healthy caps the
    root at *degraded*, with reason `dependency`, naming the dependency and
    its failing signal — the SRE rule that a service cannot be more
    available than its critical dependencies;
  * a **degradable** dependency adds a note, plus a check that the declared
    fallback held: every call the dependency left unanswered (segment outcome
    `failed` / `timed_out`) sits in a root run that still completed;
  * an **external** dependency contributes boundary metrics (A2A calls
    through the gateway: count, errors, p95 latency, and the mix of terminal
    A2A task states the root received — `proxy_logs.a2a_task_state`, written
    by the core) and the root's declared assumptions — basis `assumed`,
    never a tested status.

The per-edge status is `CompositionIndex.dependency_status` — the SAME
computation the Composition tab and the AI Systems list read. Its
responsiveness part is THE down rule (`ai_system_down_rule`):
the latest 3 calls from this caller all unanswered is failing signal
`liveness` (an outage — a critical finding); one unanswered among them is
`reliability` (degraded — still caps the root, a high finding).

## Responses

`emit_dependency_findings` turns a failing critical dependency (and a
degradable one whose fallback did not hold) into an open drift finding ON
THE ROOT, `drift_class='dependency'`, cause `dependency_degraded`,
`metric_key='dependency:<target>'`. The closed loop
(`ai_system_response`) considers it like any other finding, so a response
policy on `drift_class: dependency` — e.g. "critical dependency failing ⇒
approval_required" — targets it with no new mechanism. The finding resolves
itself when the dependency recovers. It does not count as the root's own
drift (`is_dependency_finding`): the root is capped at degraded, not
"drifting".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from ulid import ULID

from app.services.ai_system_down_rule import segment_call_outcome_sql
from app.database.models.ai_system_dependency import (
    CRITICALITY_CRITICAL,
    CRITICALITY_DEGRADABLE,
    ROLE_EXTERNAL,
    ROLE_SHARED_SERVICE,
    AISystemDependency,
)
from app.database.models.ai_system_drift import (
    CAUSE_DEPENDENCY_DEGRADED,
    DRIFT_DEPENDENCY,
    AISystemDriftEvent,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import (
    SEGMENT_UNANSWERED_OUTCOMES,
    AISystemRunSegment,
)
from app.database.models.resource_group import ResourceGroup
from app.services.ai_system_judgement_exclusion import (
    PURPOSE_RATE,
    evidence_exclusion,
    judged_log,
    judged_segment,
)
from app.services.drift_resolution import auto_clear
from app.services.ai_system_composition import (
    DEFAULT_COMPOSITION_WINDOW_HOURS,
    FAILING_SIGNAL_DEPENDENCY,
    HEALTH_DEGRADED,
    CompositionIndex,
    nearest_ai_system_map,
)
from app.services.ai_system_outcomes import RUN_NO_WORK, RUN_SUCCESS, run_class
from app.services.ai_system_service_signals import (
    DEPENDENCY_METRIC_PREFIX,
    is_dependency_finding,
)

BASIS_TESTED = "tested"
BASIS_ASSUMED = "assumed"
CAP_REASON_DEPENDENCY = "dependency"
#: Who resolves a dependency finding once the dependency recovers.
SYSTEM_ACTOR_DEPENDENCY_HEALTH = "system:dependency-health"

#: Run classes (THE classification, `ai_system_outcomes`) in which the root
#: still did its job: it ended in a declared outcome — the work, or a
#: declared "nothing to do".
_HELD_CLASSES = (RUN_SUCCESS, RUN_NO_WORK)
#: Failing signals that mean the dependency is not answering at all.
_OUTAGE_SIGNALS = ("liveness", "autonomy")


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def dependency_health(
    db: Session,
    system: ResourceGroup,
    *,
    index: Optional[CompositionIndex] = None,
    window_hours: int = DEFAULT_COMPOSITION_WINDOW_HOURS,
) -> List[Dict[str, Any]]:
    """One entry per declared edge of `system`, sorted by (name, edge id).

    Shape: `{edge_id, system_id, name, role, criticality,
    contributes_to_decision, status, failing_signal, via_system_id,
    via_system_name, basis, caps_root, note, fallback_check, boundary,
    assumptions}`.
    """
    has_edges = (
        db.query(AISystemDependency.id)
        .filter(
            AISystemDependency.tenant_id == system.tenant_id,
            AISystemDependency.ai_system_id == system.id,
        )
        .first()
    )
    if has_edges is None:
        return []
    # Scoped to what this system's dependency health can read — the health
    # scorer calls this once per system per tick.
    index = index or CompositionIndex(
        db, system.tenant_id, window_hours=window_hours, scope_root=system.id
    )
    window_start = index.now - timedelta(hours=index.window_hours)

    out: List[Dict[str, Any]] = []
    for e in index.edges_by_system.get(system.id, []):
        status, signal = index.dependency_status(system.id, e)
        external = e.role == ROLE_EXTERNAL
        name = (
            index.system_name(e.depends_on_system_id)
            if e.depends_on_system_id
            else None
        ) or index.card_name(e.agent_card_id) or e.depends_on_system_id or e.agent_card_id
        via = None
        if signal == FAILING_SIGNAL_DEPENDENCY and e.depends_on_system_id:
            failing = index.failing(e.depends_on_system_id)
            via = failing.via_system_id if failing else None
        caps = (
            e.role == ROLE_SHARED_SERVICE
            and e.criticality == CRITICALITY_CRITICAL
            and status == HEALTH_DEGRADED
        )
        entry: Dict[str, Any] = {
            "edge_id": e.id,
            "system_id": e.depends_on_system_id,
            "name": name,
            "role": e.role,
            "criticality": e.criticality,
            "contributes_to_decision": bool(e.contributes_to_decision),
            "status": status,
            "failing_signal": signal,
            "via_system_id": via,
            "via_system_name": index.system_name(via) if via else None,
            "basis": BASIS_ASSUMED if external else BASIS_TESTED,
            "caps_root": caps,
            "note": None,
            "fallback_check": None,
            "boundary": None,
            "assumptions": (e.assumptions or {}) if external else None,
        }
        if external:
            entry["boundary"] = _boundary(db, index, system.id, e.agent_card_id, window_start)
            entry["note"] = (
                "Assumed, not tested: Brutor sees only the boundary of this "
                "third-party agent; the root's declared assumptions stand in for "
                "evidence."
            )
        elif e.criticality == CRITICALITY_DEGRADABLE:
            check = _fallback_check(db, system, e.depends_on_system_id, window_start)
            entry["fallback_check"] = check
            entry["note"] = _degradable_note(e, status, check)
        out.append(entry)
    out.sort(key=lambda d: ((d["name"] or "").lower(), d["edge_id"]))
    return out


def _degradable_note(edge: AISystemDependency, status: str, check: Dict[str, Any]) -> str:
    fallback = (edge.fallback or "").strip()
    base = f"Degradable — declared fallback: {fallback!r}."
    if check["held"] is False:
        failed = check["unanswered_calls"] - check["root_runs_completed"]
        return (
            f"{base} The fallback did NOT hold: {failed} of "
            f"{check['unanswered_calls']} unanswered call(s) left the run incomplete."
        )
    if check["held"] is True:
        return (
            f"{base} Exercised {check['unanswered_calls']} time(s) in the window; "
            f"every affected run still completed."
        )
    if status == HEALTH_DEGRADED:
        return f"{base} The dependency is not healthy; the fallback has not been exercised yet."
    return base


def _fallback_check(
    db: Session, system: ResourceGroup, target: Optional[str], window_start: datetime
) -> Dict[str, Any]:
    """Did the runs complete in which this system's calls to the dependency
    went unanswered? (The run's terminal state is the run's; the calls are
    this system's own — `caller_system_id`.)"""
    if not target:
        return {"unanswered_calls": 0, "root_runs_completed": 0, "held": None}
    rows = (
        db.query(
            AISystemRun.terminal_state,
            AISystemRun.outcome_conformance,
            AISystemRun.outcome_counts_toward_baseline,
        )
        .join(AISystemRunSegment, AISystemRunSegment.run_id == AISystemRun.id)
        .filter(
            AISystemRunSegment.tenant_id == system.tenant_id,
            # The edge's owner is the IMMEDIATE caller:
            # its own calls to the dependency, whichever run they were in.
            AISystemRunSegment.caller_system_id == system.id,
            AISystemRunSegment.segment_system_id == target,
            # A gateway refusal (`failed` + `blocked_by_policy`)
            # is not the dependency failing to answer.
            segment_call_outcome_sql(AISystemRunSegment).in_(sorted(SEGMENT_UNANSWERED_OUTCOMES)),
            AISystemRunSegment.first_action_at >= window_start,
            # Calls inside an incident window are not graded.
            judged_segment(AISystemRunSegment, purpose=PURPOSE_RATE),
        )
        .all()
    )
    unanswered = len(rows)
    completed = sum(1 for row in rows if run_class(*row) in _HELD_CLASSES)
    return {
        "unanswered_calls": unanswered,
        "root_runs_completed": completed,
        "held": None if unanswered == 0 else completed == unanswered,
    }


def _boundary(
    db: Session, index: CompositionIndex, sid: str, card: Optional[str], window_start: datetime
) -> Dict[str, Any]:
    """A2A calls through the gateway from this system to the card.

    `calls` is the fact (every call); the error share, p95 and
    task-state mix are PERFORMANCE, over calls whose run lies outside every
    incident window (THE predicate, proxy-log rows judged through their
    run). `excluded_calls` says how many were left out."""
    agg = index.a2a_calls.get((sid, card or ""), {}) if card else {}
    calls = int(agg.get("calls", 0))
    judged_calls = int(agg.get("judged_calls", 0))
    errors = int(agg.get("judged_errors", 0))
    p95 = None
    task_states: Dict[str, int] = {}
    if card and judged_calls:
        from app.database.models.proxy_log import ProxyLog

        groups = [g for g, s in nearest_ai_system_map(db, index.tenant_id).items() if s == sid]
        rows = (
            db.query(ProxyLog.duration_ms, ProxyLog.a2a_task_state).filter(
                ProxyLog.tenant_id == index.tenant_id,
                ProxyLog.proxy_type == "a2a",
                ProxyLog.server_id == card,
                ProxyLog.resource_group_id.in_(groups),
                ProxyLog.timestamp >= window_start,
                judged_log(ProxyLog, index.tenant_id, purpose=PURPOSE_RATE),
            )
            .all()
        )
        durations = sorted(int(d) for d, _ in rows if d is not None)
        if durations:
            p95 = durations[min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))]
        # The task states the root received, as the core
        # normalised them (single source: the same column the run segments
        # read). A call that yielded no task state is not counted here.
        for _, state in rows:
            if state:
                task_states[state] = task_states.get(state, 0) + 1
    last = agg.get("last_seen_at")
    return {
        "calls": calls,
        "excluded_calls": calls - judged_calls,
        "errors": errors,
        "error_rate": (errors / judged_calls) if judged_calls else None,
        "p95_latency_ms": p95,
        "last_seen_at": last.isoformat() if last else None,
        "task_states": dict(sorted(task_states.items())),
    }


def dependency_cap(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The cap on the root's verdict, or None. The first capping entry in
    the (deterministic) entry order is named; the reasons list names all."""
    for d in entries:
        if d["caps_root"]:
            return {
                "reason": CAP_REASON_DEPENDENCY,
                "edge_id": d["edge_id"],
                "system_id": d["system_id"],
                "name": d["name"],
                "failing_signal": d["failing_signal"],
            }
    return None


def cap_reasons(entries: List[Dict[str, Any]]) -> List[str]:
    out = []
    for d in entries:
        if not d["caps_root"]:
            continue
        via = f" via {d['via_system_name'] or d['via_system_id']}" if d["via_system_id"] else ""
        out.append(
            f"Critical dependency {d['name']} is not healthy "
            f"({d['failing_signal']}{via}) — this system cannot be healthier "
            f"than a dependency it cannot work without."
        )
    return out


# ---------------------------------------------------------------------------
# Findings for the closed loop
# ---------------------------------------------------------------------------


def _finding_severity(entry: Dict[str, Any]) -> Optional[str]:
    if entry["caps_root"]:
        return "critical" if entry["failing_signal"] in _OUTAGE_SIGNALS else "high"
    check = entry.get("fallback_check") or {}
    if entry["criticality"] == CRITICALITY_DEGRADABLE and check.get("held") is False:
        return "medium"
    return None


def _latest_call_at(db: Session, system: ResourceGroup, target: Optional[str]) -> Optional[datetime]:
    """The latest JUDGED call from this system to `target` (None: none, or
    an external edge)."""
    if not target:
        return None
    from sqlalchemy import func

    return _aware(
        db.query(func.max(AISystemRunSegment.first_action_at))
        .filter(
            AISystemRunSegment.tenant_id == system.tenant_id,
            AISystemRunSegment.caller_system_id == system.id,
            AISystemRunSegment.segment_system_id == target,
            judged_segment(AISystemRunSegment, purpose=PURPOSE_RATE),
        )
        .scalar()
    )


def emit_dependency_findings(
    db: Session, system: ResourceGroup, entries: List[Dict[str, Any]],
    *, now: Optional[datetime] = None,
) -> int:
    """Open a `dependency` drift finding on the root for every failing
    dependency; resolve the ones whose dependency recovered. Returns the
    number opened. A standing finding is left as it is (dedup by metric)."""
    now = now or datetime.now(timezone.utc)
    open_events = {
        ev.metric_key: ev
        for ev in db.query(AISystemDriftEvent).filter(
            AISystemDriftEvent.tenant_id == system.tenant_id,
            AISystemDriftEvent.ai_system_id == system.id,
            AISystemDriftEvent.status.in_(("active", "acknowledged")),
        )
        if is_dependency_finding(ev.metric_key)
    }
    failing_keys = set()
    created = 0
    for entry in entries:
        severity = _finding_severity(entry)
        if severity is None:
            continue
        key = f"{DEPENDENCY_METRIC_PREFIX}{entry['system_id'] or entry['edge_id']}"
        failing_keys.add(key)
        if key in open_events:
            continue
        # A human resolution of this finding is honoured: when the
        # latest judged call behind the failure is at/before its epoch, it is
        # the same evidence and the finding is not re-opened.
        if evidence_exclusion(
            db, system.tenant_id, system.id, key,
            _latest_call_at(db, system, entry.get("system_id")) or now,
        ):
            continue
        db.add(AISystemDriftEvent(
            id=f"drift-{ULID()}",
            tenant_id=system.tenant_id,
            ai_system_id=system.id,
            drift_class=DRIFT_DEPENDENCY,
            metric_key=key,
            detected_at=now,
            change_point_at=now,
            direction="down",
            severity=severity,
            attributed_cause=CAUSE_DEPENDENCY_DEGRADED,
            cause_confidence=1.0,
            cause_evidence={
                "edge_id": entry["edge_id"],
                "dependency_system_id": entry["system_id"],
                "dependency_name": entry["name"],
                "criticality": entry["criticality"],
                "status": entry["status"],
                "failing_signal": entry["failing_signal"],
                "via_system_id": entry["via_system_id"],
                "fallback_check": entry.get("fallback_check"),
            },
        ))
        created += 1
    for key, ev in open_events.items():
        if key not in failing_keys:
            auto_clear(
                ev,
                by=SYSTEM_ACTOR_DEPENDENCY_HEALTH,
                note="The dependency is healthy again (or no longer declared).",
                at=now,
            )
    if created or open_events:
        db.flush()
    return created


__all__ = [
    "BASIS_ASSUMED",
    "BASIS_TESTED",
    "CAP_REASON_DEPENDENCY",
    "SYSTEM_ACTOR_DEPENDENCY_HEALTH",
    "cap_reasons",
    "dependency_cap",
    "dependency_health",
    "emit_dependency_findings",
]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Service signals (separately registered means separately assured)

A shared service is called inside other systems' runs and roots none of its
own. "It has no runs" is a fact about the ledger, never a verdict: its
signals are computed from its **segments** — the per-system slices the core
run ledger cuts from every run it serves — over the same windows and
minimums as a root system's signals are computed from runs.

`signal_context` decides the basis for one system, and every surface that
shows a signal reads it (the scorer and the health endpoint), so the basis a
number was computed on and the basis a panel says it was computed on cannot
disagree:

  * `runs`     — the system roots runs in the window (a root, or a system
                 that is both root and service: its own runs are the work it
                 is accountable for starting);
  * `segments` — no own runs in the window, but it served calls.

On the `segments` basis:

| signal | from |
|---|---|
| liveness | responsiveness when called (`ai_system_liveness_state`, unchanged) |
| behaviour | ready `basis='segments'` baselines, open drift on them |
| reliability | answered calls with a conforming outcome / served calls, and 1 − tool error rate |
| cost | the `cost_per_run` segment baseline (per served call) vs its open cost drift |
| conformance | models used in its segments vs grants; callers vs `intended_client_systems` |
| oversight | its approval holds — **not applicable** (never 0 %) when it raised none |

## Reliability — THE classification of a served call

A served call's outcome is classified by `ai_system_down_rule.
classify_segment_outcome`, the same function liveness, dependency health
and the connection line read:

  * **answered** (`answered`, `rejected`) — a success: a service that
    declines a task has answered it;
  * **unanswered** (`failed`, `timed_out`) — a failure;
  * **blocked** — the gateway refused the call (stored as
    `failed` + `blocked_by_policy`, read back as the refusal token by
    `segment_call_outcome`): EXCLUDED from the reliability denominator — the
    service never saw it, which says nothing about the service;
  * **deviated** — answered, but with an outcome the
    service's own contract does not declare (`outcome_conformance`
    `undeclared` / `unreported`): it answered (liveness) but did not do
    what it was approved to do, so it counts against reliability exactly as
    a deviating run does. A segment the core did not judge (conformance
    NULL) is read as answered.

`success_rate = (answered-class − deviated) / (served − blocked)`; None when
every call was blocked (no evidence). `rejected` and `blocked` stay visible in
`outcome_mix`, `rejected_calls` and `blocked_calls`; `deviated_calls` and
`error_causes` are disclosed beside them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models.ai_system_drift import (
    BASELINE_ALL_ACTORS,
    BASELINE_ALL_CALLERS,
    BASELINE_READY,
    BASIS_RUNS,
    BASIS_SEGMENTS,
    DRIFT_COST,
    AISystemBaseline,
    AISystemDriftEvent,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import (
    SEGMENT_OUTCOMES,
    SEGMENT_REJECTED,
    AISystemRunSegment,
)
from app.constants.outcomes import CONFORMANCE_UNDECLARED, CONFORMANCE_UNREPORTED
from app.services.ai_system_down_rule import (
    CALL_ANSWERED,
    CALL_BLOCKED,
    SEGMENT_REFUSED,
    classify_segment_outcome,
    segment_call_outcome,
)
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, judged_segment

#: The metric key a served call's cost is baselined under. The core keeps
#: the run-basis key on the segment basis ("the run is the served call").
COST_METRIC = "cost_per_run"

#: Open drift statuses (mirrors `AISystemDriftEvent.is_open`).
_OPEN = ("active", "acknowledged")

#: Metric-key prefix of the findings the dependency-health emitter raises on
#: a ROOT for a failing dependency. Not the root's own behaviour: they cap
#: the root at degraded (reason `dependency`) instead of counting as its
#: own drift. See `ai_system_dependency_health`. Wire contract with the core's
#: drift sweeper (`drift/mod.rs::metric::CP_DEPENDENCY_PREFIX`), which keeps
#: these events out of its own cooldown; pinned in tests/test_wire_contract.py.
DEPENDENCY_METRIC_PREFIX = "dependency:"


def is_dependency_finding(metric_key: Optional[str]) -> bool:
    return bool(metric_key) and metric_key.startswith(DEPENDENCY_METRIC_PREFIX)


#: Metric-key prefix of the agent implementation findings the control plane
#: opens on a system (`assurance_checks/agent.py`): the key is
#: `<finding code>:<agent identity>` and every code starts `agent.`. The
#: core's metric keys never do (they are snake_case metric names).
AGENT_FINDING_PREFIX = "agent."


def is_agent_finding(metric_key: Optional[str]) -> bool:
    return bool(metric_key) and metric_key.startswith(AGENT_FINDING_PREFIX)


def is_own_drift(metric_key: Optional[str]) -> bool:
    """Whether an open drift event is the system's OWN behaviour moving.

    Two CP-opened kinds are not: a dependency finding (it caps the verdict
    at degraded through the dependency instead) and an agent
    implementation finding (a governance finding about which release runs —
    it is in the inbox and answerable by response policies, but a release
    nobody approved is not the system behaving differently)."""
    return not is_dependency_finding(metric_key) and not is_agent_finding(metric_key)


@dataclass
class SignalContext:
    basis: str
    own_runs: int
    served_calls: int
    #: Approval holds the system raised in the window, on its basis.
    holds: int

    @property
    def is_service_basis(self) -> bool:
        return self.basis == BASIS_SEGMENTS

    @property
    def oversight_not_applicable(self) -> bool:
        """A service that raised no approval hold has no oversight loop to
        judge — `not_applicable`, rendered n/a, never 0 % and never a pass."""
        return self.is_service_basis and self.holds == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "basis": self.basis,
            "own_runs": self.own_runs,
            "served_calls": self.served_calls,
            "holds": self.holds,
        }


def signal_profile(system_kind: Optional[str], ctx: SignalContext) -> Dict[str, str]:
    """The per-signal levels for this system NOW: its kind's profile, with
    oversight `not_applicable` for a service that raised no hold. The scorer
    and the health endpoint both read this, so the level a signal was scored
    under is the level the panel shows."""
    from app.services.ai_system_signal_profile import NOT_APPLICABLE, profile_for

    prof = profile_for(system_kind)
    if ctx.oversight_not_applicable:
        prof["oversight"] = NOT_APPLICABLE
    return prof


def signal_context(db: Session, tenant_id: str, ai_system_id: str, since: datetime) -> SignalContext:
    own_runs = (
        db.query(func.count(AISystemRun.id))
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= since,
        )
        .scalar()
        or 0
    )
    served, holds = (
        db.query(
            func.count(AISystemRunSegment.id),
            func.sum(AISystemRunSegment.approval_request_count),
        )
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.segment_system_id == ai_system_id,
            AISystemRunSegment.first_action_at >= since,
        )
        .first()
        or (0, 0)
    )
    served = int(served or 0)
    basis = BASIS_SEGMENTS if own_runs == 0 and served > 0 else BASIS_RUNS
    if basis == BASIS_RUNS:
        holds = (
            db.query(func.sum(AISystemRun.approval_request_count))
            .filter(
                AISystemRun.tenant_id == tenant_id,
                AISystemRun.ai_system_id == ai_system_id,
                AISystemRun.is_replay.is_(False),
                AISystemRun.started_at >= since,
            )
            .scalar()
        )
    return SignalContext(
        basis=basis, own_runs=int(own_runs), served_calls=served, holds=int(holds or 0)
    )


def segment_facts(
    db: Session, tenant_id: str, ai_system_id: str, since: datetime
) -> Dict[str, Any]:
    """What a service did for its callers in the window — the segment
    counterpart of the assurance report's run facts.

    Graded over JUDGED calls only (THE exclusion predicate: a
    call inside an incident window of the service, of the run's root, or
    tenant-wide is kept as evidence and disclosed as `excluded_calls`)."""
    excluded_calls = int(
        db.query(func.count(AISystemRunSegment.id))
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.segment_system_id == ai_system_id,
            AISystemRunSegment.first_action_at >= since,
            ~judged_segment(AISystemRunSegment, purpose=PURPOSE_RATE),
        )
        .scalar()
        or 0
    )
    rows = (
        db.query(
            AISystemRunSegment.outcome,
            AISystemRunSegment.outcome_conformance,
            AISystemRunSegment.error_cause,
            func.count(AISystemRunSegment.id),
            func.sum(AISystemRunSegment.cost_usd),
            func.sum(AISystemRunSegment.tool_call_count),
            func.sum(AISystemRunSegment.tool_error_count),
            func.sum(AISystemRunSegment.guardrail_block_count),
            func.sum(AISystemRunSegment.policy_denial_count),
            func.sum(AISystemRunSegment.approval_request_count),
        )
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.segment_system_id == ai_system_id,
            AISystemRunSegment.first_action_at >= since,
            judged_segment(AISystemRunSegment, purpose=PURPOSE_RATE),
        )
        .group_by(
            AISystemRunSegment.outcome,
            AISystemRunSegment.outcome_conformance,
            AISystemRunSegment.error_cause,
        )
        .all()
    )
    by_outcome = {o: 0 for o in (*SEGMENT_OUTCOMES, SEGMENT_REFUSED)}
    error_causes: Dict[str, int] = {}
    total = 0
    cost = 0.0
    answered_cost = 0.0
    answered = deviated = blocked = 0
    tool_calls = tool_errors = blocks = denials = approvals = 0
    for stored, conformance, cause, n, c, tc, te, gb, pd, ar in rows:
        n = int(n)
        outcome = segment_call_outcome(stored, cause)
        by_outcome[outcome] = by_outcome.get(outcome, 0) + n
        total += n
        cost += float(c or 0)
        cls = classify_segment_outcome(outcome)
        if cls == CALL_BLOCKED:
            blocked += n
        elif cls == CALL_ANSWERED:
            if conformance in (CONFORMANCE_UNDECLARED, CONFORMANCE_UNREPORTED):
                deviated += n
            else:
                answered += n
                answered_cost += float(c or 0)
        if cause:
            error_causes[cause] = error_causes.get(cause, 0) + n
        tool_calls += int(tc or 0)
        tool_errors += int(te or 0)
        blocks += int(gb or 0)
        denials += int(pd or 0)
        approvals += int(ar or 0)
    # Gateway-refused calls never reached the service: out of the denominator.
    judged = total - blocked
    success_rate = (answered / judged) if judged else None
    tool_error_rate = (tool_errors / tool_calls) if tool_calls else None
    return {
        "served_calls": total,
        "excluded_calls": excluded_calls,
        "outcome_mix": by_outcome,
        #: answered-class with a conforming outcome: answered + rejected (the
        #: service declined — an answer), minus the deviated ones
        "answered_calls": answered,
        "rejected_calls": by_outcome.get(SEGMENT_REJECTED, 0),
        #: Answered with an undeclared / unreported outcome
        "deviated_calls": deviated,
        #: Failed served calls by cause
        "error_causes": dict(sorted(error_causes.items())),
        "blocked_calls": blocked,
        #: the reliability denominator: served − blocked
        "reliability_calls": judged,
        "success_rate": success_rate,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "tool_error_rate": tool_error_rate,
        "total_cost_usd": round(cost, 6),
        "cost_per_answered_call": (answered_cost / answered) if answered else None,
        "guardrail_blocks": blocks,
        "policy_denials": denials,
        "approval_requests": approvals,
    }


def segment_reliability(facts: Dict[str, Any]) -> Optional[float]:
    """Answered share (THE classification — see the module docstring),
    bounded by tool health: a service that answers every call while half
    its tool calls fail is not reliable."""
    success = facts.get("success_rate")
    if success is None:
        return None
    tool_err = facts.get("tool_error_rate")
    return success if tool_err is None else min(success, 1.0 - tool_err)


def _ready(db: Session, tenant_id: str, ai_system_id: str, basis: str,
           metric_key: Optional[str] = None) -> int:
    q = db.query(func.count(AISystemBaseline.id)).filter(
        AISystemBaseline.tenant_id == tenant_id,
        AISystemBaseline.ai_system_id == ai_system_id,
        AISystemBaseline.basis == basis,
        AISystemBaseline.state == BASELINE_READY,
    )
    if metric_key is not None:
        q = q.filter(
            AISystemBaseline.metric_key == metric_key,
            AISystemBaseline.bucket == "all",
            AISystemBaseline.actor_id == BASELINE_ALL_ACTORS,
            AISystemBaseline.caller_system_id == BASELINE_ALL_CALLERS,
        )
    return int(q.scalar() or 0)


def ready_baselines(db: Session, tenant_id: str, ai_system_id: str, basis: str) -> int:
    """Ready baselines on one basis. A root's run baselines never make a
    service 'ready', and a service's segment baselines never make its own
    runs 'ready' — they describe different populations."""
    return _ready(db, tenant_id, ai_system_id, basis)


def segment_cost_score(db: Session, tenant_id: str, ai_system_id: str) -> Optional[float]:
    """Cost per served call against the service's own normal: 1.0 while the
    aggregate cost baseline is ready and no cost drift is open on it, 0.5
    with one open, `None` until the baseline is ready."""
    if not _ready(db, tenant_id, ai_system_id, BASIS_SEGMENTS, COST_METRIC):
        return None
    open_cost = (
        db.query(func.count(AISystemDriftEvent.id))
        .filter(
            AISystemDriftEvent.tenant_id == tenant_id,
            AISystemDriftEvent.ai_system_id == ai_system_id,
            AISystemDriftEvent.basis == BASIS_SEGMENTS,
            AISystemDriftEvent.drift_class == DRIFT_COST,
            AISystemDriftEvent.status.in_(_OPEN),
        )
        .scalar()
        or 0
    )
    return 0.5 if open_cost else 1.0


def open_own_drift(db: Session, tenant_id: str, ai_system_id: str) -> int:
    """Open drift findings of the system's OWN — every basis, excluding the
    dependency findings raised on it for a failing dependency (those cap the
    verdict at degraded; they are not this system's behaviour)."""
    rows = (
        db.query(AISystemDriftEvent.metric_key)
        .filter(
            AISystemDriftEvent.tenant_id == tenant_id,
            AISystemDriftEvent.ai_system_id == ai_system_id,
            AISystemDriftEvent.status.in_(_OPEN),
        )
        .all()
    )
    return sum(1 for (key,) in rows if is_own_drift(key))


__all__ = [
    "COST_METRIC",
    "DEPENDENCY_METRIC_PREFIX",
    "SignalContext",
    "is_dependency_finding",
    "AGENT_FINDING_PREFIX",
    "is_agent_finding",
    "is_own_drift",
    "open_own_drift",
    "ready_baselines",
    "segment_cost_score",
    "segment_facts",
    "segment_reliability",
    "signal_context",
    "signal_profile",
]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# One liveness verdict (one fact, one computation)

Before this module the platform answered "is this AI System alive?" twice:

  * the liveness panel (`endpoints/admin/ai_system_liveness.py`) said
    *silent* only when a stored, silence-alerting expectation was overrun by
    a system that had run before ("never ran is unstarted, not silent");
  * the health scorer (`ai_system_assurance.compute_health`) said *silent*
    whenever the system had **no runs of its own** in the judgment window.

A shared service is called inside its callers' runs and never has a run of
its own, so it read *silent* on every rollup surface while the panel read
*not silent* — two rules, one fact, two answers.

`liveness_state` is now the only place the verdict is computed. The panel,
the health scorer and the AI Systems list all read it; a static guard
(`tests/test_liveness_single_computation.py`) fails if any other module in
`app/` computes liveness itself.

## Semantics by mode

| mode | alive | silent | otherwise |
|---|---|---|---|
| continuous / scheduled | activity within window + grace | a stored, enabled expectation overrun after the system has been active before | `unstarted` before the first activity; `not_configured` when no expectation is stored (the kind default is displayed, never enforced) |
| on_demand (services) | the latest inbound calls in the window were answered (one unanswered among the latest 3 = alive but *degraded*, score 0.5) | the latest 3 inbound calls in the window ALL went unanswered (segment outcome `failed` / `timed_out`) | `idle`: no inbound calls in the window — not a fault |
| sporadic | activity in the window | never | `idle` (active before) / `unstarted` |

"Activity" is a system's own runs **and** its run segments: for a service,
runs means segments. A disabled expectation is
`not_configured` — the operator switched the check off.

`idle`, `unstarted` and `not_configured` never score as silent.

## Work, not just activity — `work_quiet`

Prometheus practice: track the last run AND, separately, the last time the
job *processed something*. `last_run_at` is any own run (the agent is alive
and checking); `last_processed_run_at` is the latest `success` run (THE
classification, `ai_system_outcomes` — an outcome that counts toward the
baseline). A scheduled poller whose feed silently stopped keeps running and
reads alive; an expectation's optional `max_quiet_processed_seconds` turns
"no processed run for that long" into the separate `work_quiet` signal. It
never changes the state or the score — the system IS answering — and never
reads as silent.

## Why "the latest calls", not "any call" — and not "the latest call"

The contract phrase is "silent if an inbound call went unanswered". Taken
over a 30-day judgment window, one timeout on day 1 would hold a service
*silent* for a month while it answered every later call. Silence is a
statement about *now*, judged by THE down rule every surface shares
(`ai_system_down_rule.latest_calls_verdict`): the latest 3 inbound calls all unanswered is *silent*; one
unanswered among them is *alive* but degraded (score 0.5, below the
scorer's degraded threshold); a single timeout is not an outage.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models.ai_system_liveness import (
    LIVENESS_ON_DEMAND,
    LIVENESS_SPORADIC,
    SILENCE_ALERTING_MODES,
    AISystemLivenessExpectation,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.services.ai_system_down_rule import (
    CALL_UNANSWERED,
    CALLS_DEGRADED,
    CALLS_DOWN,
    DOWN_AFTER_CONSECUTIVE_FAILURES,
    VERDICT_SCORE,
    classify_segment_outcome,
    latest_calls_verdict,
    segment_call_outcome_sql,
)
from app.services.ai_system_judgement_exclusion import PURPOSE_RATE, judged_segment
from app.services.ai_system_outcomes import success_cond
from app.services.liveness_learning import default_mode_for

#: The judgment window every surface uses unless it passes its own. Equal to
#: the health scorer's default period so the panel and the scorer, called
#: with their defaults, judge the same interval.
DEFAULT_LIVENESS_WINDOW_HOURS = 720

STATE_ALIVE = "alive"
STATE_SILENT = "silent"
STATE_IDLE = "idle"
STATE_UNSTARTED = "unstarted"
STATE_NOT_CONFIGURED = "not_configured"
LIVENESS_STATES = (
    STATE_ALIVE, STATE_SILENT, STATE_IDLE, STATE_UNSTARTED, STATE_NOT_CONFIGURED,
)



def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def segment_calls_verdict(outcomes_newest_first: Iterable[str]) -> Optional[str]:
    """ok | degraded | down (or None: no inbound call) for a series of
    inbound calls (segment outcomes, newest first) — THE down rule applied
    to "was it answered?". Shared with the composition view's per-edge
    dependency health so the two can never disagree about what "answering"
    means. `rejected` (the delegate declined) is an answer; `blocked` (the
    gateway refused) says nothing about whether the delegate answers —
    the classification is `ai_system_down_rule.classify_segment_outcome`."""
    return latest_calls_verdict(
        classify_segment_outcome(o) == CALL_UNANSWERED for o in outcomes_newest_first
    )


@dataclass
class LivenessState:
    ai_system_id: str
    state: str
    reason: str
    mode: str
    configured: bool
    #: Latest own run start or segment start, whichever is later.
    last_activity_at: Optional[datetime]
    last_run_at: Optional[datetime]
    quiet_for_seconds: Optional[int]
    #: Inbound calls (segments) in the window, and whether the latest was answered.
    inbound_calls: int
    window_hours: int
    #: The liveness component of the health score. Computed HERE so the
    #: scorer maps nothing: 1.0 alive, 0.5 alive-but-degraded (an on-demand
    #: service with an unanswered call among its latest), 0.0 silent, None
    #: when the state does
    #: not measure liveness (idle, unstarted; not_configured without
    #: activity in the window). A not-configured system that was active in
    #: the window still scores 1.0 — it demonstrably ran.
    score: Optional[float]
    #: The latest `success` run (work done), and the separate
    #: work-quiet signal (see the module docstring). Never part of `state`.
    last_processed_run_at: Optional[datetime] = None
    processed_quiet_for_seconds: Optional[int] = None
    max_quiet_processed_seconds: Optional[int] = None
    work_quiet: bool = False
    work_quiet_reason: Optional[str] = None

    @property
    def is_silent(self) -> bool:
        return self.state == STATE_SILENT

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for key in ("last_activity_at", "last_run_at", "last_processed_run_at"):
            d[key] = d[key].isoformat() if d[key] else None
        d["is_silent"] = self.is_silent
        return d


def liveness_states(
    db: Session,
    tenant_id: str,
    systems: Iterable[Any],
    *,
    window_hours: int = DEFAULT_LIVENESS_WINDOW_HOURS,
    now: Optional[datetime] = None,
) -> Dict[str, LivenessState]:
    """The liveness verdict for many systems, in a fixed number of queries.

    `systems` are ResourceGroup rows (anything with `.id` and
    `.system_kind`). The list uses this directly; single-system callers use
    `liveness_state`, which is this function over one system — so there is
    exactly one implementation.
    """
    systems = list(systems)
    if not systems:
        return {}
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)
    ids = [s.id for s in systems]

    expectations = {
        e.ai_system_id: e
        for e in db.query(AISystemLivenessExpectation).filter(
            AISystemLivenessExpectation.tenant_id == tenant_id,
            AISystemLivenessExpectation.ai_system_id.in_(ids),
        )
    }
    last_runs = dict(
        db.query(AISystemRun.ai_system_id, func.max(AISystemRun.started_at))
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id.in_(ids),
            AISystemRun.is_replay.is_(False),
        )
        .group_by(AISystemRun.ai_system_id)
        .all()
    )
    # The latest PROCESSED run: work done, not just activity.
    last_processed = dict(
        db.query(AISystemRun.ai_system_id, func.max(AISystemRun.started_at))
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id.in_(ids),
            AISystemRun.is_replay.is_(False),
            success_cond(AISystemRun),
        )
        .group_by(AISystemRun.ai_system_id)
        .all()
    )
    # Own runs in the window — "active in the window" for sporadic and the
    # not-configured score.
    runs_in_window = dict(
        db.query(AISystemRun.ai_system_id, func.count(AISystemRun.id))
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id.in_(ids),
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= window_start,
        )
        .group_by(AISystemRun.ai_system_id)
        .all()
    )
    last_segments = dict(
        db.query(
            AISystemRunSegment.segment_system_id,
            func.max(AISystemRunSegment.first_action_at),
        )
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.segment_system_id.in_(ids),
        )
        .group_by(AISystemRunSegment.segment_system_id)
        .all()
    )
    # Inbound calls in the window: the count (aggregated in SQL) and the
    # latest DOWN_AFTER_CONSECUTIVE_FAILURES outcomes per system, newest
    # first (a window function — never every segment of the window).
    # Only JUDGED calls feed the down rule: a call inside an
    # incident window (of the service, of the run's root, or tenant-wide)
    # is evidence, not a verdict. Last-activity times stay unfiltered —
    # recency is a fact, not a grade.
    in_window = (
        AISystemRunSegment.tenant_id == tenant_id,
        AISystemRunSegment.segment_system_id.in_(ids),
        AISystemRunSegment.first_action_at >= window_start,
        judged_segment(AISystemRunSegment, purpose=PURPOSE_RATE),
    )
    inbound_count: Dict[str, int] = dict(
        db.query(AISystemRunSegment.segment_system_id, func.count(AISystemRunSegment.id))
        .filter(*in_window)
        .group_by(AISystemRunSegment.segment_system_id)
        .all()
    )
    rn = func.row_number().over(
        partition_by=AISystemRunSegment.segment_system_id,
        order_by=(AISystemRunSegment.last_action_at.desc(), AISystemRunSegment.id.desc()),
    )
    latest = (
        db.query(
            AISystemRunSegment.segment_system_id.label("sid"),
            # A refused delegation reads as the refusal token.
            segment_call_outcome_sql(AISystemRunSegment).label("outcome"),
            rn.label("rn"),
        )
        .filter(*in_window)
        .subquery()
    )
    recent_outcomes: Dict[str, List[str]] = {}
    for sid, outcome in (
        db.query(latest.c.sid, latest.c.outcome)
        .filter(latest.c.rn <= DOWN_AFTER_CONSECUTIVE_FAILURES)
        .order_by(latest.c.sid, latest.c.rn)
        .all()
    ):
        recent_outcomes.setdefault(sid, []).append(outcome)

    out: Dict[str, LivenessState] = {}
    for system in systems:
        out[system.id] = _judge(
            system,
            expectation=expectations.get(system.id),
            last_run_at=_aware(last_runs.get(system.id)),
            last_segment_at=_aware(last_segments.get(system.id)),
            runs_in_window=int(runs_in_window.get(system.id, 0) or 0),
            inbound_calls=int(inbound_count.get(system.id, 0) or 0),
            recent_inbound_outcomes=recent_outcomes.get(system.id, []),
            window_hours=window_hours,
            now=now,
            last_processed_run_at=_aware(last_processed.get(system.id)),
        )
    return out


def liveness_state(
    db: Session,
    system: Any,
    *,
    window_hours: int = DEFAULT_LIVENESS_WINDOW_HOURS,
    now: Optional[datetime] = None,
) -> LivenessState:
    """The liveness verdict for one AI System. See the module docstring."""
    return liveness_states(
        db, system.tenant_id, [system], window_hours=window_hours, now=now
    )[system.id]


def _judge(
    system: Any,
    *,
    expectation: Optional[AISystemLivenessExpectation],
    last_run_at: Optional[datetime],
    last_segment_at: Optional[datetime],
    runs_in_window: int,
    inbound_calls: int,
    recent_inbound_outcomes: List[str],
    window_hours: int,
    now: datetime,
    last_processed_run_at: Optional[datetime] = None,
) -> LivenessState:
    verdict = _judge_activity(
        system,
        expectation=expectation,
        last_run_at=last_run_at,
        last_segment_at=last_segment_at,
        runs_in_window=runs_in_window,
        inbound_calls=inbound_calls,
        recent_inbound_outcomes=recent_inbound_outcomes,
        window_hours=window_hours,
        now=now,
    )
    _judge_work(verdict, expectation, last_run_at, last_processed_run_at, now)
    return verdict


def _judge_work(
    verdict: LivenessState,
    expectation: Optional[AISystemLivenessExpectation],
    last_run_at: Optional[datetime],
    last_processed_run_at: Optional[datetime],
    now: datetime,
) -> None:
    """The work-quiet signal, beside (never inside) the verdict."""
    verdict.last_processed_run_at = last_processed_run_at
    verdict.processed_quiet_for_seconds = (
        max(0, int((now - last_processed_run_at).total_seconds()))
        if last_processed_run_at is not None
        else None
    )
    bound = (
        getattr(expectation, "max_quiet_processed_seconds", None)
        if expectation is not None and expectation.enabled
        else None
    )
    verdict.max_quiet_processed_seconds = bound
    if not bound or last_run_at is None:
        return  # not checked, or never ran (unstarted is its own state)
    quiet = verdict.processed_quiet_for_seconds
    if quiet is None:
        verdict.work_quiet = True
        verdict.work_quiet_reason = (
            "Runs, but has never processed an outcome that counts as work."
        )
    elif quiet > bound:
        verdict.work_quiet = True
        verdict.work_quiet_reason = (
            f"Runs, but no processed outcome for {quiet}s "
            f"(expected at least one every {bound}s)."
        )


def _judge_activity(
    system: Any,
    *,
    expectation: Optional[AISystemLivenessExpectation],
    last_run_at: Optional[datetime],
    last_segment_at: Optional[datetime],
    runs_in_window: int,
    inbound_calls: int,
    recent_inbound_outcomes: List[str],
    window_hours: int,
    now: datetime,
) -> LivenessState:
    configured = expectation is not None
    mode = expectation.mode if configured else default_mode_for(system.system_kind)
    candidates = [t for t in (last_run_at, last_segment_at) if t is not None]
    last_activity = max(candidates) if candidates else None
    quiet = (
        max(0, int((now - last_activity).total_seconds()))
        if last_activity is not None
        else None
    )
    active_in_window = runs_in_window > 0 or inbound_calls > 0

    def make(state: str, reason: str, score: Optional[float]) -> LivenessState:
        return LivenessState(
            ai_system_id=system.id,
            state=state,
            reason=reason,
            mode=mode,
            configured=configured,
            last_activity_at=last_activity,
            last_run_at=last_run_at,
            quiet_for_seconds=quiet,
            inbound_calls=inbound_calls,
            window_hours=window_hours,
            score=score,
        )

    if configured and not expectation.enabled:
        return make(
            STATE_NOT_CONFIGURED,
            "The liveness expectation is disabled.",
            1.0 if active_in_window else None,
        )

    if mode == LIVENESS_ON_DEMAND:
        verdict = segment_calls_verdict(recent_inbound_outcomes)
        if verdict == CALLS_DOWN:
            return make(
                STATE_SILENT,
                f"The latest {DOWN_AFTER_CONSECUTIVE_FAILURES} inbound calls all went "
                f"unanswered ({', '.join(recent_inbound_outcomes)}).",
                VERDICT_SCORE[CALLS_DOWN],
            )
        if verdict == CALLS_DEGRADED:
            unanswered = [
                o for o in recent_inbound_outcomes
                if classify_segment_outcome(o) == CALL_UNANSWERED
            ]
            return make(
                STATE_ALIVE,
                f"Answering, but degraded: {len(unanswered)} of the latest "
                f"{len(recent_inbound_outcomes)} inbound calls went unanswered "
                f"({', '.join(unanswered)}).",
                VERDICT_SCORE[CALLS_DEGRADED],
            )
        if verdict is not None:
            return make(
                STATE_ALIVE,
                f"Answered its latest inbound calls ({inbound_calls} in the window).",
                VERDICT_SCORE[verdict],
            )
        if runs_in_window > 0:
            # An on-demand ROOT (an assistant) is called by people, not by
            # other systems: its own runs are its inbound calls.
            return make(STATE_ALIVE, f"{runs_in_window} run(s) in the window.", 1.0)
        return make(
            STATE_IDLE,
            "No inbound calls in the window — on-demand, so not a fault.",
            None,
        )

    if last_activity is None:
        return make(STATE_UNSTARTED, "Has never run.", None)

    if mode == LIVENESS_SPORADIC:
        if active_in_window:
            return make(STATE_ALIVE, "Active in the window.", 1.0)
        return make(
            STATE_IDLE,
            "Quiet in the window — sporadic, so not a fault.",
            None,
        )

    if mode in SILENCE_ALERTING_MODES:
        if not configured:
            return make(
                STATE_NOT_CONFIGURED,
                f"No liveness expectation is set (the {mode} default is shown, "
                f"not enforced).",
                1.0 if active_in_window else None,
            )
        if quiet is not None and quiet > expectation.deadline_seconds:
            return make(
                STATE_SILENT,
                f"Quiet for {quiet}s, beyond window + grace "
                f"({expectation.deadline_seconds}s).",
                0.0,
            )
        return make(STATE_ALIVE, "Within its expected cadence.", 1.0)

    # An unknown mode cannot be judged; say so rather than guess.
    return make(STATE_NOT_CONFIGURED, f"Unknown liveness mode {mode!r}.", None)


__all__ = [
    "DEFAULT_LIVENESS_WINDOW_HOURS",
    "LIVENESS_STATES",
    "LivenessState",
    "STATE_ALIVE",
    "STATE_IDLE",
    "STATE_NOT_CONFIGURED",
    "STATE_SILENT",
    "STATE_UNSTARTED",
    "liveness_state",
    "liveness_states",
    "segment_calls_verdict",
]

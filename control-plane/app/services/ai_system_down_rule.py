# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# ONE "down" rule (one fact, one computation)

Four surfaces judge whether calls to something are getting through:

  * **on-demand liveness** (`ai_system_liveness_state`) — is a service
    answering the calls made to it (all callers)?
  * **dependency health** (`ai_system_composition.CompositionIndex.
    dependency_status`, read by `ai_system_dependency_health`) — is a
    shared service answering THIS caller?
  * **connection status** (`ai_system_connection_status`) — is a connector
    line of the Composition tab carrying traffic?
  * **recent runs** (`ai_system_assurance._recent_runs`) — are the system's
    OWN latest finished runs succeeding? A 30-day
    reliability rate let a system whose every run had errored for three
    hours read "Healthy 90%". All failed ⇒ health state `failing`; some
    failed ⇒ reliability capped at the degraded score. Which runs are
    evidence, and which of them failed, is THE run classification
    (`ai_system_outcomes.down_rule_failed`): a `success` answers,
    a `deviation` or `failed` run does not, and a `no_work` run (an empty
    poll) or a `not_decisive` one is skipped — an empty poll that succeeds
    is not evidence the system works.

Earlier, the first two went down on ONE unanswered call (the latest)
while the connection line needed three in a row, so the same service could
read "not answering" on its dependency node and "degraded" on the line
drawn to it. This module is the only definition of the rule; all three
import it (guard: `tests/test_down_rule_single_definition.py`).

## The rule — `latest_calls_verdict`

Over the latest ``DOWN_AFTER_CONSECUTIVE_FAILURES`` (3) calls, newest first:

  * **down**     — all of them failed (and there were that many);
  * **degraded** — at least one of them failed;
  * **ok**       — none of them failed;
  * ``None``     — no calls: the caller decides what silence means (idle).

## One classification of a delegated call — `classify_segment_outcome`

Every surface classifies a run-segment outcome (a call into another AI
System) the same way:

  * **answered** — `answered`, and `rejected`: the delegate ANSWERED, it
    declined the task. Never an error;
  * **unanswered** — `failed`, `timed_out`: the delegate did not answer.

  * **blocked** — the GATEWAY refused the call (governance), not the
    delegate. There is no stored `blocked` segment outcome: a
    refused delegation is stored as `failed` with segment `error_cause =
    blocked_by_policy` (also a segment entered only by calls held for
    approval). `segment_call_outcome` / `segment_call_outcome_sql` read that
    pair back as the refusal token `blocked` — every reader goes through
    them, so a held or refused delegation never makes the delegate look down
    (the core's segment rules).

"Failed" for the rule is then: unanswered, for liveness and dependency
health (a gateway refusal says nothing about whether the delegate answers);
unanswered or blocked, for a connection line (a refusal stops the
communication just as surely — and on proxy_logs rows the gateway's
refusals, `DENY_SUBTYPES`, guardrail blocks and 429s, are blocked too). A
down health signal (disabled, retired, circuit breaker open …) is down
regardless — that is the connection-status caller's input, not a count.

A single failure therefore degrades, never downs: one timeout is a
reliability fact, three in a row is an outage. The failure keeps the
verdict degraded until three good calls in a row have pushed it out of the
window — recovery must be demonstrated too.

## The window rate — connection lines only

`DEGRADED_FAILURE_RATE` degrades a connection line whose (errors + blocked)
/ calls over the whole composition window reaches 5 %. It is a traffic-share
fact that only the line has the totals for; liveness and dependency health
judge *now*, and a service's window-wide failure share is its reliability
signal. It lives here so there is one place every threshold of the rule is
defined.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from sqlalchemy import and_, case

from app.constants.outcomes import CAUSE_BLOCKED_BY_POLICY
from app.database.models.ai_system_run_segment import (
    SEGMENT_ANSWERED,
    SEGMENT_FAILED,
    SEGMENT_REJECTED,
    SEGMENT_TIMED_OUT,
)

#: The latest this-many calls ALL failed ⇒ down.
DOWN_AFTER_CONSECUTIVE_FAILURES = 3
#: Connection lines: (errors + blocked) / calls at or above this in the
#: window ⇒ degraded.
DEGRADED_FAILURE_RATE = 0.05

CALLS_OK = "ok"
CALLS_DEGRADED = "degraded"
CALLS_DOWN = "down"
CALL_VERDICTS = (CALLS_OK, CALLS_DEGRADED, CALLS_DOWN)

#: The liveness component score of each verdict (the health scorer's
#: degraded threshold is 0.8, so degraded scores below it and above down).
VERDICT_SCORE: Dict[str, float] = {
    CALLS_OK: 1.0,
    CALLS_DEGRADED: 0.5,
    CALLS_DOWN: 0.0,
}


CALL_ANSWERED = "answered"
CALL_UNANSWERED = "unanswered"
CALL_BLOCKED = "blocked"

#: The call token a refused delegation reads as (`segment_call_outcome`).
#: Never stored: the segment row says `failed` + `blocked_by_policy`.
SEGMENT_REFUSED = "blocked"

#: THE classification of a run-segment outcome (see the module docstring).
SEGMENT_OUTCOME_CLASS: Dict[str, str] = {
    SEGMENT_ANSWERED: CALL_ANSWERED,
    SEGMENT_REJECTED: CALL_ANSWERED,
    SEGMENT_FAILED: CALL_UNANSWERED,
    SEGMENT_TIMED_OUT: CALL_UNANSWERED,
    SEGMENT_REFUSED: CALL_BLOCKED,
}


def segment_call_outcome(outcome: str, error_cause: str = None) -> str:
    """The call token of one stored segment: its outcome, except a gateway
    refusal (`failed` + `blocked_by_policy`), which reads `blocked`."""
    if outcome == SEGMENT_FAILED and error_cause == CAUSE_BLOCKED_BY_POLICY:
        return SEGMENT_REFUSED
    return outcome


def segment_call_outcome_sql(model):
    """`segment_call_outcome` as a SQL expression over a segment model."""
    return case(
        (
            and_(model.outcome == SEGMENT_FAILED, model.error_cause == CAUSE_BLOCKED_BY_POLICY),
            SEGMENT_REFUSED,
        ),
        else_=model.outcome,
    )


def classify_segment_outcome(outcome: str) -> str:
    """answered | unanswered | blocked for one delegated call (a call token —
    see `segment_call_outcome`). An outcome this module does not know is
    unanswered — never silently an answer."""
    return SEGMENT_OUTCOME_CLASS.get(outcome, CALL_UNANSWERED)


def latest_calls_verdict(failed_newest_first: Iterable[bool]) -> Optional[str]:
    """THE rule. `failed_newest_first`: one bool per call, newest first
    (only the first ``DOWN_AFTER_CONSECUTIVE_FAILURES`` are read). Returns
    ``ok`` | ``degraded`` | ``down``, or ``None`` when there were no calls."""
    latest = []
    for failed in failed_newest_first:
        latest.append(bool(failed))
        if len(latest) == DOWN_AFTER_CONSECUTIVE_FAILURES:
            break
    if not latest:
        return None
    n_failed = sum(latest)
    if n_failed == DOWN_AFTER_CONSECUTIVE_FAILURES:
        return CALLS_DOWN
    if n_failed:
        return CALLS_DEGRADED
    return CALLS_OK


def failure_rate_degraded(calls: int, failed: int) -> bool:
    """Connection lines: the window failure share reached the threshold."""
    return calls > 0 and (failed / calls) >= DEGRADED_FAILURE_RATE


__all__ = [
    "CALL_ANSWERED",
    "CALL_BLOCKED",
    "CALL_UNANSWERED",
    "SEGMENT_REFUSED",
    "segment_call_outcome",
    "segment_call_outcome_sql",
    "SEGMENT_OUTCOME_CLASS",
    "classify_segment_outcome",
    "CALLS_DEGRADED",
    "CALLS_DOWN",
    "CALLS_OK",
    "CALL_VERDICTS",
    "DEGRADED_FAILURE_RATE",
    "DOWN_AFTER_CONSECUTIVE_FAILURES",
    "VERDICT_SCORE",
    "failure_rate_degraded",
    "latest_calls_verdict",
]

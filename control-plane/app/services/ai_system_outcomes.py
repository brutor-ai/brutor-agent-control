# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# THE run classification (one fact, one computation)

A finished run is judged on three questions answered by three authors
— the gateway's execution verdict (`terminal_state`), the
gateway's check of the agent's outcome against the contract
(`outcome_conformance`) and the contract's word on whether that outcome is
work (`outcome_counts_toward_baseline`). This module is the ONLY place the
three are combined:

    terminal_state ∈ {errored, exhausted}                     → failed
    terminal_state ∈ {abandoned, cancelled}                   → not_decisive
    a terminal state this code does not know                  → failed
        (never silently not-decisive; the core's rule too — open runs,
        NULL, are never stored but read not_decisive)
    completed / completed_degraded, conformance = declared:
        counts toward the baseline                            → success
        does not                                              → no_work
    completed / completed_degraded otherwise
        (undeclared, unreported — or NULL on a finalized run)  → deviation

and from that, once:

  * **success rate** = success / (success + deviation + failed); None when
    the denominator is 0. `no_work` (empty polls) and `not_decisive` are in
    neither — twenty empty polls do not make an agent reliable, and during
    an outage they were exactly what kept it reading healthy.
  * **the down rule's evidence** (`ai_system_down_rule`): success answers;
    deviation and failed do not; no_work and not_decisive are skipped.
  * **the work population** (cost per processed outcome, run length,
    minimum evidence, baselines): success runs. failed + deviation spend is
    *spent, nothing produced*; no_work is *no-work overhead*.

Health, run signals, the envelope, the assurance report, tokenomics,
dependency health and the run API all import it. A static guard
(`tests/test_run_success_single_definition.py`) fails if any other module in
`app/` decides success from `terminal_state`. The Rust core mirrors it in
`src/services/run_outcome.rs`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from sqlalchemy import and_, case, false, func, or_, true

from app.constants.outcomes import (
    CONFORMANCE_DECLARED,
    CONFORMANCE_UNDECLARED,
    CONFORMANCE_UNREPORTED,
    OUTCOME_CONFORMANCES,
    RUN_CLASSES,
    RUN_DEVIATION,
    RUN_FAILED,
    RUN_NOT_DECISIVE,
    RUN_NO_WORK,
    RUN_SUCCESS,
)
from app.database.models.ai_system_run import (
    SUCCESSFUL_TERMINAL_STATES,
    TERMINAL_ABANDONED,
    TERMINAL_CANCELLED,
    TERMINAL_ERRORED,
    TERMINAL_EXHAUSTED,
)

#: Execution failed (question 1): the run carries an `error_cause`.
FAILED_TERMINAL_STATES = frozenset({TERMINAL_ERRORED, TERMINAL_EXHAUSTED})
#: Ended without a verdict on whether the system works: a client that went
#: quiet, a run somebody stopped.
NOT_DECISIVE_TERMINAL_STATES = frozenset({TERMINAL_ABANDONED, TERMINAL_CANCELLED})
#: Completed without errors (the execution axis only).
COMPLETED_TERMINAL_STATES = SUCCESSFUL_TERMINAL_STATES

#: The run classes in the success-rate denominator.
RATE_CLASSES = (RUN_SUCCESS, RUN_DEVIATION, RUN_FAILED)
#: The run classes the down rule reads, and whether each is a failure.
DOWN_RULE_FAILED: Dict[str, bool] = {
    RUN_SUCCESS: False,
    RUN_DEVIATION: True,
    RUN_FAILED: True,
}
#: Cost that bought nothing: errored / exhausted runs and deviating ones.
WASTE_CLASSES = (RUN_FAILED, RUN_DEVIATION)


# ---------------------------------------------------------------------------
# The classification — Python
# ---------------------------------------------------------------------------


def run_class(
    terminal_state: Optional[str],
    outcome_conformance: Optional[str],
    counts_toward_baseline: Optional[bool],
) -> str:
    """THE classification of one finished run (see the module docstring)."""
    if terminal_state in COMPLETED_TERMINAL_STATES:
        if outcome_conformance == CONFORMANCE_DECLARED:
            return RUN_SUCCESS if counts_toward_baseline else RUN_NO_WORK
        return RUN_DEVIATION
    if not terminal_state or terminal_state in NOT_DECISIVE_TERMINAL_STATES:
        return RUN_NOT_DECISIVE
    # errored / exhausted — and a state this code does not know, which
    # counts against the system rather than vanishing (core: the same).
    return RUN_FAILED


def run_class_of(run: Any) -> str:
    """`run_class` over anything with the three run attributes (an
    `AISystemRun`, a row, a SimpleNamespace)."""
    return run_class(
        getattr(run, "terminal_state", None),
        getattr(run, "outcome_conformance", None),
        getattr(run, "outcome_counts_toward_baseline", None),
    )


def is_success(cls: str) -> bool:
    return cls == RUN_SUCCESS


def in_rate(cls: str) -> bool:
    """Whether a run of this class is in the success-rate denominator."""
    return cls in RATE_CLASSES


def down_rule_failed(cls: str) -> Optional[bool]:
    """For the down rule: True (counts as a failure), False (an answer), or
    None — not evidence (no_work, not_decisive), skipped."""
    return DOWN_RULE_FAILED.get(cls)


def success_rate(counts: Mapping[str, int]) -> Optional[float]:
    """success / (success + deviation + failed) over `{run_class: count}`;
    None when the denominator is 0 (no decisive work is not 0%)."""
    denominator = sum(int(counts.get(c, 0) or 0) for c in RATE_CLASSES)
    if denominator <= 0:
        return None
    return int(counts.get(RUN_SUCCESS, 0) or 0) / denominator


def class_counts(classes: Iterable[str]) -> Dict[str, int]:
    """{run_class: count} with every class present (zeros included)."""
    out = {c: 0 for c in RUN_CLASSES}
    for c in classes:
        out[c] = out.get(c, 0) + 1
    return out


# ---------------------------------------------------------------------------
# The classification — SQL (the same table, for aggregation in the database)
# ---------------------------------------------------------------------------


#: Not decisive in SQL: abandoned, cancelled, and NULL (coalesced to "").
_NOT_DECISIVE_SQL = sorted(NOT_DECISIVE_TERMINAL_STATES) + [""]


def _cols(model: Any) -> Tuple[Any, Any, Any]:
    """NULL-safe forms of the three columns. Three-valued logic would make
    `NOT (deviation)` drop a row whose conformance is NULL — every condition
    here is a plain true / false on every row, so negations and partitions
    stay exact."""
    return (
        func.coalesce(model.terminal_state, ""),
        func.coalesce(model.outcome_conformance, ""),
        func.coalesce(model.outcome_counts_toward_baseline, false()),
    )


def run_class_sql(model: Any = None):
    """A CASE expression yielding the run class of each row of `model`
    (default `AISystemRun`). Identical to `run_class` row by row (pinned by
    tests/test_ai_system_outcomes.py over every cell)."""
    state, conformance, counts = _cols(model or _run_model())
    completed = state.in_(sorted(COMPLETED_TERMINAL_STATES))
    declared = conformance == CONFORMANCE_DECLARED
    return case(
        (and_(completed, declared, counts == true()), RUN_SUCCESS),
        (and_(completed, declared), RUN_NO_WORK),
        (completed, RUN_DEVIATION),
        (state.in_(_NOT_DECISIVE_SQL), RUN_NOT_DECISIVE),
        else_=RUN_FAILED,
    )


def class_cond(cls: str, model: Any = None):
    """A WHERE condition true for the rows of one run class (never NULL)."""
    state, conformance, counts = _cols(model or _run_model())
    completed = state.in_(sorted(COMPLETED_TERMINAL_STATES))
    declared = conformance == CONFORMANCE_DECLARED
    if cls == RUN_FAILED:
        return state.notin_(sorted(COMPLETED_TERMINAL_STATES) + _NOT_DECISIVE_SQL)
    if cls == RUN_SUCCESS:
        return and_(completed, declared, counts == true())
    if cls == RUN_NO_WORK:
        return and_(completed, declared, counts == false())
    if cls == RUN_DEVIATION:
        return and_(completed, conformance != CONFORMANCE_DECLARED)
    if cls == RUN_NOT_DECISIVE:
        return state.in_(_NOT_DECISIVE_SQL)
    raise ValueError(f"unknown run class {cls!r}")


def classes_cond(classes: Iterable[str], model: Any = None):
    """OR of `class_cond` over several classes."""
    return or_(*[class_cond(c, model) for c in classes])


def success_cond(model: Any = None):
    return class_cond(RUN_SUCCESS, model)


def rate_cond(model: Any = None):
    """Rows in the success-rate denominator (also: the down rule's evidence)."""
    return classes_cond(RATE_CLASSES, model)


def waste_cond(model: Any = None):
    return classes_cond(WASTE_CLASSES, model)


def _run_model():
    from app.database.models.ai_system_run import AISystemRun

    return AISystemRun


# ---------------------------------------------------------------------------
# Outcome ids
# ---------------------------------------------------------------------------


def split_outcome_id(outcome_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """`"a.b"` → ("a", "b"); `"a"` → ("a", None); None → (None, None)."""
    if not outcome_id:
        return None, None
    outcome, _, variant = outcome_id.partition(".")
    return outcome, (variant or None)


def outcome_of(outcome_id: Optional[str]) -> Optional[str]:
    """The outcome part of a full id (the `<outcome>` of `<outcome>.<variant>`)."""
    return split_outcome_id(outcome_id)[0]


__all__ = [
    "COMPLETED_TERMINAL_STATES",
    "CONFORMANCE_DECLARED",
    "CONFORMANCE_UNDECLARED",
    "CONFORMANCE_UNREPORTED",
    "DOWN_RULE_FAILED",
    "FAILED_TERMINAL_STATES",
    "NOT_DECISIVE_TERMINAL_STATES",
    "OUTCOME_CONFORMANCES",
    "RATE_CLASSES",
    "RUN_CLASSES",
    "RUN_DEVIATION",
    "RUN_FAILED",
    "RUN_NOT_DECISIVE",
    "RUN_NO_WORK",
    "RUN_SUCCESS",
    "WASTE_CLASSES",
    "class_cond",
    "class_counts",
    "classes_cond",
    "down_rule_failed",
    "in_rate",
    "is_success",
    "outcome_of",
    "rate_cond",
    "run_class",
    "run_class_of",
    "run_class_sql",
    "split_outcome_id",
    "success_cond",
    "success_rate",
    "waste_cond",
]

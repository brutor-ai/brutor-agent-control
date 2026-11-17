# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Building a replay suite

## Why "the last 50 runs" is the wrong suite

It is the obvious implementation and it gates nothing. Recent traffic
encodes whatever the system happened to be doing on Tuesday: mostly
happy-path completions, one or two tool paths, a narrow band of step
counts. The cases that a config change actually breaks — the exhausted
runs, the rare tool the finance team uses twice a week, the long
trajectories that brush the iteration cap — are precisely the ones that
window misses.

A suite built that way passes at 100% right up until it matters.

## The algorithm: rarest stratum first

Each run belongs to three overlapping strata: its terminal state, its
step-count decile, and its tool signature. Strata are visited **smallest
first**, taking one unpicked run from each in turn until the budget runs
out.

Rarity-first is the whole trick. A proportional sample of 30 from a
population that is 92% `completed` gives you 27 happy-path cases and
maybe one `exhausted`; visiting the one-run `exhausted` stratum first
guarantees it survives, and the common strata still fill the remainder
because there is nothing else left to take.

Every case records **which stratum caused it to be picked**, so suite
coverage is inspectable. A suite that turns out to be thirty variations
on the same happy path is a suite that gates nothing, and without the
stratum label nobody would ever notice.

## Determinism

Selection is a pure function of the input order. Suites are evidence — an
operator who rebuilds one and gets a different set cannot compare the two
runs, and a flaky test suite for a *gating* mechanism is worse than none.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

#: How many step-count buckets. Deciles rather than quartiles because the
#: interesting tail — runs approaching the iteration cap — lives in the top
#: 10%, and a quartile would bury it with three quarters of ordinary runs.
STEP_DECILES = 10

#: How far above the population's typical step count a run must sit to earn
#: its own stratum. In robust units (median + k x MAD), matching the drift
#: module — deciles alone cannot isolate an outlier when the population has
#: near-zero variance, and "150 runs at 3 steps plus one at 90" is exactly
#: the shape where the outlier is the interesting case.
OUTLIER_MAD_MULTIPLIER = 6.0

#: Tool signatures are unbounded in cardinality; a system with 200 distinct
#: tool paths would otherwise produce 200 single-run strata and crowd out
#: every other dimension. Beyond this many, the rarest are kept.
MAX_TOOL_STRATA = 12


@dataclass
class RunRecord:
    """The facts sampling needs about one recorded run."""

    run_id: str
    root_task_id: str
    terminal_state: Optional[str] = None
    action_count: Optional[int] = None
    tool_set_signature: Optional[str] = None
    total_cost_usd: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SelectedCase:
    run: RunRecord
    #: The stratum that caused this run to be picked — not every stratum it
    #: belongs to. Coverage is about what the sample was *reaching for*.
    stratum: str


def _tool_key(signature: Optional[str]) -> str:
    if not signature:
        return "tools:none"
    # Hashed: signatures can be long and are used only for grouping.
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10]
    return f"tools:{digest}"


def action_deciles(action_counts: Sequence[int]) -> List[int]:
    """Nearest-rank decile boundaries over the population.

    Nearest-rank rather than interpolated, matching the drift module: an
    interpolated boundary can land on a step count no run actually had,
    which makes the bucket labels unexplainable to an operator.
    """
    values = sorted(v for v in action_counts if v is not None)
    if not values:
        return []
    bounds = []
    for i in range(1, STEP_DECILES):
        rank = max(0, min(len(values) - 1, (i * len(values)) // STEP_DECILES))
        bounds.append(values[rank])
    return bounds


def _median(sorted_values: Sequence[float]) -> Optional[float]:
    if not sorted_values:
        return None
    mid = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return float(sorted_values[mid])
    return (sorted_values[mid - 1] + sorted_values[mid]) / 2.0


def outlier_threshold(action_counts: Sequence[int]) -> Optional[float]:
    """Above which a step count is an outlier worth its own stratum.

    Median + k x MAD rather than a percentile. A decile over a degenerate
    distribution — 150 runs at 3 steps and one at 90 — puts every run in the
    same bucket including the interesting one, so the run that actually
    stresses the iteration cap never gets sampled.

    A zero MAD (every run identical) falls back to "strictly above the
    median", which is the only sensible reading of "unusual" there.
    """
    values = sorted(float(v) for v in action_counts if v is not None)
    med = _median(values)
    if med is None:
        return None
    deviations = sorted(abs(v - med) for v in values)
    mad = _median(deviations) or 0.0
    if mad <= 0.0:
        return med
    return med + OUTLIER_MAD_MULTIPLIER * mad


def _decile_key(action_count: Optional[int], bounds: Sequence[int]) -> str:
    if action_count is None:
        return "actions:unknown"
    idx = 0
    for b in bounds:
        if action_count >= b:
            idx += 1
    return f"actions:d{idx}"


def build_strata(runs: Sequence[RunRecord]) -> Dict[str, List[RunRecord]]:
    """Map every stratum to the runs in it. A run appears in three."""
    bounds = action_deciles([r.action_count for r in runs if r.action_count is not None])

    cutoff = outlier_threshold([r.action_count for r in runs if r.action_count is not None])

    strata: Dict[str, List[RunRecord]] = {}

    def add(key: str, run: RunRecord) -> None:
        strata.setdefault(key, []).append(run)

    tool_counts: Dict[str, int] = {}
    for run in runs:
        add(f"terminal:{run.terminal_state or 'unknown'}", run)
        add(_decile_key(run.action_count, bounds), run)
        if (
            cutoff is not None
            and run.action_count is not None
            and run.action_count > cutoff
        ):
            add("actions:outlier", run)
        tk = _tool_key(run.tool_set_signature)
        tool_counts[tk] = tool_counts.get(tk, 0) + 1

    # Keep the rarest tool paths: they are the ones a change is most likely
    # to break and the least likely to be sampled by accident.
    kept_tools = {
        k for k, _ in sorted(tool_counts.items(), key=lambda kv: (kv[1], kv[0]))[:MAX_TOOL_STRATA]
    }
    for run in runs:
        tk = _tool_key(run.tool_set_signature)
        if tk in kept_tools:
            add(tk, run)

    return strata


def select_cases(runs: Sequence[RunRecord], target: int) -> List[SelectedCase]:
    """Pick up to `target` runs, rarest stratum first.

    Deterministic given the input order.
    """
    if target <= 0 or not runs:
        return []

    strata = build_strata(runs)
    # Smallest strata first; ties broken by name so the result is stable.
    order = sorted(strata.items(), key=lambda kv: (len(kv[1]), kv[0]))

    picked: Dict[str, SelectedCase] = {}
    exhausted: set = set()

    # Round-robin: one pass takes at most one run from each stratum, so a
    # large stratum cannot swallow the budget before the rare ones are
    # visited.
    while len(picked) < target and len(exhausted) < len(order):
        progressed = False
        for key, members in order:
            if len(picked) >= target:
                break
            if key in exhausted:
                continue
            nxt = next((r for r in members if r.run_id not in picked), None)
            if nxt is None:
                exhausted.add(key)
                continue
            picked[nxt.run_id] = SelectedCase(run=nxt, stratum=key)
            progressed = True
        if not progressed:
            break

    # Stable output order: by stratum then run id, so two builds of the same
    # population produce byte-identical suites.
    return sorted(picked.values(), key=lambda c: (c.stratum, c.run.run_id))


def coverage(cases: Sequence[SelectedCase]) -> Dict[str, int]:
    """How many cases came from each stratum — the inspectable half."""
    out: Dict[str, int] = {}
    for c in cases:
        out[c.stratum] = out.get(c.stratum, 0) + 1
    return dict(sorted(out.items()))


def coverage_gaps(
    runs: Sequence[RunRecord], cases: Sequence[SelectedCase]
) -> List[str]:
    """Strata present in the population but absent from the sample.

    Reported rather than silently tolerated: a gate is only as good as what
    its evidence covers, and "30 cases, none of them the exhausted path" is
    a fact an operator should see before relying on the pass rate.
    """
    present = {c.stratum for c in cases}
    return sorted(k for k in build_strata(runs) if k not in present)


def trajectory_of(run: RunRecord) -> Dict[str, Any]:
    """The recorded shape a replay is diffed against."""
    return {
        "terminal_state": run.terminal_state,
        "action_count": run.action_count,
        "tool_set_signature": run.tool_set_signature,
        "total_cost_usd": run.total_cost_usd,
        **{k: v for k, v in run.extra.items() if k != "request"},
    }


__all__ = [
    "MAX_TOOL_STRATA",
    "OUTLIER_MAD_MULTIPLIER",
    "outlier_threshold",
    "STEP_DECILES",
    "RunRecord",
    "SelectedCase",
    "build_strata",
    "coverage",
    "coverage_gaps",
    "select_cases",
    "action_deciles",
    "trajectory_of",
]

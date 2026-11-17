# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Learning an AI System's cadence

Nobody fills in a cadence form voluntarily. Ask an operator to specify
"expect at least 4 runs every 3600 seconds, with 300 seconds of grace" for
each of forty AI Systems and the honest outcome is that liveness gets
configured for none of them — which is the same as not shipping it.

So the platform watches, proposes, and lets the operator accept in one
click.

## How the window is sized

From the **p95** inter-run gap, not the median. A window sized at the
median alerts on half of all normal quiet periods by construction; the p95
tolerates ordinary variation and still catches a system that has genuinely
stopped. Grace on top absorbs the rest.

This is the single most important decision in the module: get it wrong and
the feature pages people about nothing, which is the
critical risk to the whole feature.

## What it refuses to do

Propose from too little history. A cadence learned from three runs is not
a rhythm, and a confident-looking expectation derived from noise is worse
than no expectation at all — the operator accepts it, it fires, and they
stop trusting the category.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.database.models.ai_system_liveness import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_LIVENESS_MODE,
    DEFAULT_MODE_BY_SYSTEM_KIND,
    LIVENESS_CONTINUOUS,
    LIVENESS_ON_DEMAND,
    LIVENESS_SCHEDULED,
    MIN_RUNS_TO_LEARN_CADENCE,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import ResourceGroup

# How far back to look when learning. Long enough to see a weekly rhythm,
# short enough that a system's behaviour six months ago does not define
# what "normal" means today.
LEARNING_LOOKBACK_DAYS = 30

# Windows are rounded to something an operator recognises. "Every 3847
# seconds" reads as a machine guessing; "every hour" reads as a decision.
FRIENDLY_WINDOWS = (
    300,      # 5m
    900,      # 15m
    1800,     # 30m
    3600,     # 1h
    7200,     # 2h
    21600,    # 6h
    43200,    # 12h
    86400,    # 1d
    172800,   # 2d
    604800,   # 7d
)

# Never propose a window tighter than this: below it, ordinary scheduling
# jitter alone will trip the detector.
MIN_PROPOSED_WINDOW_SECONDS = 300


class CadenceProposal:
    """What the platform thinks an AI System's expectation should be."""

    def __init__(
        self,
        *,
        mode: str,
        window_seconds: int,
        grace_seconds: int,
        expected_min_runs: int,
        learned_from_runs: int,
        median_gap_seconds: Optional[int],
        p95_gap_seconds: Optional[int],
        confident: bool,
        rationale: str,
    ):
        self.mode = mode
        self.window_seconds = window_seconds
        self.grace_seconds = grace_seconds
        self.expected_min_runs = expected_min_runs
        self.learned_from_runs = learned_from_runs
        self.median_gap_seconds = median_gap_seconds
        self.p95_gap_seconds = p95_gap_seconds
        self.confident = confident
        self.rationale = rationale

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "window_seconds": self.window_seconds,
            "grace_seconds": self.grace_seconds,
            "expected_min_runs": self.expected_min_runs,
            "learned_from_runs": self.learned_from_runs,
            "median_gap_seconds": self.median_gap_seconds,
            "p95_gap_seconds": self.p95_gap_seconds,
            "confident": self.confident,
            "rationale": self.rationale,
        }


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile — the value actually observed, not one
    interpolated between two."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, min(len(ordered), int(round(pct * len(ordered) + 0.5))))
    return ordered[rank - 1]


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _round_up_to_friendly(seconds: float) -> int:
    """Round up to a window an operator recognises."""
    for w in FRIENDLY_WINDOWS:
        if seconds <= w:
            return w
    return FRIENDLY_WINDOWS[-1]


def default_mode_for(system_kind: Optional[str]) -> str:
    """The mode to start from, derived from what kind of system this is.

    An `assistant` defaults to `on_demand` deliberately: humans are bursty
    and go on holiday, and paging someone about a quiet Sunday afternoon on
    a chat assistant is exactly how an alert category gets muted.
    """
    if not system_kind:
        return DEFAULT_LIVENESS_MODE
    return DEFAULT_MODE_BY_SYSTEM_KIND.get(system_kind, DEFAULT_LIVENESS_MODE)


class LivenessLearningService:
    """Proposes a liveness expectation from observed behaviour."""

    def propose(
        self,
        db: Session,
        tenant_id: str,
        ai_system_id: str,
        *,
        lookback_days: int = LEARNING_LOOKBACK_DAYS,
    ) -> CadenceProposal:
        system = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.id == ai_system_id,
                ResourceGroup.tenant_id == tenant_id,
            )
            .first()
        )
        kind = system.system_kind if system else None
        mode = default_mode_for(kind)

        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        starts = [
            r.started_at
            for r in (
                db.query(AISystemRun)
                .filter(
                    AISystemRun.tenant_id == tenant_id,
                    AISystemRun.ai_system_id == ai_system_id,
                    AISystemRun.started_at >= since,
                    AISystemRun.is_replay.is_(False),
                )
                .order_by(AISystemRun.started_at.asc())
                .all()
            )
            if r.started_at is not None
        ]

        # Not enough history to have a rhythm. Return the kind-derived
        # default, flagged unconfident — an expectation invented from three
        # runs will fire, the operator will stop trusting the category, and
        # the feature dies of exactly that cause.
        if len(starts) < MIN_RUNS_TO_LEARN_CADENCE:
            return CadenceProposal(
                mode=mode,
                window_seconds=FRIENDLY_WINDOWS[3],  # 1h
                grace_seconds=DEFAULT_GRACE_SECONDS,
                expected_min_runs=1,
                learned_from_runs=len(starts),
                median_gap_seconds=None,
                p95_gap_seconds=None,
                confident=False,
                rationale=(
                    f"Only {len(starts)} run(s) in the last {lookback_days} days — "
                    f"at least {MIN_RUNS_TO_LEARN_CADENCE} are needed before a "
                    f"cadence means anything. Showing the default for a "
                    f"'{kind or 'unknown'}' system instead."
                ),
            )

        gaps = [
            (starts[i] - starts[i - 1]).total_seconds() for i in range(1, len(starts))
        ]
        gaps = [g for g in gaps if g > 0]

        median_gap = _median(gaps)
        p95_gap = _percentile(gaps, 0.95)

        if not p95_gap:
            # Every run at the same instant — a burst import, not a cadence.
            return CadenceProposal(
                mode=mode,
                window_seconds=FRIENDLY_WINDOWS[3],
                grace_seconds=DEFAULT_GRACE_SECONDS,
                expected_min_runs=1,
                learned_from_runs=len(starts),
                median_gap_seconds=None,
                p95_gap_seconds=None,
                confident=False,
                rationale=(
                    "All runs share a timestamp — this looks like a bulk import "
                    "rather than a cadence."
                ),
            )

        # Size from the p95 gap: a window at the median alerts on half of
        # all normal quiet periods by construction.
        window = max(
            MIN_PROPOSED_WINDOW_SECONDS, _round_up_to_friendly(p95_gap)
        )

        # Grace scales with the window — 10%, floored at the default. A
        # daily job deserves more slack than a per-minute one.
        grace = max(DEFAULT_GRACE_SECONDS, int(window * 0.1))

        # For a continuous system, expect roughly the observed rate, with
        # generous headroom so a normal quiet hour is not a collapse.
        if mode == LIVENESS_CONTINUOUS and median_gap:
            observed_per_window = window / median_gap
            expected_min_runs = max(1, int(observed_per_window * 0.5))
        else:
            expected_min_runs = 1

        return CadenceProposal(
            mode=mode,
            window_seconds=window,
            grace_seconds=grace,
            expected_min_runs=expected_min_runs,
            learned_from_runs=len(starts),
            median_gap_seconds=int(median_gap) if median_gap else None,
            p95_gap_seconds=int(p95_gap),
            confident=True,
            rationale=(
                f"Learned from {len(starts)} runs over {lookback_days} days. "
                f"Typical gap between runs {_humanize(median_gap)}, "
                f"95th percentile {_humanize(p95_gap)}. The window is sized from "
                f"the 95th percentile so ordinary quiet periods do not raise an "
                f"alert."
            ),
        )


def _humanize(seconds: Optional[float]) -> str:
    if not seconds:
        return "unknown"
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    if seconds >= 60:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds:.0f} seconds"


liveness_learning_service = LivenessLearningService()

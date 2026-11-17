# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System liveness expectations

The cheapest gap in the platform to close, and the one customers describe
most viscerally: *"we found out on Thursday it had been dead since
Monday."*

Every dashboard the platform had before this was a function of traffic
that arrived. An AI System that stops calling is therefore invisible to
all of them — its error rate is fine, its cost is fine, its latency is
fine, because there is nothing to be wrong. Liveness is the only signal
that fires on the **absence** of traffic, which is why it cannot be
computed on a request path and must be swept for.

## What an expectation says

One row per AI System, declaring the cadence its owner expects:

  * `continuous` — at least `expected_min_runs` per `window_seconds`.
  * `scheduled` — at least one run per `window_seconds` (a nightly job is
    `window_seconds = 86400`).
  * `sporadic` — never alert on silence. For genuinely irregular systems
    (a monthly batch, an incident-response agent) where quiet is normal
    and only a hard-fail cliff is worth waking someone for.
  * `on_demand` — no liveness expectation at all, stated explicitly.

`on_demand` exists so "we do not monitor this" is a *decision on the
record* rather than an empty row that might equally mean nobody got round
to it.

## Why cadence is learned, not typed

Nobody fills in a cadence form voluntarily. After a system has run enough
to have a rhythm, the platform proposes an expectation from the observed
inter-run gaps and the operator accepts it in one click. The learned
values are kept alongside the accepted ones so an operator can see what
the system actually does versus what they said it should do.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, String,
    UniqueConstraint,
)
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.database import Base


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

LIVENESS_CONTINUOUS = "continuous"
LIVENESS_SCHEDULED = "scheduled"
LIVENESS_SPORADIC = "sporadic"
LIVENESS_ON_DEMAND = "on_demand"

LIVENESS_MODES = frozenset({
    LIVENESS_CONTINUOUS,
    LIVENESS_SCHEDULED,
    LIVENESS_SPORADIC,
    LIVENESS_ON_DEMAND,
})

# Modes for which going quiet is itself a finding. `sporadic` and
# `on_demand` are deliberately absent: for those, silence is the expected
# state and alerting on it would train operators to ignore the category.
SILENCE_ALERTING_MODES = frozenset({LIVENESS_CONTINUOUS, LIVENESS_SCHEDULED})

# Default mode proposed per `system_kind`. An integration
# has a schedule; an application should be continuously exercised; an
# assistant is driven by humans, who are bursty and go on holiday — paging
# on an assistant's quiet Sunday is how a category gets muted.
DEFAULT_MODE_BY_SYSTEM_KIND = {
    "agent": LIVENESS_CONTINUOUS,
    "assistant": LIVENESS_ON_DEMAND,
    "application": LIVENESS_CONTINUOUS,
    "integration": LIVENESS_SCHEDULED,
    "workflow": LIVENESS_SCHEDULED,
    "service": LIVENESS_CONTINUOUS,
}

# Fallback when the system has no kind (should not happen post-a130).
DEFAULT_LIVENESS_MODE = LIVENESS_ON_DEMAND

DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_GRACE_SECONDS = 300
DEFAULT_EXPECTED_MIN_RUNS = 1

# A cadence learned from fewer runs than this is noise, not a rhythm.
MIN_RUNS_TO_LEARN_CADENCE = 10


# ---------------------------------------------------------------------------
# Detector kinds — the wire contract with the Rust detector
# ---------------------------------------------------------------------------

DETECTOR_SILENCE = "silence"
DETECTOR_MISSED_SCHEDULE = "missed_schedule"
DETECTOR_VOLUME_COLLAPSE = "volume_collapse"
DETECTOR_STALL = "stall"
DETECTOR_HARD_FAIL_CLIFF = "hard_fail_cliff"

LIVENESS_DETECTORS = frozenset({
    DETECTOR_SILENCE,
    DETECTOR_MISSED_SCHEDULE,
    DETECTOR_VOLUME_COLLAPSE,
    DETECTOR_STALL,
    DETECTOR_HARD_FAIL_CLIFF,
})

# Share of a window's runs that must be abandoned before the system counts
# as stalled — runs that start and never finish. Set high because a few
# abandoned runs usually indict the client, not the system.
STALL_ABANDONED_SHARE = 0.5

# Share of a window's runs that must be errored before it counts as a
# hard-fail cliff.
HARD_FAIL_SHARE = 0.8

# Below this many runs a share is not meaningful — two runs both failing
# is not a cliff, it is Tuesday.
MIN_RUNS_FOR_SHARE_DETECTORS = 5


class AISystemLivenessExpectation(Base):
    """The declared (or accepted-from-learned) cadence for one AI System."""

    __tablename__ = "ai_system_liveness_expectations"

    id = Column(String(64), primary_key=True)  # live-<ULID>

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    ai_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=False,
        comment="The AI System this expectation is for (a resource group)",
    )

    mode = Column(
        String(16),
        nullable=False,
        default=DEFAULT_LIVENESS_MODE,
        server_default=DEFAULT_LIVENESS_MODE,
        comment="continuous | scheduled | sporadic | on_demand",
    )

    expected_min_runs = Column(
        Integer,
        nullable=False,
        default=DEFAULT_EXPECTED_MIN_RUNS,
        server_default=str(DEFAULT_EXPECTED_MIN_RUNS),
        comment="Minimum runs expected within the window",
    )

    window_seconds = Column(
        Integer,
        nullable=False,
        default=DEFAULT_WINDOW_SECONDS,
        server_default=str(DEFAULT_WINDOW_SECONDS),
    )

    grace_seconds = Column(
        Integer,
        nullable=False,
        default=DEFAULT_GRACE_SECONDS,
        server_default=str(DEFAULT_GRACE_SECONDS),
        comment=(
            "Slack beyond the window before silence is called. Absorbs "
            "normal jitter so a job that habitually runs three minutes "
            "late does not page anyone."
        ),
    )

    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Operator kill switch, distinct from mode='on_demand'",
    )

    # A scheduled system that keeps polling while its
    # feed has silently stopped is ALIVE (it runs) but does no work. This
    # optional bound on the time since the last PROCESSED run (a `success`
    # run — an outcome that counts toward the baseline) raises the separate
    # `work_quiet` signal. Never silence: the system is answering.
    max_quiet_processed_seconds = Column(
        Integer,
        nullable=True,
        comment="Max seconds since the last processed run before "
        "work_quiet; NULL = not checked",
    )

    # ------------------------------------------------------------------
    # Learned cadence — what the system actually does
    # ------------------------------------------------------------------

    learned_from_runs = Column(
        Integer,
        nullable=True,
        comment="How many runs the proposal was derived from",
    )
    learned_median_gap_seconds = Column(Integer, nullable=True)
    learned_p95_gap_seconds = Column(
        Integer,
        nullable=True,
        comment=(
            "The p95 gap, not the median, is what a window should be sized "
            "from: a window at the median alerts on half of all normal "
            "quiet periods."
        ),
    )
    learned_at = Column(DateTime(timezone=True), nullable=True)

    accepted_from_learned = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="True when the operator took the proposal rather than typing values",
    )

    # ------------------------------------------------------------------
    # Detector state
    # ------------------------------------------------------------------

    last_seen_run_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Start of the most recent run the detector observed",
    )
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_alert_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Suppresses re-alerting on every tick while a system stays silent",
    )

    created_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "ai_system_id", name="uq_ai_system_liveness_system"
        ),
        Index("ix_ai_system_liveness_tenant_mode", "tenant_id", "mode", "enabled"),
        {
            "comment": (
                "Declared cadence per AI System. The only "
                "signal that fires on the absence of traffic."
            )
        },
    )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @validates("mode")
    def _validate_mode(self, _key, value):
        if value is not None and value not in LIVENESS_MODES:
            raise ValueError(
                f"mode must be one of {sorted(LIVENESS_MODES)}, got {value!r}"
            )
        return value

    @validates("window_seconds", "grace_seconds")
    def _validate_positive_seconds(self, key, value):
        if value is not None and value < 0:
            raise ValueError(f"{key} must not be negative, got {value!r}")
        return value

    @validates("expected_min_runs")
    def _validate_expected_min_runs(self, _key, value):
        if value is not None and value < 1:
            raise ValueError(
                f"expected_min_runs must be at least 1, got {value!r}"
            )
        return value

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------

    @property
    def alerts_on_silence(self) -> bool:
        """Whether going quiet is a finding for this system."""
        return self.enabled and self.mode in SILENCE_ALERTING_MODES

    @property
    def deadline_seconds(self) -> int:
        """How long a system may be quiet before it counts as silent."""
        return int(self.window_seconds or 0) + int(self.grace_seconds or 0)

    @property
    def has_learned_cadence(self) -> bool:
        return (
            self.learned_from_runs is not None
            and self.learned_from_runs >= MIN_RUNS_TO_LEARN_CADENCE
        )

    def __repr__(self) -> str:
        return (
            f"<AISystemLivenessExpectation(system='{self.ai_system_id}', "
            f"mode='{self.mode}', window={self.window_seconds}s)>"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "mode": self.mode,
            "expected_min_runs": self.expected_min_runs,
            "window_seconds": self.window_seconds,
            "grace_seconds": self.grace_seconds,
            "enabled": self.enabled,
            "max_quiet_processed_seconds": self.max_quiet_processed_seconds,
            "learned_from_runs": self.learned_from_runs,
            "learned_median_gap_seconds": self.learned_median_gap_seconds,
            "learned_p95_gap_seconds": self.learned_p95_gap_seconds,
            "learned_at": self.learned_at.isoformat() if self.learned_at else None,
            "accepted_from_learned": self.accepted_from_learned,
            "last_seen_run_at": (
                self.last_seen_run_at.isoformat() if self.last_seen_run_at else None
            ),
            "last_checked_at": (
                self.last_checked_at.isoformat() if self.last_checked_at else None
            ),
            "last_alert_at": (
                self.last_alert_at.isoformat() if self.last_alert_at else None
            ),
            "alerts_on_silence": self.alerts_on_silence,
            "deadline_seconds": self.deadline_seconds,
            "has_learned_cadence": self.has_learned_cadence,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "AISystemLivenessExpectation",
    "LIVENESS_MODES",
    "LIVENESS_CONTINUOUS",
    "LIVENESS_SCHEDULED",
    "LIVENESS_SPORADIC",
    "LIVENESS_ON_DEMAND",
    "SILENCE_ALERTING_MODES",
    "DEFAULT_MODE_BY_SYSTEM_KIND",
    "DEFAULT_LIVENESS_MODE",
    "DEFAULT_WINDOW_SECONDS",
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_EXPECTED_MIN_RUNS",
    "MIN_RUNS_TO_LEARN_CADENCE",
    "LIVENESS_DETECTORS",
    "DETECTOR_SILENCE",
    "DETECTOR_MISSED_SCHEDULE",
    "DETECTOR_VOLUME_COLLAPSE",
    "DETECTOR_STALL",
    "DETECTOR_HARD_FAIL_CLIFF",
    "STALL_ABANDONED_SHARE",
    "HARD_FAIL_SHARE",
    "MIN_RUNS_FOR_SHARE_DETECTORS",
]

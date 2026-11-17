# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Baselines and drift

The run ledger says what an AI System *did*. Liveness says whether it is
*alive*. This says whether what it does now is **different from what it used to do**
— and, crucially, *why*.

## Why the statistics are deliberately unfashionable

Every measure here is robust: median and MAD rather than mean and standard
deviation, Jensen–Shannon divergence rather than KL, nearest-rank
percentiles rather than interpolated ones.

Run metrics are heavy-tailed. One agent that loops 400 times moves a mean
enough to hide a real shift in the other 500 runs, and a standard
deviation computed over that tail is wide enough to swallow anything. A
median barely notices the outlier, which is the point.

## Severity is never a p-value

A statistically bulletproof 3% cost rise is `low`. A noisy 4× jump in
max-iteration exhaustion on a refund system is `critical`. Severity comes
from effect size × run volume × business impact (cost delta, autonomy
level, EU AI Act risk tier). Ranking findings by confidence rather than
consequence is how a drift feed becomes noise.

## The disambiguator

Behavioural drift and input drift moving together does not mean the system
changed — it means the *questions* changed. That distinction is worth more
than three extra metrics, and it is why `input` is a first-class drift
class rather than a footnote: without it, every Monday-morning traffic
spike looks like a regression.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    text,
    CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.database import Base

# ---------------------------------------------------------------------------
# Drift classes
# ---------------------------------------------------------------------------

DRIFT_BEHAVIOURAL = "behavioural"   # is it working differently?
DRIFT_COST = "cost"                 # is the same work costing more?
DRIFT_DEPENDENCY = "dependency"     # did something underneath change?
DRIFT_MODEL = "model"               # did the provider change the model?
DRIFT_INPUT = "input"               # did the *world* change instead?
DRIFT_CONFORMANCE = "conformance"   # is it being used as declared?

#: A declared operating-envelope term breached. Not a statistical
#: finding: the bound was signed off, the ledger says it was exceeded.
#: Emitted by the control plane's assurance tick, not the core sweeps —
#: and matchable by response policies like any other class, which is what
#: lets "breach the contract → tighten autonomy" be a one-line policy.
DRIFT_CONTRACT = "contract"

#: An agent implementation finding on the system (unapproved
#: release, name mismatch, release missing, undeclared change) opened by the
#: control plane's agent checks, so a response policy on `drift_class:
#: agent` can act on it (e.g. unapproved release on a high-risk system ⇒
#: approval_required). CP-emitted only, like `contract`; the core's sweeps
#: never emit it, so its per-class cooldown is untouched.
DRIFT_AGENT = "agent"

DRIFT_CLASSES = frozenset({
    DRIFT_BEHAVIOURAL,
    DRIFT_COST,
    DRIFT_DEPENDENCY,
    DRIFT_MODEL,
    DRIFT_INPUT,
    DRIFT_CONFORMANCE,
    DRIFT_CONTRACT,
    DRIFT_AGENT,
})

# ---------------------------------------------------------------------------
# Baseline lifecycle
# ---------------------------------------------------------------------------

BASELINE_LEARNING = "learning"   # too few runs for a shape to mean anything
BASELINE_READY = "ready"
BASELINE_STALE = "stale"         # window has aged out; recompute before trusting

BASELINE_STATES = frozenset({BASELINE_LEARNING, BASELINE_READY, BASELINE_STALE})

# Sentinel for the system-wide (not actor-scoped) baseline row. See the
# `actor_id` column comment: NULL cannot be used because it participates in
# the uniqueness key. Mirrored in gateway-core as
# `services::drift::baseline::ALL_ACTORS`.
BASELINE_ALL_ACTORS = "all"

# What a baseline (and a drift event) was computed from.
# A root system's baselines come from the runs it roots; a shared service's
# from the segments it serves inside other systems' runs. A system that does
# both has both.
BASIS_RUNS = "runs"
BASIS_SEGMENTS = "segments"
BASES = frozenset({BASIS_RUNS, BASIS_SEGMENTS})

# Aggregate over every caller of a service ('all'); per-caller rows carry the
# calling AI System's id. Sentinel, not NULL — part of the unique key.
BASELINE_ALL_CALLERS = "all"

# Below this many baseline-eligible runs a system is `learning` and emits
# **no** drift alerts at all. Being honest that value lands in week three
# beats emitting noise on day one — and an operator who is paged by a
# baseline built from eight runs never trusts the feature again.
MIN_RUNS_FOR_BASELINE = 50

# Rolling reference window. 30 days for low-volume systems so a weekly
# rhythm is still visible.
BASELINE_WINDOW_DAYS = 14
BASELINE_WINDOW_DAYS_LOW_VOLUME = 30

# A baseline older than this is stale: it describes a system that may no
# longer exist.
BASELINE_MAX_AGE_HOURS = 36

# ---------------------------------------------------------------------------
# Severity + status
# ---------------------------------------------------------------------------

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

DRIFT_SEVERITIES = frozenset({
    SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW,
})

DRIFT_ACTIVE = "active"
DRIFT_ACKNOWLEDGED = "acknowledged"
DRIFT_RESOLVED = "resolved"
# First-class, and fed back into per-system threshold tuning. A detector
# that cannot be told it was wrong will be ignored inside a month.
DRIFT_FALSE_POSITIVE = "false_positive"

DRIFT_STATUSES = frozenset({
    DRIFT_ACTIVE, DRIFT_ACKNOWLEDGED, DRIFT_RESOLVED, DRIFT_FALSE_POSITIVE,
})

# ---------------------------------------------------------------------------
# The cause-attribution ladder
# ---------------------------------------------------------------------------
#
# Walked in order; the first candidate whose change window overlaps the
# detected change point wins. An alert that says "step count up 180%,
# p=0.001" transfers work to the operator. One that says "step count up
# 180% since 14:00 on the 14th; the provider shipped a new point release
# of the underlying model at 13:52" ends the investigation before it
# starts.

#: Rung 0: the change point lies inside a declared incident
#: window (or within CAUSE_WINDOW_HOURS of its edge); the system's own window
#: is preferred over a tenant-wide one. Evidence keys: incident_window_id,
#: tenant_wide, starts_at, ends_at, cause_category, reason. Written by the
#: core's `drift/cause.rs`.
CAUSE_INCIDENT_WINDOW = "incident_window"        # rung 0
CAUSE_CONTRACT_CHANGE = "contract_change"        # rung 1
CAUSE_AGENT_RELEASE = "agent_release"            # rung 1b — the agent was redeployed
CAUSE_MODEL_CHANGE = "model_change"              # rung 2 — the differentiator
CAUSE_PROMPT_CHANGE = "prompt_change"            # rung 3 — the instructions changed, no release
CAUSE_DEPENDENCY_CHANGE = "dependency_change"    # rung 3 — tool schema moved
CAUSE_DEPENDENCY_DEGRADED = "dependency_degraded"  # rung 4
CAUSE_COMPOSITION_CHANGE = "composition_change"  # rung 5 — RG membership
CAUSE_CORPUS_CHANGE = "corpus_change"            # rung 6 — the retrieval collections changed
CAUSE_INPUT_DRIFT = "input_drift"                # rung 6 — the world changed
CAUSE_TRAFFIC_MIX = "traffic_mix"                # rung 7 — new caller
CAUSE_UNEXPLAINED = "unexplained"                # rung 8 — say so plainly

CAUSE_LADDER = (
    # A declared incident explains a change before anything else
    # does: the operator already said what happened in that period.
    CAUSE_INCIDENT_WINDOW,
    CAUSE_CONTRACT_CHANGE,
    # A release transition in the system's runs within the
    # change-point window (or an undeclared change: same release, new
    # implementation fingerprint). Mirrors the core's `drift/cause.rs`
    # rung `agent_release`, after contract_change, before model_change.
    CAUSE_AGENT_RELEASE,
    CAUSE_MODEL_CHANGE,
    CAUSE_PROMPT_CHANGE,
    CAUSE_DEPENDENCY_CHANGE,
    CAUSE_DEPENDENCY_DEGRADED,
    CAUSE_COMPOSITION_CHANGE,
    CAUSE_CORPUS_CHANGE,
    CAUSE_INPUT_DRIFT,
    CAUSE_TRAFFIC_MIX,
    CAUSE_UNEXPLAINED,
)
#: The cause recorded on `contract`-class events. Not a rung of the
#: statistical attribution ladder: a breach is not a change we explain, it
#: is a declared bound the ledger says was exceeded.
CAUSE_CONTRACT_BREACH = "contract_breach"

CAUSES = frozenset(CAUSE_LADDER) | {CAUSE_CONTRACT_BREACH}

# How far either side of a detected change point a candidate cause may sit
# and still be considered its explanation.
CAUSE_WINDOW_HOURS = 2

# ---------------------------------------------------------------------------
# Detection thresholds
# ---------------------------------------------------------------------------

# Jensen–Shannon divergence over a categorical distribution. Bounded 0–1
# and symmetric, unlike KL — an unbounded score is impossible to threshold
# meaningfully and impossible to explain to an operator.
JS_DIVERGENCE_THRESHOLD = 0.15

# Robust z-score magnitude before a continuous metric is even a candidate.
ROBUST_Z_THRESHOLD = 3.0

# Page–Hinkley, run over *robust z-scores* rather than raw metric values.
# That normalisation is what makes these constants scale-free: 0.5 units
# of slack is enormous for a cost measured in hundredths of a cent and
# invisible for a duration in thousands of milliseconds, so a detector fed
# raw values would silently never fire for some metrics.
#
# The threshold exceeds what any single observation can contribute
# (robust_z saturates at 10), so a change point must be earned over
# several observations — one bad afternoon is not a system that started
# behaving differently.
PAGE_HINKLEY_THRESHOLD = 25.0
PAGE_HINKLEY_DELTA = 0.5

# A rate must move by at least this in absolute terms as well as
# proportionally. Without it, 0.1% → 0.3% on huge volume pages someone.
MIN_ABSOLUTE_RATE_CHANGE = 0.02

# Consecutive evaluation cycles a categorical shift must persist.
CATEGORICAL_SUSTAIN_CYCLES = 2


class AISystemBaseline(Base):
    """What normal looks like, for one metric on one AI System."""

    __tablename__ = "ai_system_baselines"

    id = Column(String(64), primary_key=True)  # base-<ULID>

    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    ai_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=False,
    )

    metric_key = Column(
        String(64),
        nullable=False,
        comment="action_count | cost_per_run | tool_mix | terminal_state_mix | …",
    )

    bucket = Column(
        String(128),  # 128, not 32: per-outcome buckets are full outcome ids
        nullable=False,
        default="all",
        server_default="all",
        comment=(
            "Seasonality bucket — 'all', or 'dow:2' / 'hod:14' when volume "
            "permits; a full outcome id ('<outcome>[.<variant>]') "
            "for a per-outcome baseline. Prevents the classic Monday false positive: a system "
            "compared against a flat all-week baseline looks broken every "
            "Monday morning and fine by Wednesday."
        ),
    )

    actor_id = Column(
        String(255),
        nullable=False,
        default=BASELINE_ALL_ACTORS,
        server_default=BASELINE_ALL_ACTORS,
        comment=(
            "Optional per-actor sub-baseline. A system-level baseline can "
            "mask one worker degrading among five; 'all' is the system-wide "
            "row. Deliberately a sentinel string rather than NULL: this "
            "column is part of the uniqueness key below, and SQL treats two "
            "NULLs as distinct, so a nullable version would make every "
            "system-wide upsert insert a new row instead of updating the "
            "existing one. The sweeper runs every 15 minutes, so that grows "
            "without bound and makes 'the' baseline ambiguous. `bucket` "
            "already uses the same 'all' convention for the same reason."
        ),
    )

    basis = Column(
        String(16),
        nullable=False,
        default=BASIS_RUNS,
        server_default=BASIS_RUNS,
        comment=(
            "'runs' (the system's own runs) | 'segments' (the "
            "work it served inside other systems' runs, as a shared service)."
        ),
    )

    caller_system_id = Column(
        String(255),
        nullable=False,
        default=BASELINE_ALL_CALLERS,
        server_default=BASELINE_ALL_CALLERS,
        comment=(
            "Per-caller sub-baseline of a service; 'all' is the "
            "aggregate. Sentinel rather than NULL for the same reason as "
            "actor_id: part of the unique key."
        ),
    )

    # --- robust statistics -------------------------------------------------

    median = Column(Float, nullable=True)
    mad = Column(
        Float,
        nullable=True,
        comment=(
            "Median absolute deviation. Scaled by 1.4826 at comparison time "
            "to be comparable with a standard deviation."
        ),
    )
    p50 = Column(Float, nullable=True)
    p90 = Column(Float, nullable=True)
    p99 = Column(Float, nullable=True)

    distribution = Column(
        JSONB,
        nullable=True,
        comment="Normalized frequency vector for categorical metrics",
    )

    sample_count = Column(Integer, nullable=False, default=0, server_default="0")

    window_start = Column(DateTime(timezone=True), nullable=True)
    window_end = Column(DateTime(timezone=True), nullable=True)

    state = Column(
        String(16),
        nullable=False,
        default=BASELINE_LEARNING,
        server_default=BASELINE_LEARNING,
    )

    computed_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "ai_system_id", "metric_key", "bucket", "actor_id",
            "basis", "caller_system_id",
            name="uq_ai_system_baselines_key",
        ),
        CheckConstraint(
            "basis IN ('runs', 'segments')", name="ck_ai_system_baselines_basis"
        ),
        Index("ix_ai_system_baselines_system", "tenant_id", "ai_system_id", "state"),
        {
            "comment": (
                "Robust baseline per (system, metric, "
                "bucket). Median/MAD, not mean/sigma: run metrics are "
                "heavy-tailed."
            )
        },
    )

    @validates("basis")
    def _validate_basis(self, _key, value):
        if value is not None and value not in BASES:
            raise ValueError(f"basis must be one of {sorted(BASES)}, got {value!r}")
        return value

    @validates("state")
    def _validate_state(self, _key, value):
        if value is not None and value not in BASELINE_STATES:
            raise ValueError(
                f"state must be one of {sorted(BASELINE_STATES)}, got {value!r}"
            )
        return value

    @property
    def is_ready(self) -> bool:
        """Whether this baseline may be compared against.

        `learning` never alerts — a baseline built from eight runs will
        fire, and the operator it pages will not trust the feature again.
        """
        return self.state == BASELINE_READY and (self.sample_count or 0) >= MIN_RUNS_FOR_BASELINE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ai_system_id": self.ai_system_id,
            "metric_key": self.metric_key,
            "bucket": self.bucket,
            "actor_id": self.actor_id,
            "basis": self.basis or BASIS_RUNS,
            "caller_system_id": self.caller_system_id or BASELINE_ALL_CALLERS,
            "median": self.median,
            "mad": self.mad,
            "p50": self.p50,
            "p90": self.p90,
            "p99": self.p99,
            "distribution": self.distribution or {},
            "sample_count": self.sample_count,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "state": self.state,
            "is_ready": self.is_ready,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }


class AISystemDriftEvent(Base):
    """One detected behavioural change, with its most likely explanation."""

    __tablename__ = "ai_system_drift_events"

    id = Column(String(64), primary_key=True)  # drift-<ULID>

    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    ai_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id = Column(String(255), nullable=True)

    basis = Column(
        String(16),
        nullable=False,
        default=BASIS_RUNS,
        server_default=BASIS_RUNS,
        comment="The baseline basis this was detected against: runs | segments",
    )
    caller_system_id = Column(
        String(255),
        nullable=True,
        comment="Set when detected against a per-caller service baseline",
    )

    drift_class = Column(String(24), nullable=False)
    metric_key = Column(String(64), nullable=False)

    detected_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    change_point_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "When the behaviour actually moved, as opposed to when we "
            "noticed. This is what the cause ladder matches candidates "
            "against — matching on detection time would blame whatever "
            "happened during the sweep."
        ),
    )

    baseline_value = Column(Float, nullable=True)
    observed_value = Column(Float, nullable=True)
    effect_size = Column(
        Float,
        nullable=True,
        comment="Robust z-score, or JS divergence for categorical metrics",
    )
    direction = Column(String(8), nullable=True, comment="up | down | shift")

    severity = Column(String(16), nullable=False, default=SEVERITY_MEDIUM, server_default="medium")

    # --- the differentiator ------------------------------------------------

    attributed_cause = Column(String(32), nullable=False, default=CAUSE_UNEXPLAINED, server_default="unexplained")
    cause_confidence = Column(Float, nullable=True)
    cause_evidence = Column(
        JSONB,
        nullable=True,
        comment="What the ladder matched on — model id, snapshot diff, timings",
    )
    correlated_metrics = Column(
        JSONB,
        nullable=True,
        comment=(
            "Top correlated metrics, attached when the cause is "
            "`unexplained` so the operator has somewhere to start rather "
            "than a shrug."
        ),
    )

    response_taken = Column(
        JSONB,
        nullable=True,
        comment="Automatic actions taken (closed loop)",
    )
    system_alert_id = Column(String(64), nullable=True)

    status = Column(String(16), nullable=False, default=DRIFT_ACTIVE, server_default="active")
    resolution_note = Column(Text, nullable=True)
    resolved_by = Column(String(255), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolution_kind = Column(
        String(32),
        nullable=True,
        comment=(
            "How the finding was closed: accepted_change | "
            "cause_fixed | false_positive | auto_cleared. NULL while open."
        ),
    )

    __table_args__ = (
        Index("ix_drift_system_detected", "tenant_id", "ai_system_id", "detected_at"),
        Index("ix_drift_status", "tenant_id", "status", "detected_at"),
        Index("ix_drift_class", "tenant_id", "drift_class", "detected_at"),
        CheckConstraint(
            "basis IN ('runs', 'segments')", name="ck_ai_system_drift_events_basis"
        ),
        # Every close sets a kind — enforced by the one closer
        # (`app/services/drift_resolution.close_finding`), a flush guard and
        # a static guard; the database refuses the other half (an OPEN
        # finding carrying a kind). "Closed ⇒ kind" is not a table
        # constraint: a closed row may carry NULL.
        CheckConstraint(
            "resolution_kind IS NULL OR resolution_kind IN "
            "('accepted_change', 'cause_fixed', 'false_positive', 'auto_cleared')",
            name="ck_ai_system_drift_events_resolution_kind",
        ),
        CheckConstraint(
            "status NOT IN ('active', 'acknowledged') OR resolution_kind IS NULL",
            name="ck_ai_system_drift_events_open_no_kind",
        ),
        {
            "comment": (
                "A detected behavioural change with its "
                "attributed cause."
            )
        },
    )

    @validates("basis")
    def _validate_basis(self, _key, value):
        if value is not None and value not in BASES:
            raise ValueError(f"basis must be one of {sorted(BASES)}, got {value!r}")
        return value

    @validates("drift_class")
    def _validate_class(self, _key, value):
        if value is not None and value not in DRIFT_CLASSES:
            raise ValueError(
                f"drift_class must be one of {sorted(DRIFT_CLASSES)}, got {value!r}"
            )
        return value

    @validates("severity")
    def _validate_severity(self, _key, value):
        if value is not None and value not in DRIFT_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(DRIFT_SEVERITIES)}, got {value!r}"
            )
        return value

    @validates("status")
    def _validate_status(self, _key, value):
        if value is not None and value not in DRIFT_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(DRIFT_STATUSES)}, got {value!r}"
            )
        return value

    @validates("resolution_kind")
    def _validate_resolution_kind(self, _key, value):
        from app.constants.drift_resolution import RESOLUTION_KINDS

        if value is not None and value not in RESOLUTION_KINDS:
            raise ValueError(
                f"resolution_kind must be one of {sorted(RESOLUTION_KINDS)}, got {value!r}"
            )
        return value

    @validates("attributed_cause")
    def _validate_cause(self, _key, value):
        if value is not None and value not in CAUSES:
            raise ValueError(
                f"attributed_cause must be one of {sorted(CAUSES)}, got {value!r}"
            )
        return value

    @property
    def is_open(self) -> bool:
        return self.status in (DRIFT_ACTIVE, DRIFT_ACKNOWLEDGED)

    @property
    def is_explained(self) -> bool:
        return self.attributed_cause != CAUSE_UNEXPLAINED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ai_system_id": self.ai_system_id,
            "actor_id": self.actor_id,
            "basis": self.basis or BASIS_RUNS,
            "caller_system_id": self.caller_system_id,
            "drift_class": self.drift_class,
            "metric_key": self.metric_key,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "change_point_at": (
                self.change_point_at.isoformat() if self.change_point_at else None
            ),
            "baseline_value": self.baseline_value,
            "observed_value": self.observed_value,
            "effect_size": self.effect_size,
            "direction": self.direction,
            "severity": self.severity,
            "attributed_cause": self.attributed_cause,
            "cause_confidence": self.cause_confidence,
            "cause_evidence": self.cause_evidence or {},
            "correlated_metrics": self.correlated_metrics or [],
            "response_taken": self.response_taken or {},
            "system_alert_id": self.system_alert_id,
            "status": self.status,
            "resolution_note": self.resolution_note,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_kind": self.resolution_kind,
            "is_open": self.is_open,
            "is_explained": self.is_explained,
        }


# ---------------------------------------------------------------------------
# A finding is never closed without a kind
# ---------------------------------------------------------------------------

DRIFT_CLOSED_STATUSES = frozenset({DRIFT_RESOLVED, DRIFT_FALSE_POSITIVE})


class DriftClosedWithoutKindError(ValueError):
    """A drift finding was moved to a closed status with no resolution kind."""


def _closed_without_kind(obj: "AISystemDriftEvent", *, is_new: bool) -> bool:
    if obj.status not in DRIFT_CLOSED_STATUSES or obj.resolution_kind is not None:
        return False
    if is_new:
        return True
    from sqlalchemy import inspect as sa_inspect

    # Only a transition is judged: a closed row that carries NULL may
    # still be touched (response_taken, system_alert_id) without being
    # re-closed.
    return sa_inspect(obj).attrs.status.history.has_changes()


def _guard_drift_closures(session, _flush_context, _instances) -> None:
    offenders = [
        o.id for o in session.new
        if isinstance(o, AISystemDriftEvent) and _closed_without_kind(o, is_new=True)
    ] + [
        o.id for o in session.dirty
        if isinstance(o, AISystemDriftEvent) and _closed_without_kind(o, is_new=False)
    ]
    if offenders:
        raise DriftClosedWithoutKindError(
            "drift finding(s) closed without a resolution_kind: "
            f"{offenders}. Close findings through "
            "app.services.drift_resolution.close_finding."
        )


def _install_closure_guard() -> None:
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    if not event.contains(Session, "before_flush", _guard_drift_closures):
        event.listen(Session, "before_flush", _guard_drift_closures)


_install_closure_guard()


__all__ = [
    "AISystemBaseline",
    "AISystemDriftEvent",
    "DRIFT_CLOSED_STATUSES",
    "DriftClosedWithoutKindError",
    "DRIFT_CLASSES", "DRIFT_BEHAVIOURAL", "DRIFT_COST", "DRIFT_DEPENDENCY",
    "DRIFT_MODEL", "DRIFT_INPUT", "DRIFT_CONFORMANCE", "DRIFT_AGENT",
    "BASES", "BASIS_RUNS", "BASIS_SEGMENTS", "BASELINE_ALL_CALLERS",
    "BASELINE_STATES", "BASELINE_LEARNING", "BASELINE_READY", "BASELINE_STALE",
    "MIN_RUNS_FOR_BASELINE", "BASELINE_WINDOW_DAYS",
    "BASELINE_WINDOW_DAYS_LOW_VOLUME", "BASELINE_MAX_AGE_HOURS",
    "DRIFT_SEVERITIES", "SEVERITY_CRITICAL", "SEVERITY_HIGH",
    "SEVERITY_MEDIUM", "SEVERITY_LOW",
    "DRIFT_STATUSES", "DRIFT_ACTIVE", "DRIFT_ACKNOWLEDGED", "DRIFT_RESOLVED",
    "DRIFT_FALSE_POSITIVE",
    "CAUSES", "CAUSE_LADDER", "CAUSE_UNEXPLAINED", "CAUSE_MODEL_CHANGE",
    "CAUSE_DEPENDENCY_CHANGE", "CAUSE_DEPENDENCY_DEGRADED",
    "CAUSE_COMPOSITION_CHANGE", "CAUSE_INPUT_DRIFT", "CAUSE_TRAFFIC_MIX",
    "CAUSE_CONTRACT_CHANGE", "CAUSE_AGENT_RELEASE", "CAUSE_WINDOW_HOURS",
    "CAUSE_INCIDENT_WINDOW",
    "JS_DIVERGENCE_THRESHOLD", "ROBUST_Z_THRESHOLD",
    "PAGE_HINKLEY_THRESHOLD", "PAGE_HINKLEY_DELTA",
    "MIN_ABSOLUTE_RATE_CHANGE", "CATEGORICAL_SUSTAIN_CYCLES",
]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Shadow replay

**Your audit log is an eval dataset.** Competitors have to instrument a
customer's stack to collect traces; Brutor collects them as a byproduct of
enforcement. That makes replay nearly free product surface and the
strongest available answer to *"how do I know it still works before I ship
the change?"*

## What replay is, and what it is not

Replay re-runs recorded production requests through the **real gateway
path** under a candidate contract — same enforcement, same guardrails, same
routing — and diffs the resulting trajectory against the recorded one.

It diffs **behaviour, not truth**. A changed trajectory is a prompt to look,
never a verdict, because the proxy has no ground truth about whether the new
answer was better. This is not a correctness eval suite, must not be
called one, and the UI must not imply otherwise.

## Replay must never move money

Every case carries a `side_effect_policy`, defaulting to `mock`. A replay
that actually calls `transfer_funds` because someone forgot to set a flag is
the single worst thing this feature could do, so the default is the safe one
and `sandbox` requires a designated server to be configured. `skip_case`
exists for the cases that cannot be made safe at all.

Replay runs are additionally written to `ai_system_runs` with
`is_replay = true`, which every baseline, liveness and drift query already
excludes. A simulation that taught the detector a shape that never happened
would produce drift alerts about a system that behaved normally.

## Stateful systems

Some systems cannot be replayed at all: those whose behaviour depends on
external memory the gateway does not control, where re-running a request
against today's memory tests something other than what was recorded. Those
suites are marked `replayable = false` with a stated reason, and the
lifecycle gate reports "not replayable" rather than silently passing — an
un-runnable suite that reads as a pass is worse than no suite.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base

# ---------------------------------------------------------------------------
# Vocabularies (plain VARCHAR + @validates)
# ---------------------------------------------------------------------------

SAMPLING_STRATIFIED = "stratified"
SAMPLING_RECENT = "recent"
SAMPLING_MANUAL = "manual"
SAMPLING_STRATEGIES = frozenset({SAMPLING_STRATIFIED, SAMPLING_RECENT, SAMPLING_MANUAL})

#: The default, and the only one that produces a suite worth gating on.
#: "The last 50 runs" encodes whatever the system happened to be doing on
#: Tuesday and systematically misses the tails — the exhausted runs, the rare
#: tool paths, the long trajectories — which are exactly the cases a config
#: change breaks.
DEFAULT_SAMPLING_STRATEGY = SAMPLING_STRATIFIED

SIDE_EFFECT_MOCK = "mock"        # replay the recorded tool response
SIDE_EFFECT_SANDBOX = "sandbox"  # route to a designated sandbox server
SIDE_EFFECT_SKIP = "skip_case"   # cannot be made safe; excluded from the run
SIDE_EFFECT_POLICIES = frozenset({SIDE_EFFECT_MOCK, SIDE_EFFECT_SANDBOX, SIDE_EFFECT_SKIP})

#: Safe by default and deliberately not configurable to something else at
#: the suite level. See the module docstring.
DEFAULT_SIDE_EFFECT_POLICY = SIDE_EFFECT_MOCK

CRITICALITY_NORMAL = "normal"
CRITICALITY_CRITICAL = "critical"
CRITICALITIES = frozenset({CRITICALITY_NORMAL, CRITICALITY_CRITICAL})

# Per-case verdicts. Ordered worst-last so a suite summary can report the
# most serious outcome without a lookup table.
VERDICT_MATCHED = "matched"
VERDICT_SHAPE_CHANGED = "shape_changed"      # different steps/tools, same outcome
VERDICT_POLICY_CHANGED = "policy_changed"    # a governance decision differs
VERDICT_OUTCOME_CHANGED = "outcome_changed"  # different terminal state
VERDICT_FAILED = "failed"                    # the replay itself could not run
VERDICT_SKIPPED = "skipped"                  # side_effect_policy = skip_case

VERDICTS = frozenset({
    VERDICT_MATCHED,
    VERDICT_SHAPE_CHANGED,
    VERDICT_POLICY_CHANGED,
    VERDICT_OUTCOME_CHANGED,
    VERDICT_FAILED,
    VERDICT_SKIPPED,
})

#: Verdicts that count as a pass. `shape_changed` passes on purpose: a
#: system that reached the same outcome by a different route has not
#: regressed, and failing it would make every prompt tweak look like a
#: breakage and teach operators to ignore the gate.
PASSING_VERDICTS = frozenset({VERDICT_MATCHED, VERDICT_SHAPE_CHANGED})

REPLAY_RUNNING = "running"
REPLAY_COMPLETED = "completed"
REPLAY_FAILED = "failed"
REPLAY_CANCELLED = "cancelled"
REPLAY_STATUSES = frozenset({
    REPLAY_RUNNING, REPLAY_COMPLETED, REPLAY_FAILED, REPLAY_CANCELLED,
})

TRIGGER_MANUAL = "manual"
TRIGGER_PROMOTION_GATE = "promotion_gate"
TRIGGER_DRIFT_RESPONSE = "drift_response"
TRIGGER_SCHEDULED = "scheduled"
REPLAY_TRIGGERS = frozenset({
    TRIGGER_MANUAL, TRIGGER_PROMOTION_GATE, TRIGGER_DRIFT_RESPONSE, TRIGGER_SCHEDULED,
})

#: Default pass rate for the `approved → active` gate.
DEFAULT_PASS_THRESHOLD = 0.9

#: How many cases a stratified sample aims for. Small enough to run in
#: minutes, large enough that one flaky case does not move the pass rate
#: past the threshold on its own.
DEFAULT_TARGET_CASE_COUNT = 30


class AISystemReplaySuite(Base):
    """A frozen set of recorded production runs to replay a change against."""

    __tablename__ = "ai_system_replay_suites"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    ai_system_id = Column(String(255), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    pass_threshold = Column(
        Float,
        nullable=False,
        default=DEFAULT_PASS_THRESHOLD,
        server_default=str(DEFAULT_PASS_THRESHOLD),
        comment=(
            "Fraction of cases that must pass for the promotion gate. "
            "Critical cases are additionally all-or-nothing on outcome."
        ),
    )

    sampling_strategy = Column(
        String(16),
        nullable=False,
        default=DEFAULT_SAMPLING_STRATEGY,
        server_default=DEFAULT_SAMPLING_STRATEGY,
    )

    target_case_count = Column(
        Integer,
        nullable=False,
        default=DEFAULT_TARGET_CASE_COUNT,
        server_default=str(DEFAULT_TARGET_CASE_COUNT),
    )

    replayable = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment=(
            "False for systems whose behaviour depends on external memory the "
            "gateway does not control — re-running the request tests something "
            "other than what was recorded. The gate reports 'not replayable' "
            "rather than passing: an un-runnable suite that reads as a pass is "
            "worse than no suite."
        ),
    )
    not_replayable_reason = Column(Text, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

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
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["ai_system_id"], ["resource_groups.id"], ondelete="CASCADE"
        ),
        UniqueConstraint(
            "tenant_id", "ai_system_id", "name", name="uq_replay_suites_name"
        ),
        Index("ix_replay_suites_system", "tenant_id", "ai_system_id", "enabled"),
        {
            "comment": (
                "A frozen set of recorded production runs "
                "used to diff a candidate contract against known behaviour."
            )
        },
    )

    @validates("sampling_strategy")
    def _v_sampling(self, _k: str, v: str) -> str:
        if v not in SAMPLING_STRATEGIES:
            raise ValueError(
                f"sampling_strategy must be one of {sorted(SAMPLING_STRATEGIES)}, got {v!r}"
            )
        return v

    @validates("pass_threshold")
    def _v_threshold(self, _k: str, v: float) -> float:
        if v is None or not (0.0 <= float(v) <= 1.0):
            raise ValueError(f"pass_threshold must be between 0 and 1, got {v!r}")
        return v

    @property
    def is_usable(self) -> bool:
        """Whether this suite can gate anything."""
        return bool(self.enabled) and bool(self.replayable)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "name": self.name,
            "description": self.description,
            "pass_threshold": self.pass_threshold,
            "sampling_strategy": self.sampling_strategy,
            "target_case_count": self.target_case_count,
            "replayable": self.replayable,
            "not_replayable_reason": self.not_replayable_reason,
            "enabled": self.enabled,
            "is_usable": self.is_usable,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AISystemReplayCase(Base):
    """One recorded run, frozen so a change can be diffed against it."""

    __tablename__ = "ai_system_replay_cases"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    suite_id = Column(String(64), nullable=False, index=True)

    source_run_id = Column(
        String(64),
        nullable=True,
        comment=(
            "The run this case was frozen from. SET NULL rather than CASCADE: "
            "the run may be pruned by retention, but the case is a frozen copy "
            "and outlives it. Losing the case when the run ages out would "
            "silently shrink every suite over time."
        ),
    )
    source_root_task_id = Column(String(255), nullable=True)

    recorded_request = Column(
        JSONB,
        nullable=False,
        comment="The root request: method, endpoint and body, as sent.",
    )
    recorded_trajectory = Column(
        JSONB,
        nullable=False,
        comment=(
            "The shape to diff against: step sequence, tool set, terminal "
            "state, policy outcomes, cost."
        ),
    )

    # Denormalized for the diff and for coverage reporting without parsing
    # the trajectory blob.
    recorded_terminal_state = Column(String(32), nullable=True)
    recorded_action_count = Column(Integer, nullable=True)
    recorded_tool_signature = Column(String(512), nullable=True)

    stratum = Column(
        String(64),
        nullable=True,
        comment=(
            "Which sampling bucket this case came from, e.g. "
            "'terminal:exhausted' or 'steps:d9'. Recorded so suite coverage "
            "is inspectable — a suite that turns out to be 30 happy-path "
            "cases gates nothing, and without this nobody would notice."
        ),
    )

    side_effect_policy = Column(
        String(16),
        nullable=False,
        default=DEFAULT_SIDE_EFFECT_POLICY,
        server_default=DEFAULT_SIDE_EFFECT_POLICY,
    )
    criticality = Column(
        String(16),
        nullable=False,
        default=CRITICALITY_NORMAL,
        server_default=CRITICALITY_NORMAL,
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["suite_id"], ["ai_system_replay_suites.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["source_run_id"], ["ai_system_runs.id"], ondelete="SET NULL"
        ),
        Index("ix_replay_cases_suite", "tenant_id", "suite_id", "enabled"),
        {
            "comment": (
                "One frozen production run, with the "
                "trajectory a replay is diffed against."
            )
        },
    )

    @validates("side_effect_policy")
    def _v_side_effect(self, _k: str, v: str) -> str:
        if v not in SIDE_EFFECT_POLICIES:
            raise ValueError(
                f"side_effect_policy must be one of {sorted(SIDE_EFFECT_POLICIES)}, got {v!r}"
            )
        return v

    @validates("criticality")
    def _v_criticality(self, _k: str, v: str) -> str:
        if v not in CRITICALITIES:
            raise ValueError(
                f"criticality must be one of {sorted(CRITICALITIES)}, got {v!r}"
            )
        return v

    @property
    def is_critical(self) -> bool:
        return self.criticality == CRITICALITY_CRITICAL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "suite_id": self.suite_id,
            "source_run_id": self.source_run_id,
            "source_root_task_id": self.source_root_task_id,
            "recorded_request": self.recorded_request,
            "recorded_trajectory": self.recorded_trajectory,
            "recorded_terminal_state": self.recorded_terminal_state,
            "recorded_action_count": self.recorded_action_count,
            "recorded_tool_signature": self.recorded_tool_signature,
            "stratum": self.stratum,
            "side_effect_policy": self.side_effect_policy,
            "criticality": self.criticality,
            "is_critical": self.is_critical,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AISystemReplayRun(Base):
    """One execution of a suite against a candidate contract."""

    __tablename__ = "ai_system_replay_runs"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    suite_id = Column(String(64), nullable=False, index=True)
    ai_system_id = Column(String(255), nullable=False, index=True)

    contract_id = Column(
        String(64),
        nullable=True,
        comment="The candidate contract this run exercised, if any.",
    )

    status = Column(
        String(16), nullable=False, default=REPLAY_RUNNING,
        server_default=REPLAY_RUNNING,
    )
    triggered_by = Column(
        String(24), nullable=False, default=TRIGGER_MANUAL,
        server_default=TRIGGER_MANUAL,
    )
    triggered_by_actor = Column(String(255), nullable=True)

    started_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)

    total_cases = Column(Integer, nullable=False, default=0, server_default="0")
    matched = Column(Integer, nullable=False, default=0, server_default="0")
    shape_changed = Column(Integer, nullable=False, default=0, server_default="0")
    policy_changed = Column(Integer, nullable=False, default=0, server_default="0")
    outcome_changed = Column(Integer, nullable=False, default=0, server_default="0")
    failed = Column(Integer, nullable=False, default=0, server_default="0")
    skipped = Column(Integer, nullable=False, default=0, server_default="0")

    pass_rate = Column(Float, nullable=True)
    critical_outcome_changed = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment=(
            "Outcome changes on cases marked critical. The gate requires zero "
            "of these regardless of pass rate — a suite of 200 cases would "
            "otherwise let one broken refund path through on the arithmetic."
        ),
    )

    case_results = Column(
        JSONB,
        nullable=True,
        comment=(
            "Per-case verdicts with the diff that produced them. JSONB rather "
            "than a fourth table: a run has tens of cases, not millions, and "
            "the results are read as a set or not at all."
        ),
    )
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["suite_id"], ["ai_system_replay_suites.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["ai_system_id"], ["resource_groups.id"], ondelete="CASCADE"
        ),
        Index("ix_replay_runs_suite", "tenant_id", "suite_id", "started_at"),
        Index("ix_replay_runs_system", "tenant_id", "ai_system_id", "started_at"),
        {
            "comment": (
                "One execution of a replay suite against a "
                "candidate contract."
            )
        },
    )

    @validates("status")
    def _v_status(self, _k: str, v: str) -> str:
        if v not in REPLAY_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(REPLAY_STATUSES)}, got {v!r}"
            )
        return v

    @validates("triggered_by")
    def _v_trigger(self, _k: str, v: str) -> str:
        if v not in REPLAY_TRIGGERS:
            raise ValueError(
                f"triggered_by must be one of {sorted(REPLAY_TRIGGERS)}, got {v!r}"
            )
        return v

    @property
    def is_finished(self) -> bool:
        return self.status in (REPLAY_COMPLETED, REPLAY_FAILED, REPLAY_CANCELLED)

    def passes(self, threshold: float) -> bool:
        """Whether this run satisfies the promotion gate.

        Two independent conditions, because an aggregate alone is not
        enough: a 200-case suite at 95% has ten failures, and if one of them
        is the refund path the arithmetic should not wave it through.
        """
        if self.status != REPLAY_COMPLETED:
            return False
        if self.critical_outcome_changed:
            return False
        if self.pass_rate is None:
            # Nothing was scored — every case was skipped, or the suite was
            # empty. That is not a pass at *any* threshold, including zero:
            # `(pass_rate or 0.0) >= 0.0` would quietly wave through a suite
            # that demonstrated nothing at all.
            return False
        return self.pass_rate >= threshold

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "suite_id": self.suite_id,
            "ai_system_id": self.ai_system_id,
            "contract_id": self.contract_id,
            "status": self.status,
            "triggered_by": self.triggered_by,
            "triggered_by_actor": self.triggered_by_actor,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total_cases": self.total_cases,
            "matched": self.matched,
            "shape_changed": self.shape_changed,
            "policy_changed": self.policy_changed,
            "outcome_changed": self.outcome_changed,
            "failed": self.failed,
            "skipped": self.skipped,
            "pass_rate": self.pass_rate,
            "critical_outcome_changed": self.critical_outcome_changed,
            "case_results": self.case_results,
            "error_message": self.error_message,
            "is_finished": self.is_finished,
        }


def summarize(verdicts: List[str], criticals: Optional[List[bool]] = None) -> Dict[str, Any]:
    """Roll per-case verdicts into the counters a run stores.

    Pure, so the arithmetic that a promotion gate depends on is arguable
    against a table of numbers rather than only observable by running a
    replay.

    `skipped` cases are excluded from the pass rate entirely. Counting them
    as passes would let a suite whose every case was skipped report 100%,
    and counting them as failures would punish an operator for marking an
    unsafe case unsafe.
    """
    criticals = criticals or [False] * len(verdicts)
    counts = {v: 0 for v in VERDICTS}
    critical_outcome_changed = 0

    for verdict, is_critical in zip(verdicts, criticals):
        counts[verdict] = counts.get(verdict, 0) + 1
        if is_critical and verdict == VERDICT_OUTCOME_CHANGED:
            critical_outcome_changed += 1

    scored = len(verdicts) - counts[VERDICT_SKIPPED]
    passed = counts[VERDICT_MATCHED] + counts[VERDICT_SHAPE_CHANGED]

    return {
        "total_cases": len(verdicts),
        "matched": counts[VERDICT_MATCHED],
        "shape_changed": counts[VERDICT_SHAPE_CHANGED],
        "policy_changed": counts[VERDICT_POLICY_CHANGED],
        "outcome_changed": counts[VERDICT_OUTCOME_CHANGED],
        "failed": counts[VERDICT_FAILED],
        "skipped": counts[VERDICT_SKIPPED],
        # None, not 0.0, when nothing was scored: a confident 0% derived from
        # no evidence would read as a correctness verdict.
        "pass_rate": (passed / scored) if scored else None,
        "critical_outcome_changed": critical_outcome_changed,
    }

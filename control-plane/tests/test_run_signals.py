# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Run signal computation

The four signals are the run list's whole user-visible output, and every
one of them is a number an operator will make a decision from. So the
expected values here are hand-computed, not derived from the code under
test.

Particular attention to the two honesty rules the service enforces:

  * a 0% exhaustion rate with no client reporting means *unknown*, and the
    response must say so rather than implying health;
  * the WORK is the population: per-run figures average the
    `success` runs (completed, declared outcome that counts); a policy block
    is an errored run (cause `blocked_by_policy`) and counts against the
    rate. `add_run` defaults a completed run to the declared work outcome
    `task_completed`, so a plain completed run here is a success.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.ai_system_run import (
    AISystemRun,
    CHAIN_BROKEN,
    CHAIN_INTACT,
    CHAIN_PARTIAL,
    TERMINAL_ABANDONED,
    TERMINAL_COMPLETED,
    TERMINAL_COMPLETED_DEGRADED,
    TERMINAL_ERRORED,
    TERMINAL_EXHAUSTED,
)
from app.database.models.proxy_log import ProxyLog
from app.services.run_signals import run_signal_service

TENANT = "t1"
SYSTEM = "sys-1"


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=[AISystemRun.__table__, ProxyLog.__table__])
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Factory()
    yield s
    s.close()
    engine.dispose()


_counter = {"n": 0}


def add_run(
    session,
    *,
    state: str = TERMINAL_COMPLETED,
    cost: float = 0.0,
    steps: int = 1,
    tokens: int = 0,
    tool_calls: int = 0,
    tool_errors: int = 0,
    tool_error_types: Optional[dict] = None,
    chain: str = CHAIN_INTACT,
    finalized_by: str = "idle",
    is_replay: bool = False,
    reopened: int = 0,
    system_id: Optional[str] = SYSTEM,
    hours_ago: int = 1,
    outcome: Optional[str] = "task_completed",
    conformance: Optional[str] = None,
    counts: Optional[bool] = True,
    cause: Optional[str] = None,
):
    _counter["n"] += 1
    n = _counter["n"]
    completed = state in (TERMINAL_COMPLETED, TERMINAL_COMPLETED_DEGRADED)
    if not completed and outcome == "task_completed":
        outcome = None  # a failed run usually cannot know its outcome
    if conformance is None:
        conformance = "declared" if outcome else "unreported"
    if conformance != "declared":
        counts = None
    if cause is None and state in (TERMINAL_ERRORED, TERMINAL_EXHAUSTED):
        cause = "iteration_limit" if state == TERMINAL_EXHAUSTED else "client_reported"
    run = AISystemRun(
        id=f"run-{n}",
        tenant_id=TENANT,
        root_task_id=f"root-{n}",
        ai_system_id=system_id,
        started_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        ended_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        terminal_state=state,
        finalized_by=finalized_by,
        action_count=steps,
        tool_call_count=tool_calls,
        tool_error_count=tool_errors,
        tool_error_types=tool_error_types or {},
        total_tokens=tokens,
        total_cost_usd=cost,
        chain_integrity=chain,
        is_replay=is_replay,
        reopened_count=reopened,
        outcome_id=outcome,
        outcome_conformance=conformance,
        outcome_counts_toward_baseline=counts,
        error_cause=cause,
    )
    session.add(run)
    session.flush()
    return run


def add_step(session, run, proxy_type, *, status=200, subtype=None, success=None):
    """One gateway log row inside `run` (joined by tenant + root_task_id)."""
    _counter["n"] += 1
    session.add(ProxyLog(
        id=f"log-{_counter['n']}", tenant_id=TENANT, timestamp=run.started_at,
        proxy_type=proxy_type, proxy_subtype=subtype, http_method="POST",
        endpoint="/x", server_id="srv", correlation_id=f"c-{_counter['n']}",
        end_user_session_id="s", http_status_code=status, duration_ms=5,
        success=(status < 400) if success is None else success,
        root_task_id=run.root_task_id,
    ))
    session.flush()


def stats(session, **kw):
    return run_signal_service.compute(session, TENANT, ai_system_id=SYSTEM, **kw)


# ---------------------------------------------------------------------------
# Signal 1 — cost per completed run
# ---------------------------------------------------------------------------


class TestCostSignal:
    def test_cost_per_processed_outcome_uses_success_as_denominator(self, session):
        add_run(session, state=TERMINAL_COMPLETED, cost=1.0)
        add_run(session, state=TERMINAL_COMPLETED, cost=3.0)
        # An errored run costs money but produced nothing — it must not
        # flatter the per-outcome figure by joining the denominator.
        add_run(session, state=TERMINAL_ERRORED, cost=10.0)

        cost = stats(session).cost
        assert cost.processed_runs == 2
        assert cost.total_cost_usd == pytest.approx(14.0)
        assert cost.cost_per_processed_outcome == pytest.approx(2.0)

    def test_degraded_runs_with_a_declared_outcome_are_processed(self, session):
        # A run that needed a fallback still produced an answer and still
        # cost money.
        add_run(session, state=TERMINAL_COMPLETED, cost=1.0)
        add_run(session, state=TERMINAL_COMPLETED_DEGRADED, cost=3.0)
        assert stats(session).cost.processed_runs == 2

    def test_wasted_cost_is_failed_plus_deviating_runs(self, session):
        add_run(session, state=TERMINAL_ERRORED, cost=2.0)
        add_run(session, state=TERMINAL_ERRORED, cost=5.0, cause="blocked_by_policy")
        add_run(session, state=TERMINAL_COMPLETED, cost=7.0, outcome=None)  # unreported
        # Abandoned is not decisive: neither waste nor work.
        add_run(session, state=TERMINAL_ABANDONED, cost=3.0)
        # Spent, nothing produced: 2 + 5 (a policy block is an errored run) + 7.
        assert stats(session).cost.wasted_cost_usd == pytest.approx(14.0)

    def test_null_not_zero_when_nothing_was_processed(self, session):
        add_run(session, state=TERMINAL_ERRORED, cost=1.0)
        cost = stats(session).cost
        assert cost.cost_per_processed_outcome is None, (
            "0.0 would read as 'free', which is the opposite of the truth"
        )

    def test_percentiles_over_processed_runs(self, session):
        for c in [1.0, 2.0, 3.0, 4.0, 100.0]:
            add_run(session, state=TERMINAL_COMPLETED, cost=c)
        cost = stats(session).cost
        assert cost.median_cost_per_processed_outcome == pytest.approx(3.0)
        assert cost.p95_cost_per_processed_outcome == pytest.approx(100.0)

    def test_replays_never_move_an_operational_number(self, session):
        add_run(session, state=TERMINAL_COMPLETED, cost=1.0)
        add_run(session, state=TERMINAL_COMPLETED, cost=999.0, is_replay=True)
        assert stats(session).cost.total_cost_usd == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Signal 2 — steps + exhaustion
# ---------------------------------------------------------------------------


class TestActionSignal:
    def test_distribution_buckets_every_run_exactly_once(self, session):
        for steps in [1, 4, 8, 15, 30, 75, 500]:
            add_run(session, steps=steps)
        s = stats(session).actions
        assert sum(b.count for b in s.distribution) == 7
        top = s.distribution[-1]
        assert top.upper is None and top.count == 1

    def test_percentiles(self, session):
        for steps in [1, 2, 3, 4, 100]:
            add_run(session, steps=steps)
        s = stats(session).actions
        assert s.median_actions == pytest.approx(3)
        assert s.max_actions == 100

    def test_exhaustion_unknown_without_client_reporting(self, session):
        add_run(session, state=TERMINAL_COMPLETED, finalized_by="idle")
        s = stats(session).actions
        assert s.exhausted_runs == 0
        assert s.exhaustion_reporting_available is False, (
            "0% with no reporting means unknown, not healthy — the UI needs "
            "to be able to say which"
        )

    def test_exhaustion_reported_once_a_client_closes_a_run(self, session):
        add_run(session, state=TERMINAL_EXHAUSTED, finalized_by="explicit")
        add_run(session, state=TERMINAL_COMPLETED, finalized_by="explicit")
        s = stats(session).actions
        assert s.exhausted_runs == 1
        assert s.exhaustion_rate == pytest.approx(0.5)
        assert s.exhaustion_reporting_available is True

    def test_abandoned_is_tracked_separately(self, session):
        add_run(session, state=TERMINAL_ABANDONED)
        assert stats(session).actions.abandoned_runs == 1


# ---------------------------------------------------------------------------
# Signal 3 — tool errors
# ---------------------------------------------------------------------------


class TestToolErrorSignal:
    def test_error_rate_over_tool_calls(self, session):
        add_run(session, tool_calls=8, tool_errors=1, tool_error_types={"timeout": 1})
        add_run(session, tool_calls=2, tool_errors=1, tool_error_types={"timeout": 1})
        t = stats(session).tool_errors
        assert t.total_tool_calls == 10
        assert t.total_tool_errors == 2
        assert t.overall_error_rate == pytest.approx(0.2)

    def test_error_types_are_summed_across_runs(self, session):
        add_run(session, tool_calls=5, tool_errors=2, tool_error_types={"timeout": 2})
        add_run(
            session,
            tool_calls=5,
            tool_errors=3,
            tool_error_types={"timeout": 1, "upstream_500": 2},
        )
        rows = {r.tool: r.errors for r in stats(session).tool_errors.by_tool}
        assert rows == {"timeout": 3, "upstream_500": 2}

    def test_rate_is_null_when_no_tool_calls(self, session):
        add_run(session, tool_calls=0)
        assert stats(session).tool_errors.overall_error_rate is None


# ---------------------------------------------------------------------------
# Signal 4 — terminal state mix
# ---------------------------------------------------------------------------


class TestTerminalStateSignal:
    def test_shares_form_a_partition(self, session):
        add_run(session, state=TERMINAL_COMPLETED)
        add_run(session, state=TERMINAL_ERRORED)
        add_run(session, state=TERMINAL_ABANDONED)
        t = stats(session).terminal_states
        assert t.total_runs == 3
        # Shares are rounded to 6dp for a clean payload, so thirds sum to
        # 0.999999 rather than exactly 1. The property under test is that
        # every run lands in exactly one bucket; the tolerance covers the
        # rounding, not a gap.
        assert sum(r.share for r in t.by_state) == pytest.approx(1.0, abs=1e-4)
        assert sum(r.count for r in t.by_state) == 3

    def test_a_policy_block_counts_against_the_success_rate(self, session):
        add_run(session, state=TERMINAL_COMPLETED)
        add_run(session, state=TERMINAL_ERRORED, cause="blocked_by_policy")
        t = stats(session).terminal_states
        # 1 of 2 succeeded; the block is an errored run.
        assert t.success_rate == pytest.approx(0.5)
        assert {r.state for r in t.by_state} == {"completed", "errored"}

    def test_rates_are_null_on_an_empty_window(self, session):
        t = stats(session).terminal_states
        assert t.total_runs == 0
        assert t.success_rate is None


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


class TestDataQuality:
    def test_chain_integrity_share(self, session):
        add_run(session, chain=CHAIN_INTACT)
        add_run(session, chain=CHAIN_INTACT)
        add_run(session, chain=CHAIN_PARTIAL)
        add_run(session, chain=CHAIN_BROKEN)
        assert stats(session).chain_integrity_pct == pytest.approx(0.5)

    def test_chain_integrity_is_null_with_no_runs(self, session):
        assert stats(session).chain_integrity_pct is None

    def test_fragmented_runs_counted(self, session):
        add_run(session, reopened=0)
        add_run(session, reopened=4)  # above MAX_REOPENS_BEFORE_FRAGMENTED
        assert stats(session).fragmented_runs == 1


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


class TestScoping:
    def test_other_systems_are_excluded(self, session):
        add_run(session, cost=1.0, system_id=SYSTEM)
        add_run(session, cost=50.0, system_id="other-system")
        assert stats(session).cost.total_cost_usd == pytest.approx(1.0)

    def test_period_window_is_applied(self, session):
        add_run(session, cost=1.0, hours_ago=1)
        add_run(session, cost=99.0, hours_ago=100)
        assert stats(session, period_hours=24).cost.total_cost_usd == pytest.approx(1.0)

    def test_explicit_range_overrides_period(self, session):
        from datetime import datetime, timedelta, timezone
        add_run(session, cost=1.0, hours_ago=1)
        add_run(session, cost=99.0, hours_ago=100)
        # A 1-hour rolling period would see only the recent run; the
        # explicit range names the OLD window and must win.
        r = stats(
            session, period_hours=1,
            start=datetime.now(timezone.utc) - timedelta(hours=150),
            end=datetime.now(timezone.utc) - timedelta(hours=50),
        )
        assert r.cost.total_cost_usd == pytest.approx(99.0)
        assert r.terminal_states.total_runs == 1

    def test_period_zero_is_all_time(self, session):
        add_run(session, cost=1.0, hours_ago=1)
        add_run(session, cost=99.0, hours_ago=1000)
        assert stats(session, period_hours=0).cost.total_cost_usd == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Model invariants
# ---------------------------------------------------------------------------


class TestRunModel:
    def test_terminal_state_is_validated(self, session):
        with pytest.raises(ValueError, match="terminal_state must be one of"):
            AISystemRun(
                id="x",
                tenant_id=TENANT,
                root_task_id="r",
                started_at=datetime.now(timezone.utc),
                terminal_state="nonsense",
                finalized_by="idle",
            )

    def test_finalized_by_is_validated(self, session):
        with pytest.raises(ValueError, match="finalized_by must be one of"):
            AISystemRun(
                id="x",
                tenant_id=TENANT,
                root_task_id="r",
                started_at=datetime.now(timezone.utc),
                terminal_state=TERMINAL_COMPLETED,
                finalized_by="telepathy",
            )

    def test_baseline_eligibility_excludes_fragments_and_replays(self, session):
        ok = add_run(session, chain=CHAIN_INTACT)
        assert ok.is_baseline_eligible

        broken = add_run(session, chain=CHAIN_BROKEN)
        assert not broken.is_baseline_eligible

        replay = add_run(session, is_replay=True)
        assert not replay.is_baseline_eligible

        fragmented = add_run(session, reopened=9)
        assert not fragmented.is_baseline_eligible
        assert fragmented.is_fragmented

    def test_a_policy_block_is_not_successful(self, session):
        r = add_run(session, state=TERMINAL_ERRORED, cause="blocked_by_policy")
        assert not r.is_successful
        assert r.run_class == "failed"

    def test_to_dict_round_trips_the_derived_flags(self, session):
        r = add_run(session, state=TERMINAL_COMPLETED)
        d = r.to_dict()
        assert d["is_successful"] is True
        assert d["is_baseline_eligible"] is True
        assert d["terminal_state"] == TERMINAL_COMPLETED


# ---------------------------------------------------------------------------
# Bucketed timeseries — the chartable shape of the same signals
# ---------------------------------------------------------------------------


def timeseries(session, **kw):
    return run_signal_service.timeseries(session, TENANT, ai_system_id=SYSTEM, **kw)


class TestTimeseries:
    def test_hourly_buckets_at_short_windows(self, session):
        add_run(session, cost=0.02, steps=4, hours_ago=1)
        add_run(session, cost=0.04, steps=6, hours_ago=1)
        add_run(session, state=TERMINAL_ERRORED, cost=0.01, hours_ago=3)
        body = timeseries(session, period_hours=24)
        assert body.bucket == "hour"
        assert len(body.points) == 2  # two distinct hours, quiet ones omitted
        busy = body.points[-1]  # ordered ascending; hours_ago=1 is latest
        assert busy.runs == 2
        assert busy.success == 2
        assert busy.cost_usd == pytest.approx(0.06)
        assert busy.cost_per_processed_outcome == pytest.approx(0.03)
        assert busy.avg_actions == pytest.approx(5.0)

    def test_daily_buckets_at_long_windows(self, session):
        add_run(session, hours_ago=1)
        add_run(session, hours_ago=100)
        body = timeseries(session, period_hours=720)
        assert body.bucket == "day"
        assert len(body.points) == 2
        assert all(p.bucket_start.endswith("T00:00:00Z") for p in body.points)

    def test_run_classes_are_split_not_netted(self, session):
        add_run(session, state=TERMINAL_COMPLETED, hours_ago=1)
        add_run(session, state=TERMINAL_COMPLETED_DEGRADED, hours_ago=1)
        add_run(session, state=TERMINAL_COMPLETED, outcome="nothing_to_do", counts=False, hours_ago=1)
        add_run(session, state=TERMINAL_COMPLETED, outcome="made_up", conformance="undeclared", hours_ago=1)
        add_run(session, state=TERMINAL_ERRORED, hours_ago=1)
        add_run(session, state=TERMINAL_ERRORED, cause="blocked_by_policy", hours_ago=1)
        add_run(session, state=TERMINAL_ABANDONED, hours_ago=1)
        (point,) = timeseries(session, period_hours=24).points
        assert (point.success, point.no_work, point.deviation, point.failed, point.not_decisive) == (
            2, 1, 1, 2, 1
        )
        assert point.runs == 7

    def test_cost_per_processed_outcome_counts_degraded_completions(self, session):
        add_run(session, state=TERMINAL_COMPLETED, cost=0.02, hours_ago=1)
        add_run(session, state=TERMINAL_COMPLETED_DEGRADED, cost=0.04, hours_ago=1)
        add_run(session, state=TERMINAL_ERRORED, cost=1.0, hours_ago=1)
        add_run(session, state=TERMINAL_COMPLETED, cost=0.0, steps=1, outcome="nothing_to_do",
                counts=False, hours_ago=1)
        (point,) = timeseries(session, period_hours=24).points
        # Failed spend and the $0 empty poll stay out of the per-outcome
        # number but stay in the bucket's total spend.
        assert point.cost_per_processed_outcome == pytest.approx(0.03)
        assert point.processed_cost_usd == pytest.approx(0.06)
        assert point.cost_usd == pytest.approx(1.06)

    def test_tool_error_rate_is_none_without_calls(self, session):
        add_run(session, tool_calls=0, tool_errors=0, hours_ago=1)
        (point,) = timeseries(session, period_hours=24).points
        assert point.tool_error_rate is None

    def test_replays_never_move_the_chart(self, session):
        add_run(session, hours_ago=1)
        add_run(session, is_replay=True, cost=99.0, hours_ago=1)
        (point,) = timeseries(session, period_hours=24).points
        assert point.runs == 1
        assert point.cost_usd == pytest.approx(0.0)

    def test_a_quiet_system_returns_no_points_not_zero_rows(self, session):
        body = timeseries(session, period_hours=24)
        assert body.points == []


# ---------------------------------------------------------------------------
# Step errors by kind — the model outage the tool signal could not see
# ---------------------------------------------------------------------------


class TestStepErrorsByKind:
    def _demo_run(self, session, *, model_status):
        run = add_run(session, state=TERMINAL_ERRORED if model_status >= 400 else TERMINAL_COMPLETED)
        for _ in range(3):
            add_step(session, run, "mcp")
        add_step(session, run, "mcp", subtype="skill_run")
        add_step(session, run, "llm", status=model_status)
        return run

    def test_a_model_outage_is_counted_where_the_tool_signal_reads_zero(self, session):
        # Seen on the Brutor Demo System: MCP fine, every model call 502.
        for _ in range(4):
            self._demo_run(session, model_status=502)
        self._demo_run(session, model_status=200)

        s = stats(session)
        assert s.tool_errors.total_tool_errors == 0  # what the old tab showed
        kinds = {r.kind: r for r in s.step_errors.by_kind}
        assert kinds["model"].calls == 5 and kinds["model"].errors == 4
        assert kinds["model"].error_rate == pytest.approx(0.8)
        assert kinds["model"].status_codes == {"502": 4}
        assert kinds["tool"].calls == 15 and kinds["tool"].errors == 0
        # A skill run over MCP is a skill, never a tool (gateway-core rule).
        assert kinds["skill"].calls == 5
        assert s.step_errors.total_calls == 25 and s.step_errors.total_errors == 4

    def test_kinds_are_listed_in_display_order_and_only_when_called(self, session):
        run = add_run(session)
        add_step(session, run, "a2a")
        add_step(session, run, "llm")
        assert [r.kind for r in stats(session).step_errors.by_kind] == ["model", "agent"]

    def test_a_held_call_is_not_an_error(self, session):
        run = add_run(session)
        add_step(session, run, "mcp", status=202, subtype="approval_required", success=False)
        tool = stats(session).step_errors.by_kind[0]
        assert tool.kind == "tool" and tool.calls == 1 and tool.errors == 0

    def test_steps_of_runs_outside_the_window_or_replays_are_ignored(self, session):
        old = add_run(session, hours_ago=48)
        add_step(session, old, "llm", status=502)
        replay = add_run(session, is_replay=True)
        add_step(session, replay, "llm", status=502)
        assert stats(session, period_hours=24).step_errors.total_calls == 0

    def test_no_calls_is_no_rate(self, session):
        add_run(session)
        assert stats(session).step_errors.overall_error_rate is None

    def test_the_timeseries_carries_the_model_error_rate(self, session):
        for _ in range(3):
            self._demo_run(session, model_status=502)
        self._demo_run(session, model_status=200)
        ts = run_signal_service.timeseries(session, TENANT, ai_system_id=SYSTEM, period_hours=24)
        assert len(ts.points) == 1
        p = ts.points[0]
        assert (p.model_calls, p.model_errors) == (4, 3)
        assert p.model_error_rate == pytest.approx(0.75)


class TestContextSignal:
    """Context size: hand-computed, over the work population, with coverage."""

    @staticmethod
    def _sized(session, peak, growth=None, utilization=None, **kw):
        run = add_run(session, **kw)
        run.peak_context_tokens = peak
        run.context_growth = growth
        run.peak_context_utilization = utilization
        session.flush()
        return run

    def _context(self, session):
        return run_signal_service.compute(session, TENANT, ai_system_id=SYSTEM).context

    def test_nothing_measured_is_not_a_zero(self, session):
        add_run(session)
        c = self._context(session)
        assert c.runs == 1
        assert c.measured_runs == 0
        assert c.median_peak_tokens is None
        assert c.max_peak_tokens is None
        assert c.median_growth is None
        assert c.p95_utilization is None

    def test_figures_cover_measured_runs_and_state_the_coverage(self, session):
        for peak, growth in [(100, 1.0), (300, 2.0), (500, 3.0)]:
            self._sized(session, peak, growth)
        add_run(session)  # unmeasured: in `runs`, never a sample of zero
        c = self._context(session)
        assert (c.runs, c.measured_runs) == (4, 3)
        assert c.median_peak_tokens == 300
        assert c.p95_peak_tokens == 500
        assert c.max_peak_tokens == 500
        assert c.median_growth == 2.0

    def test_an_even_count_takes_the_midpoint(self, session):
        for peak in (100, 200, 300, 400):
            self._sized(session, peak)
        assert self._context(session).median_peak_tokens == 250

    def test_utilization_has_its_own_coverage(self, session):
        self._sized(session, 100, utilization=0.25)
        self._sized(session, 200, utilization=0.75)
        self._sized(session, 900)  # a model with no known window
        c = self._context(session)
        assert c.measured_runs == 3
        assert c.utilization_measured_runs == 2
        assert c.p95_utilization == 0.75
        assert c.max_utilization == 0.75

    def test_failed_runs_and_replays_are_not_the_work(self, session):
        self._sized(session, 100)
        self._sized(session, 99_999, state=TERMINAL_ERRORED)
        self._sized(session, 88_888, is_replay=True)
        c = self._context(session)
        assert (c.runs, c.measured_runs, c.max_peak_tokens) == (1, 1, 100)

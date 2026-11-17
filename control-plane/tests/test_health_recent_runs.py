# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Health judges NOW as well as the window — the "failing" state

The bug this locks in (seen in the Brutor Demo System, on trial): the model
provider became unreachable at ~16:40 and every run from then on errored on
its first model call (gateway 502). Three hours later the system still read
**Healthy 90%**: reliability is completed / finished runs over the 30-day
health window, and 163 good runs from the morning outvoted 8 errored ones
(95% — above the 80% floor). The agent backing off made it worse: an outage
produces FEWER runs, so it moves a window rate less.

The fix applies THE down rule (`ai_system_down_rule`, the one that already
judges shared services, dependency nodes and connection lines) to the
system's own latest finished runs:

  * all of the latest 3 errored ⇒ health state **failing**, reliability 0;
  * one or two of them errored ⇒ reliability capped at 0.5 ⇒ **degraded**;
  * a run that did not finish decisively (abandoned, cancelled) or that
    did no work (an empty poll, `no_work`) is not evidence either
    way and is skipped; a policy block is an errored run and counts. (Runs are written
    only once finalized, so there is no "still open" row to consider.)

Hand-computed expectations; nothing here is derived from the code under test.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from tests.run_outcome_defaults import work_outcome

from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_health import (
    HEALTH_FAILING,
    HEALTH_STATES,
    NON_GREEN_STATES,
    score_health,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
_n = count()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The scorer — pure
# ---------------------------------------------------------------------------


def _healthy(**overrides):
    base = dict(
        liveness=1.0, behaviour=1.0, reliability=0.95, cost=1.0, conformance=0.9,
        chain_integrity_pct=1.0,
    )
    base.update(overrides)
    return score_health(**base)


class TestScorer:
    def test_failing_is_a_closed_non_green_state(self):
        assert HEALTH_FAILING == "failing"
        assert HEALTH_FAILING in HEALTH_STATES
        assert HEALTH_FAILING in NON_GREEN_STATES

    def test_latest_runs_all_failed_is_failing_whatever_the_window_says(self):
        r = _healthy(reliability=0.0, recent_runs_verdict="down",
                     recent_runs_reason="The last 3 finished runs all errored.")
        assert r["health_state"] == "failing"
        assert r["health_score"] == 0.0
        assert "The last 3 finished runs all errored." in r["reasons"]

    def test_silence_and_suspension_still_come_first(self):
        assert _healthy(recent_runs_verdict="down", is_silent=True)["health_state"] == "silent"
        assert _healthy(recent_runs_verdict="down", is_suspended=True)["health_state"] == "suspended"

    def test_failing_beats_drift_and_a_dependency_cap(self):
        assert _healthy(recent_runs_verdict="down", open_drift_events=2)["health_state"] == "failing"
        r = _healthy(recent_runs_verdict="down", dependency_cap={"name": "dep", "state": "degraded"})
        assert r["health_state"] == "failing"

    def test_a_degraded_verdict_explains_itself(self):
        r = _healthy(reliability=0.5, recent_runs_verdict="degraded",
                     recent_runs_reason="1 of the last 3 finished runs errored.")
        assert r["health_state"] == "degraded"
        assert "1 of the last 3 finished runs errored." in r["reasons"]

    def test_an_ok_verdict_changes_nothing(self):
        assert _healthy(recent_runs_verdict="ok")["health_state"] == "healthy"
        assert _healthy(recent_runs_verdict=None)["health_state"] == "healthy"


# ---------------------------------------------------------------------------
# Against the database — the demo regression
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([], logger_name="health-recent-runs")
    yield harness
    harness.close()


def _system(h, kind="agent") -> str:
    gid = f"rr-{next(_n)}"
    h.seed(lambda s: s.add(ResourceGroup(
        id=gid, tenant_id=TENANT, name=gid, description=gid, hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM, system_kind=kind, owner="team", intended_use="x",
        lifecycle_stage="active",
    )))
    return gid


def _run(h, sid, ended, state, *, replay=False, no_work=False) -> str:
    rid = f"rrun-{next(_n)}"
    h.seed(lambda s: s.add(AISystemRun(
        id=rid, tenant_id=TENANT, root_task_id=f"root-{rid}", ai_system_id=sid,
        started_at=ended - timedelta(seconds=5),
        ended_at=ended, terminal_state=state,
        **{**work_outcome(state),
           **({"outcome_id": "nothing_to_do", "outcome_counts_toward_baseline": False}
              if no_work else {})}, finalized_by="explicit",
        is_replay=replay, action_count=5, llm_call_count=1, tool_call_count=3,
        error_count=int(state == "errored"), chain_integrity="client_asserted",
    )))
    return rid


def _recent(h, sid):
    from app.services.ai_system_assurance import _recent_runs

    s = h.Factory()
    try:
        return _recent_runs(s, TENANT, sid, _now() - timedelta(days=30))
    finally:
        s.close()


def _health(h, sid):
    from app.services.ai_system_assurance import compute_health

    s = h.Factory()
    try:
        return compute_health(s, s.get(ResourceGroup, sid))
    finally:
        s.close()


def _demo_outage(h) -> str:
    """The trial shape: a morning of good runs, then the provider goes away
    and every run errors (8 errored of 171)."""
    sid = _system(h)
    t0 = _now() - timedelta(hours=10)
    for i in range(163):
        _run(h, sid, t0 + timedelta(minutes=3 * i), "completed")
    for i in range(8):
        _run(h, sid, _now() - timedelta(minutes=10 * (8 - i)), "errored")
    return sid


class TestRecentRuns:
    def test_the_demo_outage_is_failing_not_healthy(self, h):
        sid = _demo_outage(h)
        recent = _recent(h, sid)
        assert recent["verdict"] == "down"
        assert recent["considered"] == 3 and recent["failed"] == 3

        scored = _health(h, sid)
        assert scored["health_state"] == "failing", scored["reasons"]
        assert scored["reliability_score"] == 0.0
        assert scored["health_score"] == 0.0
        # The window rate is still disclosed beside the verdict, not hidden.
        assert scored["recent_runs"]["window_success_rate"] == pytest.approx(163 / 171, abs=1e-6)
        assert any("last 3 finished runs" in r for r in scored["reasons"])

    def test_one_recent_error_degrades(self, h):
        sid = _system(h)
        base = _now() - timedelta(hours=1)
        for i in range(30):
            _run(h, sid, base + timedelta(minutes=i), "completed")
        _run(h, sid, _now() - timedelta(minutes=2), "errored")
        recent = _recent(h, sid)
        assert recent["verdict"] == "degraded" and recent["failed"] == 1
        scored = _health(h, sid)
        assert scored["health_state"] == "degraded", scored["reasons"]
        assert scored["reliability_score"] == 0.5

    def test_recovery_must_be_demonstrated(self, h):
        sid = _demo_outage(h)
        for i in range(3):
            _run(h, sid, _now() - timedelta(seconds=30 - i), "completed")
        assert _recent(h, sid)["verdict"] == "ok"
        assert _health(h, sid)["health_state"] != "failing"

    def test_runs_that_did_not_finish_decisively_are_skipped(self, h):
        sid = _system(h)
        t = _now() - timedelta(minutes=30)
        for i in range(3):
            _run(h, sid, t + timedelta(minutes=i), "errored")
        # Newer than the failures, but none of them says the system works.
        _run(h, sid, t + timedelta(minutes=10), "abandoned")
        _run(h, sid, t + timedelta(minutes=11), "cancelled")
        # An empty poll (a declared outcome that is not work)
        # is not evidence either.
        _run(h, sid, t + timedelta(minutes=12), "completed", no_work=True)
        _run(h, sid, t + timedelta(minutes=14), "completed", replay=True)
        recent = _recent(h, sid)
        assert recent["verdict"] == "down", recent

    def test_exhausted_is_a_failure(self, h):
        sid = _system(h)
        t = _now() - timedelta(minutes=30)
        for i in range(3):
            _run(h, sid, t + timedelta(minutes=i), "exhausted")
        assert _recent(h, sid)["verdict"] == "down"

    def test_no_finished_runs_is_no_verdict(self, h):
        sid = _system(h)
        recent = _recent(h, sid)
        assert recent["verdict"] is None and recent["considered"] == 0

    def test_the_verdict_is_stored_with_the_state(self, h):
        from app.services.ai_system_assurance import upsert_health

        sid = _demo_outage(h)
        s = h.Factory()
        try:
            row = upsert_health(s, s.get(ResourceGroup, sid))
            s.commit()
            data = row.to_dict()
        finally:
            s.close()
        assert data["health_state"] == "failing"
        assert data["recent_runs"]["verdict"] == "down"
        assert data["recent_runs"]["latest"][0]["terminal_state"] == "errored"

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Declared outcomes, against a database

Every consumer of THE run classification, hand-computed:

  * health — the demo outage with the poll as its own run: empty polls
    keep succeeding (`no_work`) while every screening errors at the model
    call ⇒ *failing*, and the reason names the error cause;
  * run facts / the assurance report — success rate, no-work overhead,
    outcomes and error causes;
  * run signals — cost per processed outcome per variant (never blended),
    wasted, no-work overhead, the outcomes and error-cause signals;
  * the envelope — rates over the classification, `max_outcome_share`,
    `avg_cost_per_outcome`;
  * conformance — every finding kind;
  * liveness — `last_processed_run_at` and `work_quiet` (never silent);
  * the admin API — outcomes GET/PUT, starter catalogue, dependency
    `expected_outcomes`, run list/detail fields, stats fields that survive
    Pydantic;
  * contracts — the closure section, the diff, catalogue-change epochs.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Optional

import pytest

from app.api.v1.endpoints.admin.ai_system_composition import create_ai_system_composition_router
from app.api.v1.endpoints.admin.ai_system_contracts import create_ai_system_contracts_router
from app.api.v1.endpoints.admin.ai_system_liveness import create_ai_system_liveness_router
from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_contract import AISystemContract
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_drift_resolution import AISystemBaselineEpoch
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.ai_system_outcome_catalogue import validate_catalogue
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
_n = count()
LOG = BrutorLogger("declared-outcomes")

#: The demo screening agent's catalogue, trimmed to what is used.
CATALOGUE = validate_catalogue([
    {"id": "application_processed", "label": "Application processed",
     "counts_toward_baseline": True,
     "variants": [
         {"id": "loan_approved", "label": "Approve",
          "expect": {"requires_capabilities": ["applications_set_recommendation"]}},
         {"id": "loan_rejected", "label": "Decline"},
         {"id": "underwriter_decision_applied", "label": "Applied",
          "expect": {"max_actions": 2, "max_model_calls": 0}},
     ]},
    {"id": "no_applications_available", "label": "Nothing to screen",
     "counts_toward_baseline": False,
     "expect": {"max_model_calls": 0, "max_cost_usd": 0,
                "forbids_capabilities": ["applications_set_recommendation"]}},
])
APPROVED = "application_processed.loan_approved"
APPLIED = "application_processed.underwriter_decision_applied"
POLL = "no_applications_available"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness(
        [
            create_ai_system_contracts_router(LOG),
            create_ai_system_runs_router(LOG),
            create_ai_system_liveness_router(LOG),
            create_ai_system_composition_router(LOG),
        ],
        logger_name="declared-outcomes",
    )
    yield harness
    harness.close()


def _system(h, *, catalogue=CATALOGUE, kind="agent", active=True) -> str:
    """An AI System with `catalogue` live and (when `active`) in its active
    contract — what the core judged its runs against."""
    sid = f"do-{next(_n)}"

    def seed(s):
        s.add(ResourceGroup(
            id=sid, tenant_id=TENANT, name=sid, description=sid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, system_kind=kind, owner="team", intended_use="x",
            lifecycle_stage="active", declared_outcomes=catalogue,
        ))
        if active:
            s.flush()
            s.add(AISystemContract(
                id=f"ctr-{sid}", tenant_id=TENANT, ai_system_id=sid, version=1,
                contract_hash=f"sha256:{sid}", resolved_closure={"outcomes": catalogue or []},
                status="active", approved_by="approver", approved_at=_now(),
            ))
    h.seed(seed)
    return sid


def _run(h, sid, *, minutes_ago: float = 5, state="completed", outcome: Optional[str] = None,
         conformance: Optional[str] = None, counts: Optional[bool] = None, cause=None,
         cost=0.0, actions=5, llm=1, caps=(), replay=False) -> str:
    """One finalized run. With `outcome` and no `conformance`, the outcome is
    judged against CATALOGUE as the core would."""
    from app.services.ai_system_outcome_catalogue import judge

    rid = f"run-{next(_n)}"
    if conformance is None:
        if state in ("completed", "completed_degraded") or outcome:
            conformance, judged_counts = judge(CATALOGUE, outcome)
            counts = judged_counts if counts is None else counts
        else:
            conformance = "unreported"
    if cause is None and state == "errored":
        cause = "client_reported"
    ended = _now() - timedelta(minutes=minutes_ago)

    def seed(s):
        s.add(AISystemRun(
            id=rid, tenant_id=TENANT, root_task_id=f"root-{rid}", ai_system_id=sid,
            started_at=ended - timedelta(seconds=5), ended_at=ended,
            terminal_state=state, finalized_by="explicit", is_replay=replay,
            action_count=actions, llm_call_count=llm, total_cost_usd=cost,
            chain_integrity="intact", outcome_id=outcome, outcome_conformance=conformance,
            outcome_counts_toward_baseline=counts if conformance == "declared" else None,
            outcome_contract_version=1 if outcome else None, error_cause=cause,
        ))
        for cap in caps:
            s.add(ProxyLog(
                id=f"log-{next(_n)}", tenant_id=TENANT, timestamp=ended, proxy_type="mcp",
                http_method="POST", endpoint="/mcp", server_id="srv",
                correlation_id=f"c-{next(_n)}", end_user_session_id="s", http_status_code=200,
                duration_ms=3, success=True, root_task_id=f"root-{rid}",
                mcp_capability_name=cap,
            ))
    h.seed(seed)
    return rid


def _session(h):
    return h.Factory()


def _call(h, fn, *args, **kw):
    s = _session(h)
    try:
        return fn(s, *args, **kw)
    finally:
        s.close()


def _system_row(s, sid):
    return s.get(ResourceGroup, sid)


# ---------------------------------------------------------------------------
# Health — the demo outage with the poll as its own run
# ---------------------------------------------------------------------------


def _recent(h, sid):
    from app.services.ai_system_assurance import _recent_runs

    return _call(h, _recent_runs, TENANT, sid, _now() - timedelta(days=30))


def _health(h, sid):
    from app.services.ai_system_assurance import compute_health

    s = _session(h)
    try:
        return compute_health(s, _system_row(s, sid))
    finally:
        s.close()


def _outage(h) -> str:
    """A morning of 20 screenings, then the provider goes: every screening
    errors at the model call while the 5-minutely polls keep 'succeeding'.
    The polls are the NEWEST runs."""
    sid = _system(h)
    for i in range(20):
        _run(h, sid, minutes_ago=600 - i * 10, outcome=APPROVED, cost=0.02, actions=10, llm=3,
             caps=["applications_set_recommendation"])
    for i in range(4):
        _run(h, sid, minutes_ago=60 - i * 10, state="errored", cause="model_unavailable",
             cost=0.0, actions=1, llm=1)
    for i in range(6):
        _run(h, sid, minutes_ago=30 - i * 5, outcome=POLL, actions=1, llm=0)
    return sid


class TestOutage:
    def test_polls_are_skipped_screenings_decide(self, h):
        sid = _outage(h)
        recent = _recent(h, sid)
        assert recent["verdict"] == "down", recent
        assert recent["considered"] == 3 and recent["failed"] == 3
        assert all(r["run_class"] == "failed" for r in recent["latest"])
        assert recent["failure_causes"] == {"model_unavailable": 3}

    def test_the_system_reads_failing_and_says_why(self, h):
        sid = _outage(h)
        scored = _health(h, sid)
        assert scored["health_state"] == "failing", scored["reasons"]
        assert scored["reliability_score"] == 0.0
        # Window: 20 success / (20 + 4 failed) — the 6 polls are in neither.
        assert scored["recent_runs"]["window_success_rate"] == pytest.approx(20 / 24)
        assert any("model unavailable" in r and "last 3 finished runs all failed" in r
                   for r in scored["reasons"]), scored["reasons"]

    def test_unreported_completions_are_deviations_that_fail_the_rule(self, h):
        sid = _system(h)
        for i in range(3):
            _run(h, sid, minutes_ago=10 - i, outcome=None)  # completed, said nothing
        recent = _recent(h, sid)
        assert recent["verdict"] == "down"
        assert recent["failure_causes"] == {"outcome_unreported": 3}
        assert any("reported no outcome" in r for r in _health(h, sid)["reasons"])

    def test_one_undeclared_outcome_degrades(self, h):
        sid = _system(h)
        for i in range(5):
            _run(h, sid, minutes_ago=60 - i, outcome=APPROVED)
        _run(h, sid, minutes_ago=1, outcome="application_processed.loan_maybe")
        recent = _recent(h, sid)
        assert recent["verdict"] == "degraded" and recent["failed"] == 1
        assert recent["failure_causes"] == {"outcome_undeclared": 1}

    def test_a_policy_block_counts_against_the_rule(self, h):
        sid = _system(h)
        for i in range(3):
            _run(h, sid, minutes_ago=10 - i, state="errored", cause="blocked_by_policy")
        _run(h, sid, minutes_ago=0.5, state="abandoned")  # not evidence
        recent = _recent(h, sid)
        assert recent["verdict"] == "down" and recent["failure_causes"] == {"blocked_by_policy": 3}
        assert any("blocked by policy" in r for r in _health(h, sid)["reasons"])

    def test_recovery_needs_real_work_not_polls(self, h):
        sid = _outage(h)
        for i in range(3):
            _run(h, sid, minutes_ago=0.5 - i * 0.1, outcome=POLL, actions=1, llm=0)
        assert _recent(h, sid)["verdict"] == "down"
        for i in range(3):
            _run(h, sid, minutes_ago=0.2 - i * 0.05, outcome=APPROVED,
                 caps=["applications_set_recommendation"])
        assert _recent(h, sid)["verdict"] == "ok"


# ---------------------------------------------------------------------------
# Run facts (the assurance report)
# ---------------------------------------------------------------------------


class TestRunFacts:
    def test_hand_computed(self, h):
        from app.services.ai_system_assurance import _run_facts

        sid = _system(h)
        for _ in range(3):
            _run(h, sid, outcome=APPROVED, cost=0.02)                       # success
        for _ in range(2):
            _run(h, sid, outcome=POLL, cost=0.0, llm=0)                     # no_work
        _run(h, sid, outcome="made_up", cost=0.01)                          # deviation
        _run(h, sid, outcome=None, cost=0.005)                              # deviation
        _run(h, sid, state="errored", cause="model_unavailable", cost=0.001)  # failed
        _run(h, sid, state="exhausted", cause="iteration_limit", cost=0.03)   # failed
        _run(h, sid, state="cancelled")                                     # not decisive
        facts = _call(h, _run_facts, TENANT, sid, _now() - timedelta(days=1))
        assert facts["total_runs"] == 10
        assert facts["run_classes"] == {
            "success": 3, "no_work": 2, "deviation": 2, "failed": 2, "not_decisive": 1,
        }
        assert facts["success_rate"] == pytest.approx(3 / 7)
        assert facts["successful_runs"] == 3
        assert facts["no_work_runs"] == 2 and facts["no_work_cost_usd"] == 0.0
        assert facts["cost_per_processed_outcome"] == pytest.approx(0.02)
        assert facts["wasted_cost_usd"] == pytest.approx(0.01 + 0.005 + 0.001 + 0.03)
        assert facts["error_causes"] == {"iteration_limit": 1, "model_unavailable": 1}
        rows = {r["outcome_id"]: r for r in facts["outcomes"]}
        assert rows[APPROVED]["runs"] == 3 and rows[APPROVED]["success"] == 3
        assert rows[POLL]["no_work"] == 2 and rows[POLL]["counts_toward_baseline"] is False
        assert rows["made_up"]["conformance"] == "undeclared" and rows["made_up"]["deviation"] == 1
        assert "client_outcomes" not in facts and "client_resolved_rate" not in facts


# ---------------------------------------------------------------------------
# Run signals
# ---------------------------------------------------------------------------


def _stats(h, sid, **kw):
    from app.services.run_signals import run_signal_service

    return _call(h, run_signal_service.compute, TENANT, ai_system_id=sid, period_hours=24, **kw)


class TestRunSignals:
    def _hour(self, h):
        """Eight $0 decisions applied, two $0.02 approvals, ten
        $0 empty polls, one $0.05 errored screening."""
        sid = _system(h)
        for _ in range(8):
            _run(h, sid, outcome=APPLIED, cost=0.0, actions=1, llm=0)
        for _ in range(2):
            _run(h, sid, outcome=APPROVED, cost=0.02, actions=10, llm=3)
        for _ in range(10):
            _run(h, sid, outcome=POLL, cost=0.0, actions=1, llm=0)
        _run(h, sid, state="errored", cause="model_unavailable", cost=0.05, actions=2)
        return sid

    def test_cost_per_variant_is_never_blended(self, h):
        cost = _stats(h, self._hour(h)).cost
        by = {r.outcome_id: r for r in cost.by_outcome}
        assert by[APPLIED].runs == 8 and by[APPLIED].cost_per_run == 0.0
        assert by[APPROVED].runs == 2 and by[APPROVED].cost_per_run == pytest.approx(0.02)
        assert by[APPROVED].median_cost_usd == pytest.approx(0.02)
        assert POLL not in by  # overhead, not work
        assert cost.processed_runs == 10
        assert cost.cost_per_processed_outcome == pytest.approx(0.004)
        assert cost.no_work_runs == 10 and cost.no_work_cost_usd == 0.0
        assert cost.wasted_cost_usd == pytest.approx(0.05)
        assert cost.total_cost_usd == pytest.approx(0.09)

    def test_run_length_over_the_work(self, h):
        actions = _stats(h, self._hour(h)).actions
        assert actions.runs == 10 and actions.finished_runs == 21
        # work: 8 × 1 action, 2 × 10 → median 1, max 10; the ten 1-action
        # polls and the 2-action error are not in it.
        assert actions.median_actions == 1 and actions.max_actions == 10
        assert sum(b.count for b in actions.distribution) == 10
        by = {r.outcome_id: r for r in actions.by_outcome}
        assert by[APPROVED].median_actions == 10 and by[APPLIED].max_actions == 1

    def test_outcomes_and_error_cause_signals(self, h):
        st = _stats(h, self._hour(h))
        assert st.outcomes.by_class == {
            "success": 10, "no_work": 10, "deviation": 0, "failed": 1, "not_decisive": 0,
        }
        assert st.outcomes.success_rate == pytest.approx(10 / 11, abs=1e-6)
        assert st.terminal_states.success_rate == st.outcomes.success_rate
        rows = {(r.outcome_id, r.run_class): r for r in st.outcomes.by_outcome}
        assert rows[(POLL, "no_work")].runs == 10
        assert rows[(POLL, "no_work")].counts_toward_baseline is False
        assert rows[(None, "failed")].conformance == "unreported"
        assert st.error_causes.failed_runs == 1
        assert [(r.cause, r.runs, r.share) for r in st.error_causes.by_cause] == [
            ("model_unavailable", 1, 1.0),
        ]

    def test_timeseries_series(self, h):
        from app.services.run_signals import run_signal_service

        sid = self._hour(h)
        _run(h, sid, outcome="made_up", cost=0.01)
        body = _call(h, run_signal_service.timeseries, TENANT, ai_system_id=sid, period_hours=24)
        (point,) = body.points
        assert (point.success, point.no_work, point.deviation, point.failed) == (10, 10, 1, 1)
        assert point.cost_per_processed_outcome == pytest.approx(0.004)
        # avg actions over the work: (8 × 1 + 2 × 10) / 10
        assert point.avg_actions == pytest.approx(2.8)

    def test_stats_survive_the_response_model(self, h):
        sid = self._hour(h)
        body = h.client.get(f"/v1/admin/ai-systems/{sid}/runs/stats?period_hours=24",
                            headers=h.headers()).json()
        assert body["cost"]["cost_per_processed_outcome"] == pytest.approx(0.004)
        assert body["cost"]["no_work_runs"] == 10
        assert {r["outcome_id"] for r in body["cost"]["by_outcome"]} == {APPLIED, APPROVED}
        assert body["outcomes"]["by_class"]["no_work"] == 10
        assert body["error_causes"]["by_cause"][0]["cause"] == "model_unavailable"
        assert "client_outcomes" not in body and "blocked_rate" not in body["terminal_states"]
        ts = h.client.get(f"/v1/admin/ai-systems/{sid}/runs/timeseries?period_hours=24",
                          headers=h.headers()).json()
        assert {"success", "no_work", "deviation", "failed"} <= set(ts["points"][0])
        assert "blocked" not in ts["points"][0]


# ---------------------------------------------------------------------------
# Run list / detail
# ---------------------------------------------------------------------------


class TestRunApi:
    def test_list_and_detail_carry_the_outcome(self, h):
        sid = _system(h)
        ok = _run(h, sid, outcome=APPROVED)
        bad = _run(h, sid, state="errored", cause="tool_failed")
        poll = _run(h, sid, outcome=POLL, llm=0)
        body = h.client.get(f"/v1/admin/ai-systems/{sid}/runs?period_hours=24",
                            headers=h.headers()).json()
        rows = {r["id"]: r for r in body["runs"]}
        assert rows[ok]["outcome_id"] == APPROVED and rows[ok]["run_class"] == "success"
        assert rows[ok]["outcome_conformance"] == "declared"
        assert rows[ok]["outcome_counts_toward_baseline"] is True
        assert rows[ok]["outcome_contract_version"] == 1
        assert rows[bad]["error_cause"] == "tool_failed" and rows[bad]["run_class"] == "failed"
        assert rows[poll]["run_class"] == "no_work"
        assert "client_outcome" not in rows[ok]
        detail = h.client.get(f"/v1/admin/runs/{ok}", headers=h.headers()).json()
        assert detail["run_class"] == "success" and detail["is_successful"] is True

    def test_run_class_filter(self, h):
        sid = _system(h)
        _run(h, sid, outcome=APPROVED)
        poll = _run(h, sid, outcome=POLL, llm=0)
        body = h.client.get(f"/v1/admin/ai-systems/{sid}/runs?period_hours=24&run_class=no_work",
                            headers=h.headers()).json()
        assert [r["id"] for r in body["runs"]] == [poll]
        r = h.client.get(f"/v1/admin/ai-systems/{sid}/runs?run_class=blocked", headers=h.headers())
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _envelope(h, sid, terms):
    from app.services.ai_system_envelope import evaluate_envelope

    s = _session(h)
    try:
        return {t["term"]: t for t in evaluate_envelope(
            s, _system_row(s, sid), period_hours=24, envelope=terms)["terms"]}
    finally:
        s.close()


class TestEnvelope:
    def test_completion_rate_reads_the_success_rate(self, h):
        sid = _system(h)
        for _ in range(18):
            _run(h, sid, outcome=APPROVED)
        for _ in range(2):
            _run(h, sid, state="errored", cause="model_unavailable")
        for _ in range(30):
            _run(h, sid, outcome=POLL, llm=0)
        t = _envelope(h, sid, {"min_completion_rate": 0.95, "max_error_rate": 0.05})
        # 18 / (18 + 2) = 0.9 — the 30 polls neither inflate it nor count as evidence.
        assert t["min_completion_rate"]["observed"] == pytest.approx(0.9)
        assert t["min_completion_rate"]["status"] == "breached"
        assert t["max_error_rate"]["observed"] == pytest.approx(0.1)

    def test_polls_are_not_evidence(self, h):
        sid = _system(h)
        for _ in range(5):
            _run(h, sid, outcome=APPROVED)
        for _ in range(40):
            _run(h, sid, outcome=POLL, llm=0)
        t = _envelope(h, sid, {"min_completion_rate": 0.5})
        assert t["min_completion_rate"]["status"] == "insufficient_evidence"

    def test_max_outcome_share(self, h):
        sid = _system(h)
        for _ in range(4):
            _run(h, sid, outcome=APPROVED)
        for _ in range(16):
            _run(h, sid, outcome=POLL, llm=0)
        _run(h, sid, state="errored")  # not a declared outcome: outside the share
        t = _envelope(h, sid, {f"max_outcome_share:{POLL}": 0.7,
                               "max_outcome_share:application_processed": 0.5})
        poll = t[f"max_outcome_share:{POLL}"]
        assert poll["observed"] == pytest.approx(16 / 20) and poll["status"] == "breached"
        assert poll["label"].endswith(POLL)
        # An outcome id without a variant covers all its variants: 4 / 20.
        assert t["max_outcome_share:application_processed"]["observed"] == pytest.approx(0.2)
        assert t["max_outcome_share:application_processed"]["status"] == "met"

    def test_avg_cost_per_outcome(self, h):
        sid = _system(h)
        for i in range(20):
            _run(h, sid, outcome=APPROVED, cost=0.01 if i < 10 else 0.03)
        for _ in range(20):
            _run(h, sid, outcome=APPLIED, cost=0.0, llm=0)
        t = _envelope(h, sid, {f"avg_cost_per_outcome:{APPROVED}": 0.015,
                               f"avg_cost_per_outcome:{APPLIED}": 0.01,
                               "max_avg_cost_per_completed_task_usd": 1.0})
        assert t[f"avg_cost_per_outcome:{APPROVED}"]["observed"] == pytest.approx(0.02)
        assert t[f"avg_cost_per_outcome:{APPROVED}"]["status"] == "breached"
        assert t[f"avg_cost_per_outcome:{APPLIED}"]["observed"] == 0.0
        # The blended figure: 0.4 / 40 processed runs.
        assert t["max_avg_cost_per_completed_task_usd"]["observed"] == pytest.approx(0.01)

    def test_parameterised_terms_validate(self):
        from app.services.ai_system_envelope import (
            EnvelopeValidationError,
            split_term,
            term_spec,
            validate_envelope,
        )

        ok = validate_envelope({f"max_outcome_share:{POLL}": 0.9, f"avg_cost_per_outcome:{APPROVED}": 0.5})
        assert ok == {f"max_outcome_share:{POLL}": 0.9, f"avg_cost_per_outcome:{APPROVED}": 0.5}
        assert split_term(f"max_outcome_share:{POLL}") == ("max_outcome_share", POLL)
        assert term_spec("avg_cost_per_outcome:x")["signal"] == "cost"
        for bad in ({"max_outcome_share": 0.5}, {"max_outcome_share:Bad!": 0.5},
                    {"max_outcome_share:a": 1.5}, {"max_error_rate:a": 0.1},
                    {"avg_cost_per_outcome:a": 0}):
            with pytest.raises(EnvelopeValidationError):
                validate_envelope(bad)

    def test_the_diff_classifies_keyed_terms(self):
        from app.services.ai_system_contract_service import diff_closures

        key = f"max_outcome_share:{POLL}"
        d = diff_closures({"operating_envelope": {key: 0.8}}, {"operating_envelope": {key: 0.9}})
        assert [e["kind"] for e in d["widened"]] == ["envelope_term_loosened"]
        assert POLL in d["widened"][0]["detail"]


# ---------------------------------------------------------------------------
# Conformance findings
# ---------------------------------------------------------------------------


def _conformance(h, sid):
    from app.services.ai_system_conformance import for_system

    s = _session(h)
    try:
        return for_system(s, _system_row(s, sid), period_hours=24, granted_resources=[])
    finally:
        s.close()


def _kinds(report):
    return {(f.kind, f.subject) for f in report.findings}


class TestConformance:
    def test_undeclared_and_unreported(self, h):
        sid = _system(h)
        _run(h, sid, outcome=APPROVED, caps=["applications_set_recommendation"])
        _run(h, sid, outcome="exfiltrated_data")
        _run(h, sid, outcome="exfiltrated_data")
        _run(h, sid, outcome=None)                               # completed, unreported
        _run(h, sid, state="errored", cause="model_unavailable")  # errored: not a finding
        rep = _conformance(h, sid)
        assert rep.undeclared_outcomes == {"exfiltrated_data": 2}
        assert rep.unreported_runs == 1
        f = {x.kind: x for x in rep.findings}
        assert f["undeclared_outcome"].severity == "high"
        assert f["undeclared_outcome"].subject == "exfiltrated_data"
        assert f["outcome_unreported"].severity == "medium"
        # 1 − (0.25 undeclared + 0.1 unreported)
        assert rep.score == pytest.approx(0.65)
        assert rep.to_dict()["undeclared_outcomes"] == {"exfiltrated_data": 2}

    def test_no_active_contract_makes_every_id_undeclared(self, h):
        sid = _system(h, active=False)
        _run(h, sid, outcome=APPROVED, conformance="undeclared")
        assert ("undeclared_outcome", APPROVED) in _kinds(_conformance(h, sid))

    def test_evidence_mismatch(self, h):
        sid = _system(h)
        good = _run(h, sid, outcome=POLL, llm=0, cost=0.0)
        bad = _run(h, sid, outcome=POLL, llm=1, cost=0.002)                   # a poll that called a model
        forbidden = _run(h, sid, outcome=POLL, llm=0, caps=["applications_set_recommendation"])
        _run(h, sid, outcome=APPROVED, caps=["applications_set_recommendation"])  # meets it
        missing = _run(h, sid, outcome=APPROVED)                               # no recommendation call
        rep = _conformance(h, sid)
        ev = rep.evidence_mismatches
        assert set(ev) == {POLL, APPROVED}
        assert ev[POLL]["runs"] == 2 and set(ev[POLL]["run_ids"]) == {bad, forbidden}
        assert good not in ev[POLL]["run_ids"]
        assert "1 model calls (expected at most 0)" in ev[POLL]["violations"]
        assert "forbidden capabilities used: ['applications_set_recommendation']" in ev[POLL]["violations"]
        assert ev[APPROVED]["run_ids"] == [missing]
        assert {k for k, _ in _kinds(rep)} >= {"outcome_evidence_mismatch"}

    def test_unused_outcome_needs_enough_traffic(self, h):
        sid = _system(h)
        for _ in range(5):
            _run(h, sid, outcome=APPROVED, caps=["applications_set_recommendation"])
        assert not any(f.kind == "unused_outcome" for f in _conformance(h, sid).findings)
        for _ in range(15):
            _run(h, sid, outcome=POLL, llm=0)
        rep = _conformance(h, sid)
        assert rep.unused_outcomes == [
            "application_processed.loan_rejected", APPLIED,
        ]
        assert all(f.severity == "low" for f in rep.findings if f.kind == "unused_outcome")
        # An unused declared outcome is a precision hint (the contract may be
        # broader than needed), not a fault: it must not lower the score.
        # Penalising it marked the demo down 35 points on its first day for
        # declaring rare-but-legitimate ends, and would push owners to
        # declare fewer outcomes — so the rare end later arrives undeclared.
        from app.services.ai_system_conformance import (
            UNDECLARED_CLIENT_PENALTY, UNUSED_RESOURCE_PENALTY,
        )
        without_outcome_hints = max(0.0, 1.0
            - (len(rep.undeclared_clients) + len(rep.undeclared_client_systems)) * UNDECLARED_CLIENT_PENALTY
            - len(rep.unused_resources) * UNUSED_RESOURCE_PENALTY)
        assert rep.score == pytest.approx(without_outcome_hints)

    def test_unexpected_delegate_outcome(self, h):
        caller = _system(h)
        delegate = _system(h)
        other = _system(h)
        rid = _run(h, caller, outcome=APPROVED, caps=["applications_set_recommendation"])
        rid2 = _run(h, caller, outcome=APPROVED, caps=["applications_set_recommendation"])
        rid3 = _run(h, caller, outcome=APPROVED, caps=["applications_set_recommendation"])
        rid4 = _run(h, caller, outcome=APPROVED, caps=["applications_set_recommendation"])

        def seed(s):
            s.add(AISystemDependency(
                id=f"dep-{next(_n)}", tenant_id=TENANT, ai_system_id=caller, role="shared_service",
                depends_on_system_id=delegate, criticality="critical",
                expected_outcomes=["screening_completed.clear", "screening_completed.hit"],
            ))
            # A dependency with no list is never judged.
            s.add(AISystemDependency(
                id=f"dep-{next(_n)}", tenant_id=TENANT, ai_system_id=caller, role="shared_service",
                depends_on_system_id=other, criticality="critical",
            ))
            for run_id, target, outcome, oid in (
                (rid, delegate, "answered", "screening_completed.clear"),
                (rid2, delegate, "answered", "screening_completed.maybe"),
                (rid3, delegate, "answered", None),
                (rid4, delegate, "failed", None),  # did not answer: nothing to judge
                (rid, other, "answered", "anything.at_all"),
            ):
                at = _now() - timedelta(minutes=3)
                s.add(AISystemRunSegment(
                    id=f"seg-{next(_n)}", run_id=run_id, tenant_id=TENANT,
                    root_task_id=f"root-{run_id}", root_system_id=caller,
                    caller_system_id=caller, segment_system_id=target,
                    first_action_at=at, last_action_at=at, outcome=outcome,
                    outcome_id=oid, outcome_conformance="declared" if oid else "unreported",
                ))
        h.seed(seed)
        rep = _conformance(h, caller)
        assert rep.unexpected_delegate_outcomes == {
            delegate: {"": 1, "screening_completed.maybe": 1},
        }
        f = [x for x in rep.findings if x.kind == "unexpected_delegate_outcome"]
        assert len(f) == 1 and f[0].subject == delegate and f[0].severity == "medium"
        assert "screening_completed.maybe ×1" in f[0].detail


# ---------------------------------------------------------------------------
# Liveness — work, not just activity
# ---------------------------------------------------------------------------


def _liveness(h, sid):
    from app.services.ai_system_liveness_state import liveness_state

    s = _session(h)
    try:
        return liveness_state(s, _system_row(s, sid), window_hours=24)
    finally:
        s.close()


def _expect(h, sid, bound):
    h.seed(lambda s: s.add(AISystemLivenessExpectation(
        id=f"live-{sid}", tenant_id=TENANT, ai_system_id=sid, mode="scheduled",
        expected_min_runs=1, window_seconds=3600, grace_seconds=300, enabled=True,
        max_quiet_processed_seconds=bound,
    )))


class TestLiveness:
    def test_polling_without_work_is_work_quiet_not_silent(self, h):
        sid = _system(h)
        _expect(h, sid, 3600)
        processed = _run(h, sid, minutes_ago=180, outcome=APPROVED)
        for i in range(6):
            _run(h, sid, minutes_ago=50 - i * 5, outcome=POLL, llm=0)
        v = _liveness(h, sid)
        assert v.state == "alive" and v.score == 1.0 and not v.is_silent
        assert v.work_quiet is True
        assert 180 * 60 - 60 <= v.processed_quiet_for_seconds <= 180 * 60 + 60
        assert v.last_processed_run_at is not None and v.last_processed_run_at < v.last_run_at
        assert "no processed outcome" in v.work_quiet_reason
        assert processed

    def test_recent_work_is_not_quiet(self, h):
        sid = _system(h)
        _expect(h, sid, 3600)
        _run(h, sid, minutes_ago=10, outcome=APPROVED)
        _run(h, sid, minutes_ago=1, outcome=POLL, llm=0)
        assert _liveness(h, sid).work_quiet is False

    def test_never_processed(self, h):
        sid = _system(h)
        _expect(h, sid, 3600)
        _run(h, sid, minutes_ago=1, outcome=POLL, llm=0)
        v = _liveness(h, sid)
        assert v.work_quiet is True and v.last_processed_run_at is None

    def test_not_checked_without_a_bound(self, h):
        sid = _system(h)
        _expect(h, sid, None)
        _run(h, sid, minutes_ago=1, outcome=POLL, llm=0)
        v = _liveness(h, sid)
        assert v.work_quiet is False and v.max_quiet_processed_seconds is None

    def test_the_api_carries_it(self, h):
        sid = _system(h)
        r = h.client.put(f"/v1/admin/ai-systems/{sid}/liveness",
                         json={"mode": "scheduled", "max_quiet_processed_seconds": 7200},
                         headers=h.headers())
        assert r.status_code == 200 and r.json()["max_quiet_processed_seconds"] == 7200
        _run(h, sid, minutes_ago=1, outcome=POLL, llm=0)
        body = h.client.get(f"/v1/admin/ai-systems/{sid}/liveness", headers=h.headers()).json()
        assert body["work_quiet"] is True and body["is_silent"] is False
        assert body["expectation"]["max_quiet_processed_seconds"] == 7200
        r = h.client.put(f"/v1/admin/ai-systems/{sid}/liveness",
                         json={"max_quiet_processed_seconds": 10}, headers=h.headers())
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Admin API — the catalogue
# ---------------------------------------------------------------------------

DEMO_DOC = [
    {"id": "application_processed", "label": "Application processed", "counts_toward_baseline": True,
     "variants": [
         {"id": "underwriter_hold_renewed", "label": "Hold renewed",
          "expect": {"max_actions": 2, "max_model_calls": 0,
                     "requires_capabilities": ["applications_set_recommendation"]}},
         {"id": "underwriter_hold_closed", "label": "Hold closed",
          "expect": {"max_model_calls": 0, "requires_capabilities": ["applications_add_note"]}},
     ]},
    {"id": "applications_dispatched", "label": "Dispatched", "counts_toward_baseline": False,
     "expect": {"max_model_calls": 0, "max_cost_usd": 0,
                "forbids_capabilities": ["applications_set_recommendation", "applications_add_note",
                                         "applications_hand_off"]}},
    {"id": "no_applications_available", "label": "Nothing to screen", "counts_toward_baseline": False,
     "expect": {"max_model_calls": 0, "max_cost_usd": 0,
                "forbids_capabilities": ["applications_set_recommendation"]}},
]


class TestOutcomesApi:
    def test_get_reads_live_and_active(self, h):
        sid = _system(h)
        body = h.client.get(f"/v1/admin/ai-systems/{sid}/outcomes", headers=h.headers()).json()
        assert body["ai_system_id"] == sid
        assert [o["id"] for o in body["outcomes"]] == ["application_processed", POLL]
        assert body["active_contract_version"] == 1
        assert [o["id"] for o in body["active_outcomes"]] == ["application_processed", POLL]
        assert APPROVED in body["reportable_ids"]

    def test_put_replaces_normalises_and_drifts(self, h):
        sid = _system(h)
        r = h.client.put(f"/v1/admin/ai-systems/{sid}/outcomes", json={"outcomes": DEMO_DOC},
                         headers=h.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert [o["id"] for o in body["outcomes"]] == [
            "application_processed", "applications_dispatched", POLL,
        ]
        assert [v["id"] for v in body["outcomes"][0]["variants"]] == [
            "underwriter_hold_closed", "underwriter_hold_renewed",
        ]
        assert body["outcomes"][1]["expect"]["forbids_capabilities"] == [
            "applications_add_note", "applications_hand_off", "applications_set_recommendation",
        ]
        again = h.client.get(f"/v1/admin/ai-systems/{sid}/outcomes", headers=h.headers()).json()
        assert again["outcomes"] == body["outcomes"]
        # The active contract still carries the old catalogue until a new one is promoted.
        assert [o["id"] for o in again["active_outcomes"]] == ["application_processed", POLL]

    def test_put_null_clears(self, h):
        sid = _system(h)
        r = h.client.put(f"/v1/admin/ai-systems/{sid}/outcomes", json={"outcomes": None},
                         headers=h.headers())
        assert r.status_code == 200 and r.json()["outcomes"] is None
        assert r.json()["reportable_ids"] == []

    def test_422_with_field_paths_and_nothing_saved(self, h):
        sid = _system(h)
        r = h.client.put(f"/v1/admin/ai-systems/{sid}/outcomes",
                         json={"outcomes": [{"id": "a", "label": "A", "counts_toward_baseline": True,
                                             "expect": {"max_tokens": 1}}]},
                         headers=h.headers())
        assert r.status_code == 422
        assert r.json()["detail"][0]["loc"] == ["outcomes", 0, "expect", "max_tokens"]
        body = h.client.get(f"/v1/admin/ai-systems/{sid}/outcomes", headers=h.headers()).json()
        assert [o["id"] for o in body["outcomes"]] == ["application_processed", POLL]

    def test_a_bare_list_is_refused(self, h):
        sid = _system(h)
        r = h.client.put(f"/v1/admin/ai-systems/{sid}/outcomes", json=DEMO_DOC, headers=h.headers())
        assert r.status_code == 422

    def test_tenant_isolation_and_non_systems(self, h):
        sid = _system(h)
        r = h.client.get(f"/v1/admin/ai-systems/{sid}/outcomes", headers=h.headers("t2"))
        assert r.status_code == 404
        gid = f"grp-{next(_n)}"
        h.seed(lambda s: s.add(ResourceGroup(
            id=gid, tenant_id=TENANT, name=gid, description=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.TEAM,
        )))
        assert h.client.get(f"/v1/admin/ai-systems/{gid}/outcomes", headers=h.headers()).status_code == 422


class TestDependencyExpectedOutcomes:
    def test_create_update_and_list(self, h):
        caller, target = _system(h), _system(h)
        url = f"/v1/admin/ai-systems/{caller}/dependencies"
        r = h.client.post(url, json={
            "role": "shared_service", "depends_on_system_id": target,
            "expected_outcomes": ["screening_completed.hit", "screening_completed.clear",
                                  "screening_completed.hit"],
        }, headers=h.headers())
        assert r.status_code == 201, r.text
        edge = r.json()
        assert edge["expected_outcomes"] == ["screening_completed.clear", "screening_completed.hit"]
        listed = h.client.get(url, headers=h.headers()).json()["dependencies"]
        assert listed[0]["expected_outcomes"] == edge["expected_outcomes"]
        r = h.client.patch(f"{url}/{edge['id']}", json={"expected_outcomes": ["Bad Id"]},
                           headers=h.headers())
        assert r.status_code == 422
        r = h.client.patch(f"{url}/{edge['id']}", json={"expected_outcomes": None},
                           headers=h.headers())
        assert r.status_code == 200 and r.json()["expected_outcomes"] is None


# ---------------------------------------------------------------------------
# Contracts — closure section, diff, catalogue-change epochs
# ---------------------------------------------------------------------------


class TestContracts:
    def test_closure_mint_diff_promote_epochs(self, h):
        from app.services import ai_system_contract_service as contracts
        from app.services.ai_system_closure import resolve_closure
        from app.services.ai_system_judgement_exclusion import epoch_floor

        sid = _system(h, active=False)
        s = _session(h)
        try:
            system = _system_row(s, sid)
            closure = resolve_closure(s, system)
            assert closure["outcomes"] == CATALOGUE
            v1, created = contracts.mint(s, system, created_by="a")
            assert created
            contracts.approve(s, v1, approved_by="b")
            contracts.promote(s, v1)
            s.commit()

            # The poll now counts (narrowing) and the applied variant's bound rises
            # (widening); a new variant is a widening too.
            doc = [dict(o) for o in CATALOGUE]
            processed = dict(doc[0])
            processed["variants"] = [dict(v) for v in processed["variants"]] + [
                {"id": "loan_referred", "label": "Refer"}]
            for v in processed["variants"]:
                if v["id"] == "underwriter_decision_applied":
                    v["expect"] = {"max_actions": 4, "max_model_calls": 0}
            poll = dict(doc[1], counts_toward_baseline=True)
            system.declared_outcomes = validate_catalogue([processed, poll])
            s.flush()
            v2, created = contracts.mint(s, system, created_by="a")
            assert created and v2.version == 2
            kinds_w = {(e["kind"], e["id"]) for e in v2.diff_summary["widened"]}
            kinds_n = {(e["kind"], e["id"]) for e in v2.diff_summary["narrowed"]}
            assert ("outcome_variant_added", "application_processed.loan_referred") in kinds_w
            assert ("outcome_expect_loosened", APPLIED) in kinds_w
            assert ("outcome_baseline_flag", POLL) in kinds_n
            contracts.approve(s, v2, approved_by="b")
            contracts.promote(s, v2)
            s.commit()
            epochs = s.query(AISystemBaselineEpoch).filter(
                AISystemBaselineEpoch.ai_system_id == sid).all()
            got = sorted((e.metric_key, e.bucket, e.kind) for e in epochs)
            expected = sorted(
                [(m, b, "catalogue_change")
                 for b in (APPLIED, POLL)
                 for m in ("action_count", "cost_per_run", "duration_ms", "tool_call_count",
                           "peak_context_tokens", "context_growth")]
                + [("outcome_mix", "all", "catalogue_change")]
            )
            assert got == expected
            # Bucket epochs re-learn the core's per-outcome baselines only: the
            # control plane's all-runs floors ignore them.
            assert epoch_floor(s, TENANT, sid, "action_count") is None
            assert epoch_floor(s, TENANT, sid, "outcome_mix") is not None
        finally:
            s.close()

    def test_dependency_expected_outcomes_in_closure_and_diff(self, h):
        from app.services.ai_system_closure import resolve_closure
        from app.services.ai_system_contract_service import diff_closures

        caller, target = _system(h), _system(h)
        dep_id = f"dep-{next(_n)}"
        h.seed(lambda s: s.add(AISystemDependency(
            id=dep_id, tenant_id=TENANT, ai_system_id=caller, role="shared_service",
            depends_on_system_id=target, criticality="critical",
            expected_outcomes=["b.y", "a.x"],
        )))
        s = _session(h)
        try:
            before = resolve_closure(s, _system_row(s, caller))
            assert before["dependencies"][0]["expected_outcomes"] == ["a.x", "b.y"]
            edge = s.get(AISystemDependency, dep_id)
            edge.expected_outcomes = ["a.x", "c.z"]
            s.flush()
            after = resolve_closure(s, _system_row(s, caller))
        finally:
            s.close()
        d = diff_closures(before, after)
        assert [e.get("added") for e in d["widened"]] == [["c.z"]]
        assert [e.get("removed") for e in d["narrowed"]] == [["b.y"]]


class TestEvidenceUsesTheStampedContract:
    def test_a_run_is_held_to_the_contract_it_was_judged_against(self, h):
        """The core judges a run against the contract stamped on it, else the
        active one; the evidence an outcome needs is read from the same catalogue."""
        sid = _system(h)
        old = validate_catalogue([{"id": POLL, "label": "Poll", "counts_toward_baseline": False,
                                   "expect": {"max_model_calls": 2}}])
        h.seed(lambda s: s.add(AISystemContract(
            id=f"ctr-old-{sid}", tenant_id=TENANT, ai_system_id=sid, version=0,
            contract_hash=f"sha256:old-{sid}", resolved_closure={"outcomes": old},
            status="superseded", approved_by="approver", approved_at=_now(),
        )))
        stamped = _run(h, sid, outcome=POLL, llm=1)   # judged under v0: 1 ≤ 2
        current = _run(h, sid, outcome=POLL, llm=1)   # no stamp: active, max 0

        def stamp(s):
            s.get(AISystemRun, stamped).contract_id = f"ctr-old-{sid}"
        h.seed(stamp)
        ev = _conformance(h, sid).evidence_mismatches
        assert ev[POLL]["run_ids"] == [current]

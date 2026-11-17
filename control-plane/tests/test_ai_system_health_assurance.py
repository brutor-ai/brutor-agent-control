# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Health score, conformance and the Assurance Report

Two judgements carry this phase, and both are places where the comfortable
implementation is the wrong one:

  * **Health is worst-of, not an average.** A system that is cheap, fast
    and dead is not 80% healthy. Averaging is how a dashboard reports a
    reassuring number for a system nobody can call.

  * **Absence of evidence is never scored as good news.** Too little
    history, a call chain we could only partly observe, a system with no
    traffic at all — each is `unknown` or `None`, never a green tick. This
    is the single most tempting corner to cut and the one that would make
    the whole report worthless to an auditor.

The report's `not_covered` section is tested as carefully as its claims,
because it is the reason the claims are credible.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.database.models.ai_system_health import (
    AISystemAssuranceReport,
    AISystemHealth,
    COMPONENTS,
    HEALTH_DEGRADED,
    HEALTH_DRIFTING,
    HEALTH_HEALTHY,
    HEALTH_LEARNING,
    HEALTH_SILENT,
    HEALTH_STALLED,
    HEALTH_STATES,
    HEALTH_SUSPENDED,
    HEALTH_UNKNOWN,
    MIN_CHAIN_INTEGRITY_FOR_SCORE,
    NON_GREEN_STATES,
    REPORT_TRIGGERS,
    score_health,
)
from app.services.ai_system_conformance import (
    MIN_CALLS_FOR_CLIENT_FINDING,
    evaluate,
)


def healthy(**overrides):
    base = dict(
        liveness=1.0, behaviour=1.0, reliability=1.0, cost=1.0,
        conformance=1.0, chain_integrity_pct=1.0,
    )
    base.update(overrides)
    return score_health(**base)


# ---------------------------------------------------------------------------
# Worst-of
# ---------------------------------------------------------------------------


class TestWorstOf:
    def test_a_healthy_system_scores_one(self):
        r = healthy()
        assert r["health_state"] == HEALTH_HEALTHY
        assert r["health_score"] == 1.0

    def test_the_score_is_the_worst_component_not_the_mean(self):
        # Four components at 1.0 and one at 0.2 averages to 0.84 — a number
        # that would render green for a system with a serious problem.
        r = healthy(behaviour=0.2)
        assert r["health_score"] == 0.2
        assert r["health_state"] == HEALTH_DEGRADED

    def test_a_cheap_fast_dead_system_is_not_eighty_percent_healthy(self):
        r = score_health(
            liveness=0.0, reliability=1.0, cost=1.0, conformance=1.0,
            chain_integrity_pct=1.0, is_silent=True,
        )
        assert r["health_state"] == HEALTH_SILENT
        assert r["health_score"] == 0.0

    def test_every_component_is_kept_alongside_the_total(self):
        # A score nobody can decompose is a score nobody trusts.
        r = healthy(behaviour=0.5)
        for component in COMPONENTS:
            assert f"{component}_score" in r

    def test_a_missing_component_does_not_drag_the_score_down(self):
        # None means "not measured", not "measured as zero".
        r = healthy(cost=None)
        assert r["health_score"] == 1.0


# ---------------------------------------------------------------------------
# Absence of evidence
# ---------------------------------------------------------------------------


class TestUnknownIsNotGreen:
    def test_no_signals_at_all_is_unknown_with_no_score(self):
        r = score_health()
        assert r["health_state"] == HEALTH_UNKNOWN
        assert r["health_score"] is None

    def test_a_broken_chain_forces_unknown(self):
        # A score computed from a third of the evidence is a guess wearing a
        # number's clothes.
        r = healthy(chain_integrity_pct=0.4)
        assert r["health_state"] == HEALTH_UNKNOWN
        assert r["health_score"] is None
        assert "40%" in r["reasons"][0]

    def test_the_integrity_floor_is_a_real_threshold(self):
        assert healthy(chain_integrity_pct=MIN_CHAIN_INTEGRITY_FOR_SCORE)[
            "health_state"
        ] == HEALTH_HEALTHY
        assert healthy(chain_integrity_pct=MIN_CHAIN_INTEGRITY_FOR_SCORE - 0.01)[
            "health_state"
        ] == HEALTH_UNKNOWN

    def test_unknown_and_learning_are_both_non_green(self):
        assert HEALTH_UNKNOWN in NON_GREEN_STATES
        assert HEALTH_LEARNING in NON_GREEN_STATES
        assert HEALTH_HEALTHY not in NON_GREEN_STATES

    def test_a_row_with_no_integrity_is_not_trustworthy(self):
        row = AISystemHealth(
            id="h1", tenant_id="t1", ai_system_id="sys",
            health_state=HEALTH_HEALTHY, chain_integrity_pct=None,
        )
        assert not row.is_trustworthy


# ---------------------------------------------------------------------------
# State precedence
# ---------------------------------------------------------------------------


class TestStatePrecedence:
    def test_suspended_beats_a_perfect_score(self):
        # Its components describe traffic that is no longer being allowed.
        r = healthy(is_suspended=True)
        assert r["health_state"] == HEALTH_SUSPENDED
        assert r["health_score"] is None

    def test_a_broken_chain_beats_everything_except_suspension(self):
        assert healthy(chain_integrity_pct=0.1, is_silent=True)[
            "health_state"
        ] == HEALTH_UNKNOWN
        assert healthy(chain_integrity_pct=0.1, is_suspended=True)[
            "health_state"
        ] == HEALTH_SUSPENDED

    def test_silence_beats_drift(self):
        # A system that has stopped calling is the more urgent fact.
        r = healthy(is_silent=True, open_drift_events=3)
        assert r["health_state"] == HEALTH_SILENT

    def test_stalling_beats_drift(self):
        r = healthy(is_stalled=True, open_drift_events=3)
        assert r["health_state"] == HEALTH_STALLED

    def test_drift_beats_learning(self):
        # A system can be both still learning and already drifting; the
        # finding is what an operator needs to see.
        r = healthy(is_learning=True, open_drift_events=1)
        assert r["health_state"] == HEALTH_DRIFTING

    def test_a_measured_failure_beats_learning(self):
        # A system failing 5 of 6 runs while its behaviour baselines are
        # still learning must not read "Still learning ... not yet a clean
        # bill" — an outage hidden behind the mildest non-green state.
        # Reliability needs no baseline: what is measured is reported, and
        # learning is said alongside it.
        r = healthy(is_learning=True, reliability=1 / 6)
        assert r["health_state"] == HEALTH_DEGRADED
        assert "reliability" in r["reasons"][0]
        assert any("learning" in reason for reason in r["reasons"])

    def test_learning_is_reported_as_not_a_clean_bill(self):
        r = healthy(is_learning=True)
        assert r["health_state"] == HEALTH_LEARNING
        assert "not yet a clean bill" in r["reasons"][0]

    def test_every_non_healthy_state_explains_itself(self):
        # A state with no explanation sends someone hunting through four
        # other panels.
        for kwargs in (
            {"is_suspended": True},
            {"is_silent": True},
            {"is_stalled": True},
            {"recent_runs_verdict": "down"},
            {"open_drift_events": 2},
            {"is_learning": True},
            {"behaviour": 0.3},
            {"chain_integrity_pct": 0.2},
        ):
            r = healthy(**kwargs)
            assert r["reasons"], f"{r['health_state']} gave no reason"

    def test_a_healthy_system_needs_no_explanation(self):
        assert healthy()["reasons"] == []

    def test_the_degraded_reason_names_the_weakest_component(self):
        r = healthy(reliability=0.4)
        assert "reliability" in r["reasons"][0]

    def test_an_unknown_state_is_rejected_by_the_model(self):
        with pytest.raises(ValueError):
            AISystemHealth(
                id="h1", tenant_id="t1", ai_system_id="sys",
                health_state="probably_fine",
            )

    def test_the_state_vocabulary_is_closed(self):
        assert HEALTH_STATES == {
            "healthy", "learning", "degraded", "drifting",
            "stalled", "failing", "silent", "suspended", "unknown",
        }


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


class TestConformance:
    def _eval(self, **kwargs):
        base = dict(
            declared_clients=["sdk"],
            observed={"sdk": 100},
            granted_resources=["gpt-4o"],
            used=["gpt-4o"],
        )
        base.update(kwargs)
        return evaluate(**base)

    def test_a_conforming_system_scores_one(self):
        assert self._eval().score == 1.0
        assert self._eval().findings == []

    def test_an_undeclared_client_is_a_finding(self):
        r = self._eval(observed={"sdk": 100, "curl": 5})
        assert r.undeclared_clients == ["curl"]
        assert r.findings[0].kind == "undeclared_client"
        assert r.score < 1.0

    def test_an_undeclared_client_on_a_high_risk_system_is_escalated(self):
        # It is the finding a risk owner reads first.
        normal = self._eval(observed={"sdk": 100, "curl": 5})
        high = self._eval(observed={"sdk": 100, "curl": 5}, risk_tier="high")
        assert normal.findings[0].severity == "medium"
        assert high.findings[0].severity == "high"

    def test_a_declared_client_that_never_called_is_a_low_finding(self):
        r = self._eval(declared_clients=["sdk", "portal"], observed={"sdk": 100})
        assert r.unused_declared_clients == ["portal"]
        assert any(f.kind == "declared_client_absent" for f in r.findings)

    def test_a_quiet_week_does_not_produce_absent_client_findings(self):
        # Below the traffic floor, "absent" and "quiet" are indistinguishable.
        r = self._eval(declared_clients=["sdk", "portal"], observed={"sdk": 3})
        assert r.unused_declared_clients == []

    def test_an_unused_grant_is_a_least_privilege_finding(self):
        r = self._eval(granted_resources=["gpt-4o", "gpt-5"], used=["gpt-4o"])
        assert r.unused_resources == ["gpt-5"]
        finding = next(f for f in r.findings if f.kind == "unused_grant")
        assert "blast radius" in finding.detail

    def test_undeclared_access_outweighs_unused_grants(self):
        # Ten stale bindings must not outrank one unknown caller.
        stale = self._eval(
            granted_resources=["a", "b", "c", "gpt-4o"], used=["gpt-4o"]
        )
        unknown = self._eval(observed={"sdk": 100, "curl": 1})
        assert unknown.score < stale.score

    def test_a_system_with_no_traffic_has_no_conformance_score(self):
        # It has not demonstrated conformance; it has demonstrated nothing.
        r = self._eval(observed={})
        assert r.score is None

    def test_declaring_nothing_is_itself_reported(self):
        r = self._eval(declared_clients=[], observed={"curl": 50})
        assert any(f.kind == "no_declared_clients" for f in r.findings)
        # ...and it is not treated as a violation, because there is nothing
        # to violate.
        assert r.undeclared_clients == []

    def test_the_scope_limit_is_stated_on_every_report(self):
        # A system reachable by another path would show clean here.
        assert "through the gateway" in self._eval().scope_note

    def test_the_traffic_floor_is_a_real_threshold(self):
        assert MIN_CALLS_FOR_CLIENT_FINDING > 0
        below = self._eval(
            declared_clients=["sdk", "portal"],
            observed={"sdk": MIN_CALLS_FOR_CLIENT_FINDING - 1},
        )
        at = self._eval(
            declared_clients=["sdk", "portal"],
            observed={"sdk": MIN_CALLS_FOR_CLIENT_FINDING},
        )
        assert below.unused_declared_clients == []
        assert at.unused_declared_clients == ["portal"]

    def test_the_score_never_goes_below_zero(self):
        r = self._eval(observed={f"c{i}": 5 for i in range(20)})
        assert r.score == 0.0


# ---------------------------------------------------------------------------
# The report model
# ---------------------------------------------------------------------------


class TestAssuranceReportModel:
    def test_the_summary_can_omit_the_blob(self):
        # A history listing should not carry twenty full reports.
        row = AISystemAssuranceReport(
            id="asr-1", tenant_id="t1", ai_system_id="sys",
            period_hours=720, report_json={"big": "payload"},
            trigger="manual",
        )
        assert "report_json" not in row.to_dict(include_report=False)
        assert row.to_dict()["report_json"] == {"big": "payload"}

    def test_an_unknown_trigger_is_rejected(self):
        with pytest.raises(ValueError):
            AISystemAssuranceReport(
                id="asr-1", tenant_id="t1", ai_system_id="sys",
                period_hours=720, report_json={}, trigger="whenever",
            )

    def test_the_trigger_vocabulary_includes_the_scheduled_job(self):
        # The scheduled report job is a trigger of its own.
        assert "scheduled" in REPORT_TRIGGERS


class TestProvenanceCoverageSplit:
    """Coverage gates evidence; provenance is disclosed, never a gate.

    Regression: `compute_health` fed the scorer the signed-only share, so a
    system whose agents correlate runs by id would flip to `unknown` for
    following the documented integration path. And the report described
    asserted runs as "partial records", which is simply false — they are
    complete observations with weaker linkage provenance.
    """

    def test_run_facts_reports_the_split(self):
        from app.services.ai_system_assurance import _run_facts  # noqa: SLF001

        import inspect

        src = inspect.getsource(_run_facts)
        for key in ("observed_pct", "asserted_pct", "chain_integrity_pct"):
            assert key in src, f"_run_facts must report {key}"

    def test_health_gate_uses_coverage_not_provenance(self):
        import inspect

        from app.services import ai_system_assurance as a

        src = inspect.getsource(a.compute_health)
        assert 'chain_integrity_pct=runs.get("observed_pct")' in src, (
            "the integrity floor is an evidence-sufficiency gate and must take "
            "coverage (intact + client_asserted), not the signed-only share"
        )

    def test_not_covered_discloses_assertion_without_calling_it_partial(self):
        import inspect

        from app.services import ai_system_assurance as a

        src = inspect.getsource(a)
        assert "client-asserted" in src
        # The old wording described asserted runs as partial records.
        assert "intact call chain; the remainder are partial records" not in src



# ---------------------------------------------------------------------------
# Recommended controls (report spec 1.1) — the rule ladder, unit level.
# The HTTP tests cover the wiring; these cover the branches that need
# shaped conformance and evidence inputs.
# ---------------------------------------------------------------------------


class TestRecommendedControlRules:
    def _call(self, system=None, **overrides):
        from types import SimpleNamespace

        from app.services.ai_system_assurance import _recommended_controls

        kw = dict(
            runs={"total_runs": 10, "asserted_pct": 0.0,
                  "chain_integrity_pct": 1.0},
            contract_compliance={"under_signature": True, "envelope": None},
            open_drift=[],
            conf=SimpleNamespace(undeclared_clients=[], unused_resources=[]),
            suite=object(),
            expectation=object(),
            evidence_rows=[],
        )
        kw.update(overrides)
        return _recommended_controls(
            system or SimpleNamespace(eu_ai_act_risk_tier=None), **kw
        )

    def test_a_fully_governed_system_gets_no_recommendations(self):
        # The section must be able to go quiet — a report that always
        # recommends something teaches operators to ignore it.
        assert self._call() == []

    def test_an_undeclared_client_is_named_as_the_subject(self):
        from types import SimpleNamespace

        recs = self._call(
            conf=SimpleNamespace(
                undeclared_clients=["curl-dev"], unused_resources=[]
            )
        )
        (rec,) = [r for r in recs if r["control"] == "declare_or_block_client"]
        assert rec["subject"] == "curl-dev"
        assert rec["severity"] == "medium"

    def test_an_undeclared_client_on_a_high_risk_system_is_high_severity(self):
        from types import SimpleNamespace

        recs = self._call(
            system=SimpleNamespace(eu_ai_act_risk_tier="high"),
            conf=SimpleNamespace(
                undeclared_clients=["curl-dev"], unused_resources=[]
            ),
            # keep the FRIA rule out of this test's way
            evidence_rows=[SimpleNamespace(
                kind="impact_assessment", is_expired=False, independent=True
            )],
        )
        (rec,) = [r for r in recs if r["control"] == "declare_or_block_client"]
        assert rec["severity"] == "high"

    def test_unused_grants_are_a_least_privilege_note(self):
        from types import SimpleNamespace

        recs = self._call(
            conf=SimpleNamespace(
                undeclared_clients=[], unused_resources=["mcp:jira", "skill:pdf"]
            )
        )
        (rec,) = [r for r in recs if r["control"] == "revoke_unused_grants"]
        assert rec["severity"] == "low"
        assert "2" in rec["reason"]

    def test_asserted_chains_recommend_sdk_signing(self):
        recs = self._call(
            runs={"total_runs": 10, "asserted_pct": 0.3,
                  "chain_integrity_pct": 0.7}
        )
        assert any(r["control"] == "sign_run_chains" for r in recs)

    def test_no_runs_does_not_nag_about_signing(self):
        # With zero runs there is no chain to sign; the missing-evidence
        # story belongs to not_covered and the liveness rule.
        recs = self._call(
            runs={"total_runs": 0, "asserted_pct": 0.0,
                  "chain_integrity_pct": None},
            expectation=None,
        )
        assert not any(r["control"] == "sign_run_chains" for r in recs)
        assert any(r["control"] == "configure_liveness" for r in recs)

    def test_high_risk_without_impact_assessment_is_high_severity(self):
        from types import SimpleNamespace

        recs = self._call(system=SimpleNamespace(eu_ai_act_risk_tier="high"))
        (rec,) = [r for r in recs if r["control"] == "attach_impact_assessment"]
        assert rec["severity"] == "high"

    def test_an_unexpired_impact_assessment_satisfies_the_rule(self):
        from types import SimpleNamespace

        recs = self._call(
            system=SimpleNamespace(eu_ai_act_risk_tier="high"),
            evidence_rows=[SimpleNamespace(
                kind="impact_assessment", is_expired=False, independent=True
            )],
        )
        assert not any(
            r["control"] == "attach_impact_assessment" for r in recs
        )

    def test_self_assessed_evidence_only_is_a_low_severity_note(self):
        from types import SimpleNamespace

        recs = self._call(
            evidence_rows=[SimpleNamespace(
                kind="evaluation", is_expired=False, independent=False
            )],
        )
        (rec,) = [
            r for r in recs if r["control"] == "attach_independent_evidence"
        ]
        assert rec["severity"] == "low"

    def test_insufficient_envelope_terms_recommend_run_end_reporting(self):
        recs = self._call(
            contract_compliance={
                "under_signature": True,
                "envelope": {"insufficient": 2, "breached": 0, "terms": []},
            }
        )
        (rec,) = [r for r in recs if r["control"] == "report_run_ends"]
        assert "2" in rec["reason"]

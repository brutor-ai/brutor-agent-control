# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The operating envelope + artifact standards (a147_operating_envelope), end to end:

  * **Terms are validated, evaluated honestly, and hash-pinned.** Unknown
    terms are refused; a rate over too few runs is `insufficient_evidence`,
    never `met`; declaring an envelope drifts the contract until a new
    version is approved.
  * **The diff reads direction.** Loosening a bound (or removing a term)
    is a widening the approver must read; adding or tightening is not.
  * **Breaches become findings and floor health.** One open `contract`
    event per standing breach, auto-resolved when healed; cost gets a
    declared target.
  * **The report is a specified document with a verdict.** Spec fields
    validate against the vendored JSON Schema; the verdict is worst-first
    and insufficient evidence never reads as assured; the certificate
    export carries the digest and an honest `signed: false` when no audit
    key is reachable.
"""

from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from tests.run_outcome_defaults import work_outcome

from tests.admin_endpoint_harness import build_admin_harness

from app import standards
from app.api.v1.endpoints.admin.ai_system_contracts import (
    create_ai_system_contracts_router,
)
from app.api.v1.endpoints.admin.ai_system_response import (
    create_ai_system_response_router,
)
from app.api.v1.endpoints.admin.standards import create_standards_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_drift import DRIFT_CONTRACT, AISystemDriftEvent
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services import ai_system_envelope as env


@pytest.fixture()
def h():
    log = BrutorLogger("test-envelope-standards")
    harness = build_admin_harness(
        [
            create_ai_system_contracts_router(log),
            create_ai_system_response_router(log),
            create_standards_router(log),
        ],
        logger_name="test-envelope-standards",
    )
    yield harness
    harness.close()


def _add_system(h, gid, tenant="t1", **kwargs):
    def fn(s):
        s.add(
            ResourceGroup(
                id=gid, tenant_id=tenant, name=gid, hierarchy_level=0,
                inherit_resources=True, is_active=True, is_system=False,
                group_type=GroupType.AI_SYSTEM, system_kind="agent", **kwargs,
            )
        )

    h.seed(fn)
    return gid


def _seed_runs(h, sid, *, n, state="completed", cost=0.01, approvals=0,
               tenant="t1", days_ago=1):
    def fn(s):
        base = datetime.now(timezone.utc) - timedelta(days=days_ago)
        for i in range(n):
            started = base + timedelta(minutes=i)
            s.add(
                AISystemRun(
                    id=f"run-{sid}-{state}-{i}",
                    tenant_id=tenant,
                    root_task_id=f"root-{sid}-{state}-{i}",
                    ai_system_id=sid,
                    started_at=started,
                    ended_at=started + timedelta(minutes=1),
                    terminal_state=state, **work_outcome(state),
                    finalized_by="explicit",
                    total_cost_usd=cost,
                    approval_request_count=approvals,
                    is_replay=False,
                )
            )

    h.seed(fn)


def _put_envelope(h, sid, body, tenant="t1"):
    return h.client.put(
        f"/v1/admin/ai-systems/{sid}/envelope",
        json={"operating_envelope": body},
        headers=h.headers(tenant),
    )


def _mint_and_approve(h, sid, tenant="t1"):
    r = h.client.post(
        f"/v1/admin/ai-systems/{sid}/contracts", headers=h.headers(tenant)
    )
    assert r.status_code == 200, r.text
    cid = r.json()["contract"]["id"]
    assert (
        h.client.post(
            f"/v1/admin/contracts/{cid}/approve", headers=h.headers(tenant), json={}
        ).status_code
        == 200
    )
    assert (
        h.client.post(
            f"/v1/admin/contracts/{cid}/promote", headers=h.headers(tenant)
        ).status_code
        == 200
    )
    return r.json()["contract"]["version"]


# ---------------------------------------------------------------------------
# Validation + hash pinning
# ---------------------------------------------------------------------------


class TestEnvelopeDeclaration:
    def test_round_trip(self, h):
        sid = _add_system(h, "sys-env")
        r = _put_envelope(h, sid, {"max_cost_per_run_usd": 0.15,
                                   "min_completion_rate": 0.9})
        assert r.status_code == 200, r.text
        assert r.json()["operating_envelope"] == {
            "max_cost_per_run_usd": 0.15, "min_completion_rate": 0.9,
        }

    def test_unknown_term_is_refused(self, h):
        sid = _add_system(h, "sys-envbad")
        r = _put_envelope(h, sid, {"min_correct_decisions": 0.95})
        assert r.status_code == 422
        assert "unknown envelope term" in r.text

    def test_rate_domain_is_enforced(self, h):
        sid = _add_system(h, "sys-envdom")
        assert _put_envelope(h, sid, {"max_error_rate": 1.5}).status_code == 422
        assert _put_envelope(h, sid, {"max_monthly_cost_usd": -5}).status_code == 422

    def test_declaring_an_envelope_drifts_the_contract(self, h):
        sid = _add_system(h, "sys-envdrift", owner="team", intended_use="x")
        _mint_and_approve(h, sid)
        r = _put_envelope(h, sid, {"max_cost_per_run_usd": 0.10})
        assert r.status_code == 200
        assert r.json()["config_drifted"] is True

    def test_a_system_without_an_envelope_keeps_its_hash(self, h):
        """The fleet must not read config-drifted the day a147_operating_envelope ships."""
        sid = _add_system(h, "sys-envnull", owner="team", intended_use="x")
        _mint_and_approve(h, sid)
        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/contracts", headers=h.headers()
        )
        assert r.json()["config_drifted"] is False


class TestEnvelopeDiff:
    def _diff_for(self, h, sid):
        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/contracts", headers=h.headers()
        )
        newest = r.json()["contracts"][0]
        return newest["diff_summary"]

    def test_loosening_is_widening_and_tightening_is_not(self, h):
        sid = _add_system(h, "sys-diff", owner="team", intended_use="x")
        _put_envelope(h, sid, {"max_cost_per_run_usd": 0.10})
        _mint_and_approve(h, sid)

        _put_envelope(h, sid, {"max_cost_per_run_usd": 0.50})  # loosened
        h.client.post(f"/v1/admin/ai-systems/{sid}/contracts", headers=h.headers())
        diff = self._diff_for(h, sid)
        assert any(
            e["kind"] == "envelope_term_loosened" for e in diff["widened"]
        ), diff
        assert diff["has_widened_permissions"] is True

        _put_envelope(h, sid, {"max_cost_per_run_usd": 0.05})  # tightened
        h.client.post(f"/v1/admin/ai-systems/{sid}/contracts", headers=h.headers())
        diff = self._diff_for(h, sid)
        assert any(
            e["kind"] == "envelope_term_tightened" for e in diff["narrowed"]
        ), diff

    def test_removing_a_term_is_a_removed_promise(self, h):
        sid = _add_system(h, "sys-diffrm", owner="team", intended_use="x")
        _put_envelope(h, sid, {"max_error_rate": 0.02})
        _mint_and_approve(h, sid)
        _put_envelope(h, sid, {})
        h.client.post(f"/v1/admin/ai-systems/{sid}/contracts", headers=h.headers())
        diff = self._diff_for(h, sid)
        assert any(
            e["kind"] == "envelope_term_removed" for e in diff["widened"]
        ), diff


# ---------------------------------------------------------------------------
# Evaluation honesty
# ---------------------------------------------------------------------------


class TestEvaluation:
    def _eval(self, h, sid, envelope, period_hours=720):
        s = h.Factory()
        try:
            system = s.query(ResourceGroup).filter_by(id=sid).one()
            return env.evaluate_envelope(
                s, system, period_hours=period_hours, envelope=envelope
            )
        finally:
            s.close()

    def test_too_few_runs_is_insufficient_never_met(self, h):
        sid = _add_system(h, "sys-thin")
        _seed_runs(h, sid, n=3)
        out = self._eval(h, sid, {"min_completion_rate": 0.9})
        assert out["terms"][0]["status"] == env.STATUS_INSUFFICIENT

    def test_a_met_cost_bound_reads_met(self, h):
        sid = _add_system(h, "sys-met")
        _seed_runs(h, sid, n=30, cost=0.01)
        out = self._eval(h, sid, {"max_cost_per_run_usd": 0.10})
        assert out["terms"][0]["status"] == env.STATUS_MET

    def test_a_breached_bound_reads_breached_with_magnitude_severity(self, h):
        sid = _add_system(h, "sys-breach")
        _seed_runs(h, sid, n=30, cost=0.30)  # 3x the bound → high
        out = self._eval(h, sid, {"max_cost_per_run_usd": 0.10})
        term = out["terms"][0]
        assert term["status"] == env.STATUS_BREACHED
        assert term["severity"] == "high"

    def test_monthly_spend_needs_no_minimum_evidence(self, h):
        sid = _add_system(h, "sys-monthly")
        _seed_runs(h, sid, n=2, cost=400.0)
        out = self._eval(h, sid, {"max_monthly_cost_usd": 500.0})
        assert out["terms"][0]["status"] == env.STATUS_BREACHED  # 800 > 500

    def test_no_declaration_evaluates_to_none(self, h):
        sid = _add_system(h, "sys-none")
        assert self._eval(h, sid, None) is None


class TestViolationEvents:
    def test_breach_emits_one_open_event_and_healing_resolves_it(self, h):
        sid = _add_system(h, "sys-events")
        _seed_runs(h, sid, n=30, cost=0.30)

        s = h.Factory()
        try:
            system = s.query(ResourceGroup).filter_by(id=sid).one()
            evaluation = env.evaluate_envelope(
                s, system, period_hours=720,
                envelope={"max_cost_per_run_usd": 0.10},
            )
            assert env.emit_envelope_violations(s, system, evaluation) == 1
            # A standing breach must not multiply on re-evaluation.
            assert env.emit_envelope_violations(s, system, evaluation) == 0
            open_events = (
                s.query(AISystemDriftEvent)
                .filter_by(ai_system_id=sid, drift_class=DRIFT_CONTRACT)
                .all()
            )
            assert len(open_events) == 1
            assert open_events[0].metric_key == "envelope:max_cost_per_run_usd"

            healed = env.evaluate_envelope(
                s, system, period_hours=720,
                envelope={"max_cost_per_run_usd": 5.00},
            )
            env.emit_envelope_violations(s, system, healed)
            s.flush()
            assert open_events[0].status == "resolved"
            assert open_events[0].resolved_by == "system:envelope-evaluator"
            s.commit()
        finally:
            s.close()


# ---------------------------------------------------------------------------
# The report as a specified document
# ---------------------------------------------------------------------------


def _report(h, sid, period_hours=720):
    r = h.client.get(
        f"/v1/admin/ai-systems/{sid}/assurance",
        params={"period_hours": period_hours},
        headers=h.headers(),
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestReportStandard:
    def test_the_live_report_validates_against_the_vendored_schema(self, h):
        sid = _add_system(h, "sys-spec", owner="team", intended_use="x")
        _seed_runs(h, sid, n=25)
        report = _report(h, sid)
        jsonschema.validate(report, standards.load_schema(standards.REPORT_SPEC))
        assert report["spec"] == standards.REPORT_SPEC

    def test_the_coverage_attestation_is_part_of_the_standard(self, h):
        """The population proof is a documented section of the exported
        standard, not an undeclared extra riding on additionalProperties."""
        sid = _add_system(h, "sys-cov", owner="team", intended_use="x")
        _seed_runs(h, sid, n=5)
        report = _report(h, sid)
        jsonschema.validate(report, standards.load_schema(standards.REPORT_SPEC))
        cov = report["coverage"]
        assert cov["basis"] in ("unmeasured", "stale", "measured")
        assert cov["statement"]

    def test_an_unmeasured_estate_yields_a_null_percentage_in_the_report(self, h):
        """The schema permits null and the report must actually use it. A
        harness tenant has no collectors, so this is the unmeasured path
        end to end — the one case where a number would be a lie."""
        sid = _add_system(h, "sys-cov2", owner="team", intended_use="x")
        _seed_runs(h, sid, n=5)
        cov = _report(h, sid)["coverage"]
        assert cov["basis"] == "unmeasured"
        assert cov["coverage_pct"] is None
        assert any("unmeasured" in g for g in _report(h, sid)["not_covered"])

    def test_verdict_precedence_breach_beats_everything(self, h):
        sid = _add_system(h, "sys-vio", owner="team", intended_use="x")
        _put_envelope(h, sid, {"max_cost_per_run_usd": 0.10})
        _mint_and_approve(h, sid)
        _seed_runs(h, sid, n=30, cost=0.30)
        report = _report(h, sid)
        assert report["verdict"]["status"] == "violations"
        assert report["contract_compliance"]["under_signature"] is True

    def test_no_runs_is_insufficient_evidence_never_assured(self, h):
        sid = _add_system(h, "sys-silent", owner="team", intended_use="x")
        report = _report(h, sid)
        assert report["verdict"]["status"] == "insufficient_evidence"

    def test_clean_period_with_signed_envelope_is_assured(self, h):
        sid = _add_system(h, "sys-clean", owner="team", intended_use="x")
        _put_envelope(h, sid, {"max_cost_per_run_usd": 1.0})
        _mint_and_approve(h, sid)
        _seed_runs(h, sid, n=30, cost=0.01)
        report = _report(h, sid)
        assert report["verdict"]["status"] == "assured"
        assert "remained within its approved operating envelope" in (
            report["verdict"]["conclusion"]
        )

    def test_an_unsigned_declaration_is_flagged_not_evaluated(self, h):
        sid = _add_system(h, "sys-unsigned", owner="team", intended_use="x")
        _put_envelope(h, sid, {"max_cost_per_run_usd": 0.10})
        _seed_runs(h, sid, n=30, cost=0.30)  # would breach, but not signed
        report = _report(h, sid)
        cc = report["contract_compliance"]
        assert cc["under_signature"] is False
        assert cc["envelope"] is None
        assert "unsigned_declaration" in cc
        assert report["verdict"]["status"] != "violations"

    def test_exceptions_and_changes_sections_exist_and_count(self, h):
        sid = _add_system(h, "sys-exc", owner="team", intended_use="x")
        _mint_and_approve(h, sid)
        _seed_runs(h, sid, n=25, approvals=1)
        report = _report(h, sid)
        assert report["exceptions"]["approval_requests"] == 25
        assert report["changes"]["contract_versions_minted"] == [1]


class TestContractExportAndCertificate:
    def test_contract_export_validates_and_is_self_contained(self, h):
        sid = _add_system(h, "sys-ctr", owner="team", intended_use="x")
        _put_envelope(h, sid, {"max_error_rate": 0.02})
        version = _mint_and_approve(h, sid)
        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/contracts/{version}/export",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        doc = r.json()
        jsonschema.validate(doc, standards.load_schema(standards.CONTRACT_SPEC))
        assert doc["closure"]["operating_envelope"] == {"max_error_rate": 0.02}
        assert doc["contract"]["hash"]

    def test_certificate_export_is_honest_about_signing(self, h, monkeypatch):
        # Pin an unreachable proxy: sign_manifest defaults to
        # http://127.0.0.1:8100, and a developer running a local core
        # (which signs report manifests since the a147_operating_envelope work) would
        # otherwise turn this into a test of their laptop, not the code.
        monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", "http://127.0.0.1:1")
        sid = _add_system(h, "sys-cert", owner="team", intended_use="x")
        _seed_runs(h, sid, n=25)
        cap = h.client.post(
            f"/v1/admin/ai-systems/{sid}/assurance", headers=h.headers(), json={}
        )
        assert cap.status_code == 200, cap.text
        assert cap.json()["signed"] is False  # proxy unreachable by design

        rid = cap.json()["id"]
        r = h.client.get(
            f"/v1/admin/ai-systems/{sid}/assurance/history/{rid}/export",
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        cert = r.json()
        assert cert["attestation"]["signed"] is False
        assert "report_sha256" in cert["attestation"]
        jsonschema.validate(
            cert["report"], standards.load_schema(standards.REPORT_SPEC)
        )

    def test_foreign_tenant_cannot_export(self, h):
        sid = _add_system(h, "sys-iso2", owner="team", intended_use="x")
        version = _mint_and_approve(h, sid)
        assert (
            h.client.get(
                f"/v1/admin/ai-systems/{sid}/contracts/{version}/export",
                headers=h.headers("t2"),
            ).status_code
            == 404
        )


class TestSchemasServed:
    def test_both_schemas_are_served_and_valid(self, h):
        r = h.client.get("/v1/admin/standards", headers=h.headers())
        assert r.status_code == 200
        slugs = {s["slug"] for s in r.json()["standards"]}
        assert slugs == {"ai-system-contract", "ai-system-assurance-report"}
        for slug in slugs:
            rs = h.client.get(
                f"/v1/admin/standards/{slug}/schema", headers=h.headers()
            )
            assert rs.status_code == 200
            jsonschema.Draft202012Validator.check_schema(rs.json())

    def test_unknown_standard_is_404(self, h):
        assert (
            h.client.get(
                "/v1/admin/standards/ai-vibes/schema", headers=h.headers()
            ).status_code
            == 404
        )


class TestHealthWiring:
    def test_a_breached_cost_term_floors_the_cost_component(self, h):
        from app.services.ai_system_assurance import compute_health

        sid = _add_system(h, "sys-hw", owner="team", intended_use="x")
        _put_envelope(h, sid, {"max_cost_per_run_usd": 0.10})
        _mint_and_approve(h, sid)
        _seed_runs(h, sid, n=30, cost=0.30)

        s = h.Factory()
        try:
            system = s.query(ResourceGroup).filter_by(id=sid).one()
            scored = compute_health(s, system)
            assert scored["cost_score"] == 0.0  # 3x = high severity
            assert scored["envelope"]["breached"] == 1
        finally:
            s.close()


# ---------------------------------------------------------------------------
# a153_envelope_per_run — per-run ceilings and the honest cost vocabulary
#
# Regression suite for the defect this work exists to fix: a term named
# `max_cost_per_run_usd` that was graded as a 30-day MEAN, so one runaway run
# hid inside thousands of cheap ones and the term still read "met".
# ---------------------------------------------------------------------------


def _seed_one_expensive_run(h, sid, *, cost, tenant="t1"):
    """One outlier alongside whatever else the system did."""
    def fn(s):
        started = datetime.now(timezone.utc) - timedelta(hours=2)
        s.add(
            AISystemRun(
                id=f"run-{sid}-outlier",
                tenant_id=tenant,
                root_task_id=f"root-{sid}-outlier",
                ai_system_id=sid,
                started_at=started,
                ended_at=started + timedelta(minutes=9),
                terminal_state="completed", **work_outcome("completed"),
                finalized_by="explicit",
                total_cost_usd=cost,
                llm_call_count=400,
                total_tokens=900_000,
                max_delegation_depth=7,
                duration_ms=540_000,
                active_duration_ms=540_000,
                is_replay=False,
            )
        )

    h.seed(fn)


def _seed_timed_run(h, sid, *, rid, elapsed_ms, awaiting_ms, tenant="t1"):
    """One run with an explicit elapsed / awaiting-a-human split (a182_run_active_duration)."""
    def fn(s):
        started = datetime.now(timezone.utc) - timedelta(days=1)
        s.add(
            AISystemRun(
                id=f"run-{sid}-{rid}",
                tenant_id=tenant,
                root_task_id=f"root-{sid}-{rid}",
                ai_system_id=sid,
                started_at=started,
                ended_at=started + timedelta(milliseconds=elapsed_ms),
                terminal_state="completed", **work_outcome("completed"),
                finalized_by="explicit",
                total_cost_usd=0.01,
                duration_ms=elapsed_ms,
                awaiting_human_ms=awaiting_ms,
                active_duration_ms=elapsed_ms - awaiting_ms,
                approval_request_count=1 if awaiting_ms else 0,
                is_replay=False,
            )
        )

    h.seed(fn)


class TestDurationCeilingExcludesTimeAwaitingAHuman:
    """a182_run_active_duration — the per-run duration ceiling bounds the system's own execution.

    Counting the wait would let a decision approved 13 h after it was held
    breach a 300 s ceiling (severity high), and the response policy would
    demote the AI System's autonomy — for having asked a person.
    """

    def test_a_run_held_13h_for_approval_does_not_breach_a_300s_ceiling(self, h):
        sid = "sys-held"
        _add_system(h, sid)
        thirteen_h = 13 * 3600 * 1000
        _seed_timed_run(h, sid, rid="held", elapsed_ms=thirteen_h + 41_000, awaiting_ms=thirteen_h)
        assert _put_envelope(h, sid, {env.TERM_MAX_DURATION_PER_RUN: 300}).status_code == 200
        _mint_and_approve(h, sid)

        (t,) = _report(h, sid)["contract_compliance"]["envelope"]["terms"]
        assert t["status"] == env.STATUS_MET, t
        assert t["observed"] == pytest.approx(41.0)

    def test_a_run_active_400s_breaches_a_300s_ceiling(self, h):
        sid = "sys-slow"
        _add_system(h, sid)
        # Active time is what counts — even with a short hold on the side.
        _seed_timed_run(h, sid, rid="slow", elapsed_ms=460_000, awaiting_ms=60_000)
        assert _put_envelope(h, sid, {env.TERM_MAX_DURATION_PER_RUN: 300}).status_code == 200
        _mint_and_approve(h, sid)

        (t,) = _report(h, sid)["contract_compliance"]["envelope"]["terms"]
        assert t["status"] == env.STATUS_BREACHED
        assert t["observed"] == pytest.approx(400.0)

    def test_the_term_says_it_excludes_time_awaiting_a_human(self):
        spec = env.ENVELOPE_TERMS[env.TERM_MAX_DURATION_PER_RUN]
        assert spec["label"] == "Active time per run"
        assert "awaiting a human" in spec["help"]


class TestCostVocabularyIsHonest:
    def test_the_mean_term_exists_under_its_own_name(self):
        spec = env.ENVELOPE_TERMS[env.TERM_MAX_AVG_COST_PER_TASK]
        assert spec["label"] == "Average cost per completed task"
        assert spec["enforced"] is False

    def test_the_per_run_term_is_a_ceiling_not_an_average(self):
        # The name finally means what it says.
        spec = env.ENVELOPE_TERMS[env.TERM_MAX_COST_PER_RUN]
        assert env.TERM_MAX_COST_PER_RUN == "max_cost_per_run_usd"
        assert spec["enforced"] is True

    def test_the_averaging_alias_is_gone(self):
        # One name, one meaning. A key that grades as a mean while reading
        # like a ceiling is the defect this work removed.
        assert not hasattr(env, "TERM_LEGACY_MEAN_COST")

    def test_a_runaway_hides_from_the_mean_but_not_from_the_ceiling(self, h):
        """The whole defect, reproduced end to end.

        30 runs at $0.01 plus one $5.00 run: the mean is ~$0.17, under a
        $0.50 average bound, while the ceiling for one run is breached 10x.
        """
        _add_system(h, "sys-cap")
        _seed_runs(h, "sys-cap", n=30, cost=0.01)
        _seed_one_expensive_run(h, "sys-cap", cost=5.00)

        r = _put_envelope(h, "sys-cap", {
            env.TERM_MAX_AVG_COST_PER_TASK: 0.50,
            env.TERM_MAX_COST_PER_RUN: 0.50,
        })
        assert r.status_code == 200, r.text
        _mint_and_approve(h, "sys-cap")

        terms = {t["term"]: t for t in _report(h, "sys-cap")["contract_compliance"]["envelope"]["terms"]}
        assert terms[env.TERM_MAX_AVG_COST_PER_TASK]["status"] == env.STATUS_MET
        assert terms[env.TERM_MAX_COST_PER_RUN]["status"] == env.STATUS_BREACHED
        assert terms[env.TERM_MAX_COST_PER_RUN]["observed"] == pytest.approx(5.00)


class TestPerRunTermsAreGradedAgainstTheWorstRun:
    @pytest.mark.parametrize(
        "term,bound,expected_observed",
        [
            ("max_llm_calls_per_run", 50, 400),
            ("max_tokens_per_run", 100_000, 900_000),
            ("max_delegation_depth_per_run", 3, 7),
            ("max_duration_seconds_per_run", 60, 540),
        ],
    )
    def test_each_enforced_term_reports_the_worst_run(
        self, h, term, bound, expected_observed
    ):
        sid = f"sys-{term}"
        _add_system(h, sid)
        _seed_runs(h, sid, n=25, cost=0.01)  # a well-behaved majority
        _seed_one_expensive_run(h, sid, cost=0.02)
        assert _put_envelope(h, sid, {term: bound}).status_code == 200
        _mint_and_approve(h, sid)

        (t,) = _report(h, sid)["contract_compliance"]["envelope"]["terms"]
        assert t["status"] == env.STATUS_BREACHED
        assert t["observed"] == pytest.approx(expected_observed)
        assert t["enforced"] is True
        assert t["detail"], "a breach with no detail is not actionable"

    def test_a_ceiling_needs_only_one_run_not_the_rate_evidence_floor(self, h):
        # Rates need 20 finished runs before they mean anything. A ceiling
        # does not: one run over the bound IS the breach, and suppressing it
        # because the window was quiet would hide the very outlier the
        # ceiling exists to catch.
        _add_system(h, "sys-single")
        _seed_one_expensive_run(h, "sys-single", cost=9.99)
        assert _put_envelope(
            h, "sys-single", {env.TERM_MAX_COST_PER_RUN: 1.00}
        ).status_code == 200
        _mint_and_approve(h, "sys-single")

        (t,) = _report(h, "sys-single")["contract_compliance"]["envelope"]["terms"]
        assert t["status"] == env.STATUS_BREACHED

    def test_no_runs_is_insufficient_evidence_never_met(self, h):
        _add_system(h, "sys-quiet")
        assert _put_envelope(
            h, "sys-quiet", {env.TERM_MAX_COST_PER_RUN: 1.00}
        ).status_code == 200
        _mint_and_approve(h, "sys-quiet")
        (t,) = _report(h, "sys-quiet")["contract_compliance"]["envelope"]["terms"]
        assert t["status"] == env.STATUS_INSUFFICIENT


class TestEnforcedTermValidation:
    @pytest.mark.parametrize("term", sorted(env.ENFORCED_TERMS))
    def test_every_enforced_term_is_declarable(self, h, term):
        _add_system(h, f"sys-v-{term}")
        bound = 0.5 if term.endswith("_usd") else 10
        envelope = {term: bound}
        if term == env.TERM_NEW_RUNS_WINDOW_SECONDS:
            # The window of a maximum: declared with it.
            envelope[env.TERM_MAX_NEW_RUNS_PER_WINDOW] = 3
        assert _put_envelope(h, f"sys-v-{term}", envelope).status_code == 200

    @pytest.mark.parametrize(
        "value", [0, -1, 0.5, "ten", None],
    )
    def test_a_count_ceiling_refuses_nonsense(self, h, value):
        # A zero or fractional ceiling would either refuse every call or be
        # silently truncated. Both are worse than refusing the write.
        _add_system(h, "sys-bad")
        r = _put_envelope(h, "sys-bad", {"max_llm_calls_per_run": value})
        assert r.status_code == 422, r.text

    def test_the_vocabulary_still_refuses_unknown_terms(self, h):
        _add_system(h, "sys-unknown")
        r = _put_envelope(h, "sys-unknown", {"max_vibes_per_run": 3})
        assert r.status_code == 422

    def test_enforced_and_graded_terms_are_distinguishable_by_the_form(self):
        # The UI groups the form by this flag; a term that lies about
        # whether it stops traffic is a control an operator misreads.
        for key, spec in env.ENVELOPE_TERMS.items():
            assert "enforced" in spec, f"{key} does not say whether it enforces"
            assert "help" in spec and spec["help"], f"{key} has no explanation"
            assert (key in env.ENFORCED_TERMS) == spec["enforced"], key


class TestContractVersionExposesItsClosure:
    """An approver must be able to read what they are signing.

    The list endpoint omits `resolved_closure` deliberately; the per-version
    endpoint is how the UI shows the models, servers, skills, guardrails and
    merged limits a version froze.
    """

    def _mint(self, h, sid="sys-cl"):
        _add_system(h, sid)
        r = h.client.post(
            f"/v1/admin/ai-systems/{sid}/contracts", headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text
        return r.json()["contract"]["version"]

    def test_the_list_omits_the_closure(self, h):
        v = self._mint(h, "sys-list")
        body = h.client.get(
            "/v1/admin/ai-systems/sys-list/contracts", headers=h.headers("t1")
        ).json()
        assert body["contracts"], "expected at least the minted version"
        assert "resolved_closure" not in body["contracts"][0] or (
            body["contracts"][0].get("resolved_closure") is None
        )
        assert v == 1

    def test_the_version_endpoint_returns_the_stamped_closure(self, h):
        v = self._mint(h, "sys-one")
        body = h.client.get(
            f"/v1/admin/ai-systems/sys-one/contracts/{v}", headers=h.headers("t1")
        ).json()
        closure = body["resolved_closure"]
        assert closure is not None, "the approver cannot sign what they cannot read"
        # The shape the panel groups by.
        assert "composition" in closure
        for key in ("llm_models", "mcp_servers", "skills", "guardrails"):
            assert key in closure["composition"], key
        assert "limits" in closure and "governance" in closure
        assert "autonomy_level" in closure
        assert body["short_hash"]

    def test_current_still_wins_over_the_version_route(self, h):
        # `/contracts/current` is declared first; a `{version}` route above it
        # would swallow the literal and 422 on the int parse.
        self._mint(h, "sys-cur")
        r = h.client.get(
            "/v1/admin/ai-systems/sys-cur/contracts/current", headers=h.headers("t1")
        )
        assert r.status_code in (200, 404), r.text
        assert r.status_code != 422

    def test_an_unknown_version_is_404(self, h):
        self._mint(h, "sys-404")
        r = h.client.get(
            "/v1/admin/ai-systems/sys-404/contracts/99", headers=h.headers("t1")
        )
        assert r.status_code == 404

    def test_another_tenant_cannot_read_the_closure(self, h):
        self._mint(h, "sys-iso")
        r = h.client.get(
            "/v1/admin/ai-systems/sys-iso/contracts/1", headers=h.headers("t2")
        )
        assert r.status_code == 404


class TestEnforcedOnlyTerms:
    """A term the gateway enforces and the run ledger has no fact for is
    reported as enforced and not graded: it does not fail the report, and
    it is not counted as met, breached or unevaluable."""

    @pytest.mark.parametrize(
        "envelope",
        [
            {"max_actions_per_run": 5},
            {"max_new_runs_per_window": 3, "new_runs_window_seconds": 600},
            {env.TERM_MAX_COST_PER_SESSION: 2.5},
        ],
    )
    def test_the_report_carries_it_as_not_graded(self, h, envelope):
        sid = _add_system(h, f"sys-eo-{len(envelope)}-{sorted(envelope)[0]}")
        assert _put_envelope(h, sid, envelope).status_code == 200
        _mint_and_approve(h, sid)
        evaluation = _report(h, sid)["contract_compliance"]["envelope"]
        assert {t["term"]: t["status"] for t in evaluation["terms"]} == {term: env.STATUS_NOT_GRADED for term in envelope}
        assert all(t["enforced"] and t["observed"] is None and t["detail"] for t in evaluation["terms"])
        assert (evaluation["breached"], evaluation["insufficient"]) == (0, 0)

    def test_the_form_is_told_which_terms_are_not_graded(self):
        for key, spec in env.ENVELOPE_TERMS.items():
            assert spec.get("graded", True) == (key not in env.ENFORCED_ONLY_TERMS), key

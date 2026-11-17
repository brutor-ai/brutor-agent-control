# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composite AI Systems — health, responses and contracts, behavioural spec

Real app, real auth, full-schema SQLite (the shared admin harness). One class
per rule, each guarding the CLASS of bug:

  * Service signals — a system whose activity is segments gets every
    signal from segments; run-basis baselines never make a service ready;
    oversight is not applicable (never 0 %) when it raised no hold; the
    period verdict is evidenced by served calls.
  * Dependency health — a critical dependency that is not healthy caps
    the root at degraded (reason `dependency`, naming it and its failing
    signal), transitively; a cycle cannot sustain a failure nobody has;
    degradable adds a note and a fallback check; external is assumed with
    boundary metrics. The list, the Composition tab and the health endpoint
    read the same status.
  * Responses — a failing critical dependency is an open `dependency`
    finding on the root that a response policy targets; it resolves itself.
  * Contracts — the closure's `dependencies` section; a service
    promotion drifts a FOLLOWING caller, not a pinned one; the caller's diff
    carries the service's own classified diff.
  * Composition checks — each new rule fires exactly once and clears;
    the checks raise and resolve inbox items; the Composition tab, the list
    and the inbox read one computation.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import re
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from tests.run_outcome_defaults import work_outcome
from ulid import ULID

from app.api.v1.endpoints.admin.ai_system_composition import (
    create_ai_system_composition_router,
)
from app.api.v1.endpoints.admin.ai_system_response import create_ai_system_response_router
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_identity import (
    AgentGrant,
    AgentIdentity,
    ResourceGroupAgentMember,
)
from app.database.models.ai_system_contract import AISystemContract, hash_closure
from app.database.models.ai_system_drift import AISystemBaseline, AISystemDriftEvent
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_response_policy import AISystemResponsePolicy
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness
from tests.source_trees import python_sources, source_label

T1 = "t1"
_n = count(1)


def _now():
    """Seed-time now — never import time."""
    return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("composite-health-tests")
    harness = build_admin_harness(
        [
            create_ai_system_composition_router(log),
            create_ai_systems_router(log),
            create_ai_system_response_router(log),
            create_resource_groups_router(UsageLimitEnforcer(logger=log), log),
        ],
        logger_name="composite-health-tests",
    )
    yield harness
    harness.close()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def system(h, gid, *, clients=None, kind="agent", tier=None, on_demand=False):
    def fn(s):
        s.add(ResourceGroup(
            id=gid, tenant_id=T1, name=gid, description=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, system_kind=kind, owner="team",
            intended_use="x", lifecycle_stage="active", intended_client_systems=clients,
            eu_ai_act_risk_tier=tier,
        ))
        if on_demand:
            s.flush()
            s.add(AISystemLivenessExpectation(
                id=f"live-{gid}", tenant_id=T1, ai_system_id=gid, mode="on_demand",
            ))
    h.seed(fn)
    return gid


def card(h, cid):
    h.seed(lambda s: s.add(AgentCard(
        id=cid, tenant_id=T1, name=f"card {cid}", version="1.0.0",
        card_json={"name": cid, "capabilities": []},
    )))
    return cid


def own_run(h, sid, *, minutes_ago=60.0, state="completed"):
    rid = f"run-{next(_n)}"
    at = _now() - timedelta(minutes=minutes_ago)
    h.seed(lambda s: s.add(AISystemRun(
        id=rid, tenant_id=T1, root_task_id=f"root-{rid}", ai_system_id=sid,
        started_at=at, ended_at=at, terminal_state=state, **work_outcome(state), finalized_by="explicit",
        is_replay=False, action_count=3, llm_call_count=3, chain_integrity="intact",
    )))
    return rid


def served(h, root, svc, *, outcome="answered", minutes_ago=60.0, n=1, run_state="completed",
           tool_calls=0, tool_errors=0, holds=0, models=None, cause=None):
    """`n` runs of `root`, each passing through `svc` once."""
    for _ in range(n):
        rid = own_run(h, root, minutes_ago=minutes_ago, state=run_state)
        at = _now() - timedelta(minutes=minutes_ago)
        h.seed(lambda s, rid=rid, at=at: s.add(AISystemRunSegment(
            id=f"seg-{next(_n)}", run_id=rid, tenant_id=T1, root_task_id=f"root-{rid}",
            root_system_id=root, caller_system_id=root, segment_system_id=svc, min_depth=1,
            first_action_at=at, last_action_at=at + timedelta(seconds=5),
            action_count=2, llm_call_count=1, tool_call_count=tool_calls,
            tool_error_count=tool_errors, approval_request_count=holds,
            cost_usd=0.001, outcome=outcome, models_used=models or {}, error_cause=cause,
        )))


def edge(h, root, **body):
    # An external dependency names its supplier (EU AI Act Art 25(4)).
    if body.get("role") == "external":
        body.setdefault("supplier_legal_entity", "Supplier Ltd")
    r = h.client.post(f"/v1/admin/ai-systems/{root}/dependencies", json=body,
                      headers=h.headers(T1))
    assert r.status_code == 201, r.text
    return r.json()


def patch_edge(h, root, edge_id, **body):
    r = h.client.patch(f"/v1/admin/ai-systems/{root}/dependencies/{edge_id}", json=body,
                       headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def health(h, sid):
    r = h.client.get(f"/v1/admin/ai-systems/{sid}/health?refresh=true", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def report(h, sid):
    r = h.client.get(f"/v1/admin/ai-systems/{sid}/assurance", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def composition(h, sid):
    r = h.client.get(f"/v1/admin/ai-systems/{sid}/composition", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def listing(h):
    r = h.client.get("/v1/admin/ai-systems?limit=500", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["systems"]}


def codes(body, code):
    return [f for f in body["findings"] if f["code"] == code]


def baseline(h, sid, metric, *, basis="segments", state="ready", caller="all"):
    h.seed(lambda s: s.add(AISystemBaseline(
        id=f"base-{next(_n)}", tenant_id=T1, ai_system_id=sid, metric_key=metric,
        bucket="all", actor_id="all", basis=basis, caller_system_id=caller,
        median=1.0, mad=0.1, sample_count=60, state=state,
    )))


def health_row(h, sid, **scores):
    """A materialised health row for `sid` (what the index reads for a
    dependency's OWN failing signal)."""
    def fn(s):
        row = s.query(AISystemHealth).filter_by(tenant_id=T1, ai_system_id=sid).first()
        if row is None:
            row = AISystemHealth(id=f"hlt-{next(_n)}", tenant_id=T1, ai_system_id=sid)
            s.add(row)
        row.health_state = scores.pop("state", "healthy")
        row.open_drift_events = scores.pop("open_drift", 0)
        for k in ("liveness", "behaviour", "reliability", "cost", "conformance",
                  "oversight", "review"):
            setattr(row, f"{k}_score", scores.get(k, 1.0))
    h.seed(fn)


def contract(h, sid, version, *, status="active", closure=None):
    closure = closure or {"schema_version": 3, "ai_system": {"id": sid, "name": sid},
                          "composition": {"llm_models": []}, "v": version}
    cid = f"ctr-{sid}-v{version}"
    h.seed(lambda s: s.add(AISystemContract(
        id=cid, tenant_id=T1, ai_system_id=sid, version=version,
        contract_hash=hash_closure(closure), resolved_closure=closure, status=status,
        approved_by="approver" if status != "draft" else None,
    )))
    return cid


def set_status(h, cid, status):
    def fn(s):
        s.query(AISystemContract).filter_by(id=cid).first().status = status
    h.seed(fn)


def agent_with_grants(h, sid, agent_id, targets):
    def fn(s):
        s.add(AgentIdentity(id=agent_id, tenant_id=T1, name=agent_id, enabled=True,
                            status="active", default_deny=True, enforcement_mode="enforce"))
        s.add(ResourceGroupAgentMember(tenant_id=T1, agent_id=agent_id, group_id=sid,
                                       role="component"))
        s.flush()
        for i, (target, effect) in enumerate(targets):
            s.add(AgentGrant(id=f"grant-{agent_id}-{i}", tenant_id=T1, agent_id=agent_id,
                             action_type="a2a_call", target=target, effect=effect, enabled=True))
    h.seed(fn)


def a2a_call(h, rg, card_id, *, status=200, duration=100, minutes_ago=30):
    h.seed(lambda s: s.add(ProxyLog(
        id=f"log-{ULID()}", tenant_id=T1, timestamp=_now() - timedelta(minutes=minutes_ago),
        proxy_type="a2a", http_method="POST", endpoint="/a2a", server_id=card_id,
        correlation_id=f"corr-{next(_n)}", end_user_session_id="s", success=status < 400,
        http_status_code=status, duration_ms=duration, resource_group_id=rg,
    )))


def session(h):
    return h.Factory()


# ---------------------------------------------------------------------------
# Service signals
# ---------------------------------------------------------------------------

SIGNALS_MEASURED = ("liveness", "behaviour", "reliability", "cost", "conformance")


class TestServiceSignals:
    def test_a_service_past_the_minimum_has_every_signal_measured(self, h):
        root = system(h, "ss-root")
        svc = system(h, "ss-svc", clients=[root], kind="service", on_demand=True)
        served(h, root, svc, n=55)
        for metric in ("action_count", "cost_per_run", "tool_mix"):
            baseline(h, svc, metric)
        body = health(h, svc)
        assert body["signal_basis"] == "segments"
        for sig in SIGNALS_MEASURED:
            assert body[f"{sig}_score"] is not None, f"{sig} not measured"
        assert body["baseline_state"] == "ready"
        assert body["health_state"] == "healthy", body["reasons"]
        # Oversight: no hold raised → n/a, never 0 % and never a pass.
        assert body["signal_levels"]["oversight"] == {"score": None, "level": "not_applicable"}
        assert body["oversight_score"] is None

    def test_run_basis_baselines_never_make_a_service_ready(self, h):
        root = system(h, "sb-root")
        svc = system(h, "sb-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=3)
        baseline(h, svc, "action_count", basis="runs")  # a different population
        body = health(h, svc)
        assert body["behaviour_score"] is None
        assert body["baseline_state"] == "learning"
        baseline(h, svc, "action_count", basis="segments")
        assert health(h, svc)["behaviour_score"] == 1.0

    def test_segment_cost_reads_the_cost_baseline_and_its_drift(self, h):
        root = system(h, "sc-root")
        svc = system(h, "sc-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=2)
        assert health(h, svc)["cost_score"] is None  # no ready cost baseline
        baseline(h, svc, "cost_per_run")
        assert health(h, svc)["cost_score"] == 1.0
        h.seed(lambda s: s.add(AISystemDriftEvent(
            id=f"drift-{next(_n)}", tenant_id=T1, ai_system_id=svc, drift_class="cost",
            metric_key="cost_per_run", basis="segments", severity="high",
        )))
        assert health(h, svc)["cost_score"] == 0.5

    def test_reliability_is_answered_share_bounded_by_tool_errors(self, h):
        root = system(h, "sr-root")
        svc = system(h, "sr-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=3, minutes_ago=90)
        served(h, root, svc, n=1, outcome="failed", minutes_ago=80)
        served(h, root, svc, n=1, minutes_ago=70)  # latest answered: alive
        assert health(h, svc)["reliability_score"] == pytest.approx(0.8)
        # 9 of 10 answered; 20 of 50 tool calls failed → min(0.9, 0.6).
        served(h, root, svc, n=5, tool_calls=10, tool_errors=4, minutes_ago=60)
        assert health(h, svc)["reliability_score"] == pytest.approx(0.6)

    def test_a_service_that_declines_every_call_is_reliable(self, h):
        """`rejected` = the service ANSWERED (it declined the task) — THE
        classification (ai_system_down_rule.classify_segment_outcome)."""
        from app.services import ai_system_service_signals as signals

        root = system(h, "srj-root")
        svc = system(h, "srj-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=4, outcome="rejected")
        assert health(h, svc)["reliability_score"] == pytest.approx(1.0)
        s = session(h)
        try:
            facts = signals.segment_facts(s, T1, svc, _now() - timedelta(days=30))
        finally:
            s.close()
        # Still visible as rejected — not folded into "answered".
        assert facts["outcome_mix"]["rejected"] == 4 and facts["outcome_mix"]["answered"] == 0
        assert (facts["answered_calls"], facts["rejected_calls"]) == (4, 4)

    def test_gateway_refused_calls_do_not_lower_reliability(self, h):
        """A call the gateway refused never reached the service: it is out
        of the reliability denominator, and still reported as blocked.
        The core stores it as `failed` + `blocked_by_policy`; the readers
        take that pair as the refusal (`segment_call_outcome`)."""
        from app.services import ai_system_service_signals as signals

        root = system(h, "sbl-root")
        svc = system(h, "sbl-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=2, minutes_ago=90)
        served(h, root, svc, n=3, outcome="failed", cause="blocked_by_policy", minutes_ago=80)
        served(h, root, svc, n=2, minutes_ago=70)
        assert health(h, svc)["reliability_score"] == pytest.approx(1.0)
        s = session(h)
        try:
            facts = signals.segment_facts(s, T1, svc, _now() - timedelta(days=30))
        finally:
            s.close()
        assert (facts["served_calls"], facts["blocked_calls"], facts["reliability_calls"]) == (7, 3, 4)
        assert facts["outcome_mix"]["blocked"] == 3 and facts["outcome_mix"]["failed"] == 0
        assert facts["error_causes"] == {"blocked_by_policy": 3}
        # Failures do lower it: 4 answered of 5 judged.
        served(h, root, svc, n=1, outcome="timed_out", minutes_ago=60)
        assert health(h, svc)["reliability_score"] == pytest.approx(0.8)

    def test_a_fully_refused_service_has_no_reliability_evidence(self, h):
        root = system(h, "sfb-root")
        svc = system(h, "sfb-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=3, outcome="failed", cause="blocked_by_policy")
        assert health(h, svc)["reliability_score"] is None

    def test_a_deviating_delegate_answer_counts_against_reliability(self, h):
        """Answered, but with an outcome its own contract does
        not declare: an answer for liveness, a deviation for reliability."""
        from app.services import ai_system_service_signals as signals

        root = system(h, "sdv-root")
        svc = system(h, "sdv-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=3, minutes_ago=90)
        rid = own_run(h, root, minutes_ago=80)
        at = _now() - timedelta(minutes=80)
        h.seed(lambda s: s.add(AISystemRunSegment(
            id=f"seg-{next(_n)}", run_id=rid, tenant_id=T1, root_task_id=f"root-{rid}",
            root_system_id=root, caller_system_id=root, segment_system_id=svc, min_depth=1,
            first_action_at=at, last_action_at=at, outcome="answered",
            outcome_id="screening_completed.maybe", outcome_conformance="undeclared",
            error_cause="delegate_deviated",
        )))
        s = session(h)
        try:
            facts = signals.segment_facts(s, T1, svc, _now() - timedelta(days=30))
        finally:
            s.close()
        assert (facts["answered_calls"], facts["deviated_calls"], facts["served_calls"]) == (3, 1, 4)
        assert facts["success_rate"] == pytest.approx(0.75)

    def test_oversight_applies_when_the_service_raises_holds(self, h):
        root = system(h, "so-root")
        svc = system(h, "so-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=2, holds=1)
        assert health(h, svc)["signal_levels"]["oversight"]["level"] == "enforced"

    def test_the_period_verdict_is_evidenced_by_served_calls(self, h):
        root = system(h, "sv-root")
        svc = system(h, "sv-svc", clients=[root], on_demand=True)
        served(h, root, svc, n=4)
        rep = report(h, svc)
        assert rep["signal_basis"] == "segments"
        assert rep["service"]["served_calls"] == 4
        assert rep["verdict"]["status"] != "insufficient_evidence", rep["verdict"]
        # The report states what the evidence IS, not "no runs".
        gaps = " ".join(rep["not_covered"])
        assert "4 call(s) it served" in gaps and "No runs in this window" not in gaps

    def test_an_advisory_signal_is_shown_but_never_judged(self, h):
        """Kind `service`: liveness is advisory — computed and displayed
        (never "not measured"), but kept out of the verdict."""
        root = system(h, "sa-root")
        svc = system(h, "sa-svc", clients=[root], kind="service", on_demand=True)
        served(h, root, svc, n=3, minutes_ago=90)
        served(h, root, svc, outcome="timed_out", minutes_ago=30)
        body = health(h, svc)
        # One unanswered among the latest 3: degraded liveness (THE down rule).
        assert body["signal_levels"]["liveness"] == {"score": 0.5, "level": "advisory"}
        assert body["health_state"] != "silent"
        assert body["health_score"] == pytest.approx(0.75)  # reliability, not liveness

    def test_an_advisory_component_does_not_fail_a_dependency(self, h):
        root = system(h, "sq-root")
        svc = system(h, "sq-svc", clients=[root], kind="service", on_demand=True)
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        served(h, root, svc)
        health_row(h, svc, liveness=0.0)  # advisory for a service
        assert health(h, root)["dependency_cap"] is None

    def test_a_system_with_own_runs_stays_on_the_run_basis(self, h):
        root = system(h, "sx-root")
        both = system(h, "sx-both", clients=[root])
        own_run(h, both)
        served(h, root, both, n=2)
        assert health(h, both)["signal_basis"] == "runs"


# ---------------------------------------------------------------------------
# Dependency health in the root's verdict
# ---------------------------------------------------------------------------


class TestDependencyCap:
    def test_a_critical_dependency_not_answering_caps_the_root(self, h):
        root = system(h, "dc-root")
        svc = system(h, "dc-svc", clients=[root], on_demand=True)
        e = edge(h, root, role="shared_service", depends_on_system_id=svc)
        served(h, root, svc, n=2, minutes_ago=90)
        before = health(h, root)
        assert before["health_state"] != "degraded" and before["dependency_cap"] is None
        # THE down rule (one fact, one computation): ONE unanswered call among the latest
        # degrades the dependency — it caps the root, but as `reliability`,
        # not as an outage.
        served(h, root, svc, outcome="timed_out", minutes_ago=30)
        body = health(h, root)
        assert body["health_state"] == "degraded"
        assert body["dependency_cap"]["failing_signal"] == "reliability"

        # The latest THREE all unanswered: not answering — `liveness`.
        served(h, root, svc, outcome="timed_out", minutes_ago=25)
        served(h, root, svc, outcome="timed_out", minutes_ago=20)
        body = health(h, root)
        assert body["health_state"] == "degraded"
        assert body["dependency_cap"] == {
            "reason": "dependency", "edge_id": e["id"], "system_id": svc,
            "name": svc, "failing_signal": "liveness",
        }
        assert any(svc in r and "liveness" in r for r in body["reasons"])
        (dep,) = body["dependency_health"]
        assert (dep["status"], dep["basis"], dep["caps_root"]) == ("degraded", "tested", True)
        # Recovery must be demonstrated: degraded until three answered calls
        # in a row push the failures out of the latest three.
        served(h, root, svc, minutes_ago=12)
        assert health(h, root)["dependency_cap"]["failing_signal"] == "reliability"
        served(h, root, svc, minutes_ago=11)
        served(h, root, svc, minutes_ago=10)
        assert health(h, root)["dependency_cap"] is None

    def test_the_dependency_own_failing_signal_is_named(self, h):
        root = system(h, "dn-root")
        svc = system(h, "dn-svc", clients=[root], on_demand=True)
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        served(h, root, svc, n=1)
        health_row(h, svc, reliability=0.4)
        cap = health(h, root)["dependency_cap"]
        assert cap["failing_signal"] == "reliability"

    def test_degradable_never_caps_and_checks_the_fallback(self, h):
        root = system(h, "dd-root")
        svc = system(h, "dd-svc", clients=[root], on_demand=True)
        edge(h, root, role="shared_service", depends_on_system_id=svc,
             criticality="degradable", fallback="manual review queue")
        served(h, root, svc, outcome="failed", minutes_ago=50)
        body = health(h, root)
        (dep,) = body["dependency_health"]
        assert body["dependency_cap"] is None and dep["caps_root"] is False
        assert dep["status"] == "degraded"
        assert dep["fallback_check"] == {"unanswered_calls": 1, "root_runs_completed": 1, "held": True}
        assert "manual review queue" in dep["note"]
        served(h, root, svc, outcome="failed", minutes_ago=40, run_state="errored")
        (dep,) = health(h, root)["dependency_health"]
        assert dep["fallback_check"]["held"] is False
        assert "did NOT hold" in dep["note"]

    def test_external_is_assumed_with_boundary_metrics(self, h):
        root = system(h, "de-root")
        c = card(h, "de-card")
        edge(h, root, role="external", agent_card_id=c,
             assumptions={"retention_at_delegate": "30 days"})
        a2a_call(h, root, c, duration=100)
        a2a_call(h, root, c, status=502, duration=900)
        body = health(h, root)
        (dep,) = body["dependency_health"]
        assert (dep["status"], dep["basis"], dep["caps_root"]) == ("assumed", "assumed", False)
        assert dep["boundary"]["calls"] == 2 and dep["boundary"]["errors"] == 1
        assert dep["boundary"]["error_rate"] == 0.5
        assert dep["boundary"]["p95_latency_ms"] == 900
        assert dep["assumptions"] == {"retention_at_delegate": "30 days"}
        assert body["dependency_cap"] is None
        assert any("assumed, not tested" in g for g in report(h, root)["not_covered"])

    def test_failure_propagates_along_critical_edges_transitively(self, h):
        a = system(h, "dt-a")
        b = system(h, "dt-b", clients=[a])
        c = system(h, "dt-c", clients=[b])
        edge(h, a, role="shared_service", depends_on_system_id=b)
        edge(h, b, role="shared_service", depends_on_system_id=c)
        served(h, a, b)
        health_row(h, c, reliability=0.3)
        cap = health(h, a)["dependency_cap"]
        (dep,) = health(h, a)["dependency_health"]
        assert cap["failing_signal"] == "dependency"
        assert (dep["via_system_id"], dep["via_system_name"]) == (c, c)

    def test_a_cycle_cannot_sustain_a_failure_nobody_has(self, h):
        """Reading back a CAPPED state would let A and B hold each other down
        after A recovered; only own evidence seeds the propagation."""
        a = system(h, "dy-a", clients=["dy-b"])
        b = system(h, "dy-b", clients=["dy-a"])
        edge(h, a, role="shared_service", depends_on_system_id=b)
        edge(h, b, role="shared_service", depends_on_system_id=a)
        served(h, a, b)
        served(h, b, a)
        health_row(h, a, reliability=0.3)
        assert health(h, b)["dependency_cap"]["failing_signal"] == "reliability"
        health_row(h, a)  # recovered (1.0 everywhere)
        assert health(h, b)["dependency_cap"] is None
        assert health(h, a)["dependency_cap"] is None

    def test_list_tab_and_health_read_one_status(self, h):
        root = system(h, "do-root")
        svc = system(h, "do-svc", clients=[root], on_demand=True)
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        served(h, root, svc)
        health_row(h, svc, conformance=0.5)
        tab = composition(h, root)["dependencies"][0]["health"]
        fleet = listing(h)[root]["composition"]["worst_dependency_health"]
        own = health(h, root)["dependency_health"][0]["status"]
        assert tab == fleet == own == "degraded"


# ---------------------------------------------------------------------------
# Responses on dependency findings
# ---------------------------------------------------------------------------


class TestDependencyResponses:
    def _open(self, s, root):
        return [
            e for e in s.query(AISystemDriftEvent).filter_by(tenant_id=T1, ai_system_id=root)
            if e.metric_key.startswith("dependency:") and e.status in ("active", "acknowledged")
        ]

    def test_a_policy_targets_the_dependency_finding_on_the_root(self, h):
        from app.services.ai_system_assurance import upsert_health
        from app.services.ai_system_response.actuator import apply_plan, plan_for_drift

        root = system(h, "rp-root")
        svc = system(h, "rp-svc", clients=[root], on_demand=True)
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        # Not answering (THE down rule: the latest three unanswered) → critical.
        served(h, root, svc, outcome="failed", n=3)
        h.seed(lambda s: s.add(AISystemResponsePolicy(
            id="pol-rp", tenant_id=T1, ai_system_id=root, name="dependency down",
            signal_kind="drift", drift_class="dependency", severity_at_least="high",
            actions=[{"kind": "set_autonomy", "level": "approval_required"}],
        )))

        s = session(h)
        try:
            system_row = s.get(ResourceGroup, root)
            row = upsert_health(s, system_row)
            s.commit()
            (ev,) = self._open(s, root)
            assert (ev.drift_class, ev.attributed_cause, ev.severity) == (
                "dependency", "dependency_degraded", "critical")
            assert ev.metric_key == f"dependency:{svc}"
            # Capped, not "drifting": a dependency finding is not own drift.
            assert row.health_state == "degraded" and row.open_drift_events == 0

            plan = plan_for_drift(s, ev)
            assert [a.to_dict()["params"] for a in plan.actions] == [{"level": "approval_required"}]
            apply_plan(s, ev, plan)
            s.commit()
            assert s.get(ResourceGroup, root).autonomy_level == "approval_required"

            # A second tick does not duplicate the standing finding, and the
            # open finding still does not count as the root's own drift.
            row = upsert_health(s, s.get(ResourceGroup, root))
            s.commit()
            assert len(self._open(s, root)) == 1
            assert row.health_state == "degraded" and row.open_drift_events == 0
        finally:
            s.close()

        # The dependency answers again — three in a row (THE down rule).
        served(h, root, svc, n=3, minutes_ago=10)
        s = session(h)
        try:
            upsert_health(s, s.get(ResourceGroup, root))
            s.commit()
            assert self._open(s, root) == []
            resolved = s.query(AISystemDriftEvent).filter_by(
                ai_system_id=root, metric_key=f"dependency:{svc}").one()
            assert resolved.resolved_by == "system:dependency-health"
        finally:
            s.close()

    def test_a_drifting_service_triggers_the_roots_response(self, h):
        """The rollup: the core writes segment drift against the SERVICE;
        the ROOT's policy responds through the declared critical edge, on
        the root's next tick — without waiting for the service's health."""
        from app.services.ai_system_assurance import upsert_health
        from app.services.ai_system_response.actuator import apply_plan, plan_for_drift

        root = system(h, "rd-root")
        svc = system(h, "rd-svc", clients=[root], on_demand=True)
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        served(h, root, svc)
        h.seed(lambda s: s.add(AISystemResponsePolicy(
            id="pol-rd", tenant_id=T1, ai_system_id=root, name="dependency drifting",
            signal_kind="drift", drift_class="dependency", severity_at_least="high",
            actions=[{"kind": "set_autonomy", "level": "approval_required"}],
        )))
        h.seed(lambda s: s.add(AISystemDriftEvent(
            id="drift-rd-svc", tenant_id=T1, ai_system_id=svc, drift_class="behavioural",
            metric_key="tool_mix", basis="segments", severity="high",
        )))
        s = session(h)
        try:
            assert s.query(AISystemHealth).filter_by(ai_system_id=svc).first() is None
            upsert_health(s, s.get(ResourceGroup, root))
            s.commit()
            (ev,) = self._open(s, root)
            assert (ev.severity, ev.cause_evidence["failing_signal"]) == ("high", "behaviour")
            apply_plan(s, ev, plan_for_drift(s, ev))
            s.commit()
            assert s.get(ResourceGroup, root).autonomy_level == "approval_required"
        finally:
            s.close()

    def test_a_degradable_dependency_opens_a_finding_only_when_its_fallback_fails(self, h):
        from app.services.ai_system_assurance import upsert_health

        root = system(h, "rq-root")
        svc = system(h, "rq-svc", clients=[root], on_demand=True)
        edge(h, root, role="shared_service", depends_on_system_id=svc,
             criticality="degradable", fallback="skip")
        served(h, root, svc, outcome="failed")
        s = session(h)
        try:
            upsert_health(s, s.get(ResourceGroup, root))
            s.commit()
            assert self._open(s, root) == []  # fallback held
        finally:
            s.close()
        served(h, root, svc, outcome="failed", run_state="errored")
        s = session(h)
        try:
            upsert_health(s, s.get(ResourceGroup, root))
            s.commit()
            (ev,) = self._open(s, root)
            assert ev.severity == "medium"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class TestContracts:
    def _closure(self, h, sid):
        from app.services.ai_system_closure import resolve_closure

        s = session(h)
        try:
            return resolve_closure(s, s.get(ResourceGroup, sid))
        finally:
            s.close()

    def test_dependencies_section_is_conditional_sorted_and_carries_the_contract(self, h):
        root = system(h, "cc-root")
        assert "dependencies" not in self._closure(h, root)
        svc_b = system(h, "cc-b", clients=[root])
        svc_a = system(h, "cc-a", clients=[root])
        c = card(h, "cc-card")
        v1 = contract(h, svc_a, 1)
        edge(h, root, role="shared_service", depends_on_system_id=svc_b)
        edge(h, root, role="shared_service", depends_on_system_id=svc_a)
        edge(h, root, role="external", agent_card_id=c)
        deps = self._closure(h, root)["dependencies"]
        assert [(d["role"], d["depends_on_system_id"]) for d in deps] == [
            ("external", None), ("shared_service", svc_a), ("shared_service", svc_b)]
        a = deps[1]
        assert a["contract"]["id"] == v1 and a["contract"]["version"] == 1
        assert a["contract"]["hash"].startswith("sha256:")
        assert a["contract_pin"] is None and deps[2]["contract"] is None
        assert "contract" not in deps[0]  # external: no Brutor contract
        for term in ("criticality", "contributes_to_decision", "fallback",
                     "separability_rationale", "assumptions"):
            assert term in a

    def test_a_service_promotion_drifts_following_callers_not_pinned_ones(self, h):
        from app.services import ai_system_contract_service as contracts

        svc = system(h, "cp-svc", clients=["cp-follow", "cp-pinned"])
        follow = system(h, "cp-follow")
        pinned = system(h, "cp-pinned")
        v1 = contract(h, svc, 1)
        edge(h, follow, role="shared_service", depends_on_system_id=svc)
        edge(h, pinned, role="shared_service", depends_on_system_id=svc, contract_pin=v1)

        s = session(h)
        try:
            for sid in (follow, pinned):
                c, _ = contracts.mint(s, s.get(ResourceGroup, sid), created_by="t")
                contracts.approve(s, c, approved_by="approver")
                contracts.promote(s, c)
            s.commit()
            assert not contracts.is_config_drifted(s, s.get(ResourceGroup, follow))
        finally:
            s.close()

        set_status(h, v1, "superseded")
        contract(h, svc, 2, closure={"schema_version": 3, "ai_system": {"id": svc, "name": svc},
                                     "composition": {"llm_models": [{"id": "m-new"}]}})
        s = session(h)
        try:
            assert contracts.is_config_drifted(s, s.get(ResourceGroup, follow)) is True
            assert contracts.is_config_drifted(s, s.get(ResourceGroup, pinned)) is False
            # The follower's next version shows the SERVICE's own diff.
            c, created = contracts.mint(s, s.get(ResourceGroup, follow), created_by="t")
            assert created
            (entry,) = [w for w in c.diff_summary["widened"]
                        if w["kind"] == "dependency_contract_changed"]
            assert entry["detail"].startswith(f"dependency {svc} widened (v1 → v2): ")
            assert "llm_models gained m-new" in entry["detail"]
            assert entry["dependency_diff"]["widened"][0]["id"] == "m-new"
            s.rollback()
        finally:
            s.close()
        # The pinned caller gets a stale-pin finding instead of drifting.
        found = codes(composition(h, pinned), "composition.dependency_pin_stale")
        assert [f["subject_id"] for f in found] == [svc]
        assert "v1" in found[0]["message"] and "v2" in found[0]["message"]

    def test_diff_classifies_the_services_own_change(self):
        from app.services.ai_system_contract_service import diff_closures

        def svc_closure(models):
            return {"ai_system": {"id": "s", "name": "Scorer"},
                    "composition": {"llm_models": [{"id": m} for m in models]}}

        store = {"c1": svc_closure(["a", "b"]), "c2": svc_closure(["a"]),
                 "c3": svc_closure(["a", "b", "c"])}

        def caller(cid, **extra):
            return {"dependencies": [{
                "role": "shared_service", "depends_on_system_id": "s", "agent_card_id": None,
                "criticality": "critical", "contributes_to_decision": True, "fallback": None,
                "separability_rationale": None, "assumptions": {}, "contract_pin": None,
                "contract": {"id": cid, "version": int(cid[1]), "hash": cid}, **extra}]}

        narrowed = diff_closures(caller("c1"), caller("c2"), resolve_contract=store.get)
        assert [n["detail"] for n in narrowed["narrowed"]] == [
            "dependency Scorer narrowed (v1 → v2): llm_models lost b"]
        assert not narrowed["widened"]
        widened = diff_closures(caller("c1"), caller("c3"), resolve_contract=store.get)
        assert widened["has_widened_permissions"]
        # Without a resolver it is still never "silently fine".
        blind = diff_closures(caller("c1"), caller("c3"))
        assert "read the service's contract" in blind["widened"][0]["detail"]

    def test_edge_changes_are_classified(self):
        from app.services.ai_system_contract_service import diff_closures

        def dep(**over):
            d = {"role": "shared_service", "depends_on_system_id": "s", "agent_card_id": None,
                 "criticality": "critical", "contributes_to_decision": True, "fallback": None,
                 "separability_rationale": None, "assumptions": {}, "contract_pin": None,
                 "contract": None}
            d.update(over)
            return {"dependencies": [d]}

        added = diff_closures({}, dep())
        assert [w["kind"] for w in added["widened"]] == ["dependency_added"]
        removed = diff_closures(dep(), {})
        assert [n["kind"] for n in removed["narrowed"]] == ["dependency_removed"]
        separable = diff_closures(dep(), dep(contributes_to_decision=False,
                                             separability_rationale="r"))
        assert [w["kind"] for w in separable["widened"]] == ["dependency_separability_declared"]
        degradable = diff_closures(dep(), dep(criticality="degradable", fallback="f"))
        assert [w["kind"] for w in degradable["widened"]] == ["dependency_criticality"]


# ---------------------------------------------------------------------------
# Composition checks
# ---------------------------------------------------------------------------


class TestCompositionChecks:
    def test_grant_without_edge(self, h):
        root = system(h, "cg-root")
        c = card(h, "cg-card")
        agent_with_grants(h, root, "cg-agent", [(c, "allow")])
        found = codes(composition(h, root), "composition.grant_without_edge")
        assert [f["subject_id"] for f in found] == [c]
        edge(h, root, role="external", agent_card_id=c)
        assert codes(composition(h, root), "composition.grant_without_edge") == []

    def test_a_deny_grant_is_not_a_grant(self, h):
        root = system(h, "cd-root")
        c = card(h, "cd-card")
        agent_with_grants(h, root, "cd-agent", [(c, "deny")])
        assert codes(composition(h, root), "composition.grant_without_edge") == []

    def test_edge_without_grant(self, h):
        root = system(h, "ce-root")
        c = card(h, "ce-card")
        edge(h, root, role="external", agent_card_id=c)
        found = codes(composition(h, root), "composition.edge_without_grant")
        assert [f["subject_id"] for f in found] == [c]
        agent_with_grants(h, root, "ce-agent", [(c, "approval_required")])
        assert codes(composition(h, root), "composition.edge_without_grant") == []

    def test_a_wildcard_grant_reaches_every_edge(self, h):
        root = system(h, "cw-root")
        c = card(h, "cw-card")
        edge(h, root, role="external", agent_card_id=c)
        agent_with_grants(h, root, "cw-agent", [("*", "allow")])
        assert codes(composition(h, root), "composition.edge_without_grant") == []

    def test_dependency_pin_stale(self, h):
        root = system(h, "cs-root")
        svc = system(h, "cs-svc", clients=[root])
        v1 = contract(h, svc, 1)
        e = edge(h, root, role="shared_service", depends_on_system_id=svc, contract_pin=v1)
        assert codes(composition(h, root), "composition.dependency_pin_stale") == []
        set_status(h, v1, "superseded")
        v2 = contract(h, svc, 2)
        found = codes(composition(h, root), "composition.dependency_pin_stale")
        assert [f["subject_id"] for f in found] == [svc]
        patch_edge(h, root, e["id"], contract_pin=v2)
        assert codes(composition(h, root), "composition.dependency_pin_stale") == []

    def test_undeclared_a2a_card_call(self, h):
        root = system(h, "cu-root")
        c = card(h, "cu-card")
        a2a_call(h, root, c)
        found = codes(composition(h, root), "composition.undeclared_dependency")
        assert [f["subject_id"] for f in found] == [c]
        edge(h, root, role="external", agent_card_id=c)
        assert codes(composition(h, root), "composition.undeclared_dependency") == []

    def test_one_call_to_a_known_service_is_one_finding(self, h):
        """A segment through a service AND an A2A row naming its card are one
        fact: the undeclared dependency on that system, not two findings."""
        root, other = system(h, "cx-root"), system(h, "cx-other")
        svc = system(h, "cx-svc", clients=[root, other])
        c = card(h, "cx-card")
        edge(h, other, role="shared_service", depends_on_system_id=svc, agent_card_id=c)
        served(h, root, svc)
        a2a_call(h, root, c)
        found = codes(composition(h, root), "composition.undeclared_dependency")
        assert [f["subject_id"] for f in found] == [svc]

    def test_checks_raise_and_resolve_inbox_items(self, h):
        from app.services.assurance_checks.composition import run_composition_checks

        root = system(h, "ci-root")
        c = card(h, "ci-card")
        agent_with_grants(h, root, "ci-agent", [(c, "allow")])
        fp = f"composition:{root}:composition.grant_without_edge:{c}"

        def items(s):
            return s.query(AssuranceInboxItem).filter_by(tenant_id=T1, fingerprint=fp).all()

        s = session(h)
        try:
            run_composition_checks(s, T1)
            run_composition_checks(s, T1)
            s.commit()
            (item,) = items(s)
            assert (item.category, item.state, item.ai_system_id) == ("composition", "open", root)
            assert item.occurrence_count == 2
        finally:
            s.close()
        edge(h, root, role="external", agent_card_id=c)
        s = session(h)
        try:
            stats = run_composition_checks(s, T1)
            s.commit()
            (item,) = items(s)
            assert (item.state, item.disposition) == ("resolved", "fixed")
            assert item.resolved_by == "system:composition-checks"
            assert stats["resolved"] >= 1
        finally:
            s.close()

    def test_tab_list_and_inbox_read_one_computation(self, h):
        from app.services.ai_system_composition import CompositionIndex
        from app.services.assurance_checks.composition import composition_findings

        root = system(h, "co-root")
        c = card(h, "co-card")
        agent_with_grants(h, root, "co-agent", [(c, "allow")])
        own_run(h, root)
        tab = composition(h, root)["findings"]
        s = session(h)
        try:
            direct = composition_findings(CompositionIndex(s, T1), root)
        finally:
            s.close()
        assert tab == direct and tab
        assert listing(h)[root]["composition_finding_count"] == len(direct)


def test_composition_findings_are_constructed_in_one_module():
    """One fact, one computation: every composition finding code is emitted by the check library
    only. A second module building one is a second computation of the fact."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    home = app_dir / "services" / "assurance_checks" / "composition.py"
    vocab = app_dir / "services" / "ai_system_composition.py"
    offenders = []
    for path in python_sources(app_dir):
        if path in (home, vocab):
            continue
        text = path.read_text()
        if re.search(r"FINDING_(UNDECLARED_DEPENDENCY|UNACCEPTED_CALLER|DECLARED_NEVER_OBSERVED|"
                     r"ROLE_MISMATCH|DEPENDENCY_CYCLE|GRANT_WITHOUT_EDGE|EDGE_WITHOUT_GRANT|"
                     r"DEPENDENCY_PIN_STALE)\b", text) or "\"composition." in text:
            offenders.append(source_label(path))
    assert offenders == []
    vocab_text = vocab.read_text()
    assert not re.search(r"(?<![A-Za-z0-9_])_finding\(", vocab_text), (
        "findings must be built in the check library")

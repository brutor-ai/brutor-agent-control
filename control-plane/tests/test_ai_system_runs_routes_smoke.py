# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Every GET route of the runs API answers without a server error

Unit tests cover what each endpoint returns; this covers that each one RETURNS
at all against a fully populated run: a field added to the run summary can
make run details 500 on a duplicate keyword in a route no other test opens.
This test enumerates the router's GET routes
itself, so a route added later is covered without anyone remembering to.
"""

from __future__ import annotations

import os
import re
from tests.pg_test_url import pg_test_url

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.agent_identities import create_agent_identities_router
from app.api.v1.endpoints.admin.ai_obligations import create_ai_obligations_router
from app.api.v1.endpoints.admin.ai_system_contracts import create_ai_system_contracts_router
from app.api.v1.endpoints.admin.ai_system_composition import (
    create_ai_system_composition_router,
)
from app.api.v1.endpoints.admin.ai_system_evidence import create_ai_system_evidence_router
from app.api.v1.endpoints.admin.compliance_documentation import (
    create_compliance_documentation_router,
)
from app.api.v1.endpoints.admin.ai_system_liveness import create_ai_system_liveness_router
from app.api.v1.endpoints.admin.ai_system_response import create_ai_system_response_router
from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_identity import AgentIdentity, ResourceGroupAgentMember
from app.database.models.agent_implementation_release import AgentImplementationRelease
from app.database.models.ai_system_lifecycle_event import AISystemLifecycleEvent
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.proxy_log import ProxyLog
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
SYS = "sys-smoke"
RUN = "run-smoke"
ROOT = "root-smoke"

SVC = "sys-smoke-svc"
_LOG = BrutorLogger("runs-smoke")
#: Every AI-System GET surface the run ledger feeds — runs, the
#: composition/dependency routes, liveness and the fleet list. A route added
#: to any of these routers is covered without anyone remembering to.
ROUTERS = [
    create_ai_system_runs_router(_LOG),
    create_ai_system_composition_router(_LOG),
    create_ai_system_liveness_router(_LOG),
    create_ai_systems_router(_LOG),
    # Health and the assurance report carry
    # dependency_health / signal_basis, computed from segments and edges.
    create_ai_system_response_router(_LOG),
    # supplier_information evidence on an edge, the
    # obligation registry at the effective tier, and DC3 / Annex IV(2)
    # listing components and dependencies.
    create_ai_system_evidence_router(_LOG),
    create_ai_obligations_router(_LOG),
    create_compliance_documentation_router(_LOG),
    # The identity's release timeline, and the lifecycle record that
    # carries agent.new_release events next to stage transitions.
    create_agent_identities_router(_LOG),
    create_ai_system_contracts_router(_LOG),
]
AGENT = "agent-1"
#: `report_id` names no snapshot (and `version` no contract): a 404 is an
#: answer, a 500 is not.
PATH_VALUES = {"ai_system_id": SYS, "system_id": SVC, "run_id": RUN, "report_id": "asr-none",
               "agent_id": AGENT, "version": "1"}
EXT_CARD = "card-smoke-ext"


#: Routes whose SQL is Postgres-only (window functions, FILTER). SQLite cannot
#: run them, so they are covered when BRUTOR_PG_TESTS=1 (a throwaway Postgres
#: database is created and dropped). Must name real routes: see below.
PG_ONLY = {
    "/admin/actions",
    "/admin/runs/{run_id}/consequences",
    "/admin/runs/{run_id}/trajectory",
}
USE_PG = os.environ.get("BRUTOR_PG_TESTS") == "1"


@pytest.fixture(scope="module")
def database_url():
    if not USE_PG:
        yield None
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    import uuid

    admin = make_url(pg_test_url())
    name = f"brutor_smoke_{uuid.uuid4().hex[:10]}"
    server = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    with server.connect() as c:
        # From template0: the shared server's template1 can carry an older schema.
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    try:
        yield admin.set(database=name).render_as_string(hide_password=False)
    finally:
        with server.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        server.dispose()


@pytest.fixture(scope="module")
def harness(database_url):
    h = build_admin_harness(ROUTERS, logger_name="runs-smoke", database_url=database_url)
    now = datetime.now(timezone.utc)

    def seed(s):
        s.add(ResourceGroup(
            id=SYS, tenant_id=TENANT, name="Smoke", description="s", hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, eu_ai_act_risk_tier="high",
            eu_ai_act_role="provider_and_deployer",
        ))
        # The run's actor is a member agent with a declaration,
        # an observed release and its new-release lifecycle event.
        s.add(AgentIdentity(
            id=AGENT, tenant_id=TENANT, name="Smoke agent", enabled=True, status="active",
            default_deny=True, enforcement_mode="enforce", implementation_name="smoke-agent",
            approved_versions=["1.*"], require_release=True,
        ))
        s.flush()
        s.add(ResourceGroupAgentMember(tenant_id=TENANT, agent_id=AGENT, group_id=SYS,
                                       role="component"))
        s.add(AgentImplementationRelease(
            id="rel-smoke", tenant_id=TENANT, agent_id=AGENT, name="smoke-agent",
            version="2.0.0", build="sha256:" + "e" * 64, trust="verified",
            first_seen_at=now - timedelta(minutes=5), last_seen_at=now, run_count=1,
            fingerprints=["sha256:" + "f" * 64],
        ))
        s.add(AISystemLifecycleEvent(
            id="lce-smoke-rel", tenant_id=TENANT, ai_system_id=SYS,
            event_type="agent.new_release", subject_id="rel-smoke",
            recorded_by="system:agent-releases",
            details={"agent_id": AGENT, "name": "smoke-agent", "version": "2.0.0"},
        ))
        s.flush()
        # Every count populated, so a field added to the summary or the detail
        # is exercised with a real value rather than a default.
        s.add(AISystemRun(
            id=RUN, tenant_id=TENANT, root_task_id=ROOT, ai_system_id=SYS,
            started_at=now - timedelta(minutes=2), ended_at=now, duration_ms=120_000,
            awaiting_human_ms=90_000, active_duration_ms=30_000,
            terminal_state="completed", finalized_by="explicit", is_replay=False,
            actor_type="agent_identity", actor_id=AGENT, agent_id=AGENT,
            agent_impl_name="smoke-agent", agent_impl_version="2.0.0",
            agent_impl_build="sha256:" + "e" * 64, agent_impl_trust="verified",
            implementation_fingerprint="sha256:" + "f" * 64, client_label="python-httpx",
            action_count=9, step_count=3, turn_count=3, turn_attribution="client_asserted",
            llm_call_count=3, tool_call_count=4, skill_call_count=1, a2a_call_count=1,
            max_delegation_depth=1, distinct_tools=4, total_tokens=4200,
            prompt_tokens=3000, completion_tokens=1200, total_cost_usd=0.021,
            error_count=0, tool_error_count=0, approval_request_count=1,
            guardrail_block_count=0, policy_denial_count=0,
            contract_id="ctr-1", contract_hash="sha256:" + "0" * 64,
        ))
        # A shared service this run passed through, with a declared
        # edge, so the composition/dependency routes serve real rows.
        s.add(ResourceGroup(
            id=SVC, tenant_id=TENANT, name="Smoke service", description="s",
            hierarchy_level=0, inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, intended_client_systems=[SYS],
            eu_ai_act_risk_tier="minimal",
        ))
        s.flush()
        s.add(AISystemRunSegment(
            id="seg-smoke", run_id=RUN, tenant_id=TENANT, root_task_id=ROOT,
            root_system_id=SYS, caller_system_id=SYS, segment_system_id=SVC, min_depth=1,
            first_action_at=now - timedelta(minutes=1), last_action_at=now,
            active_ms=1000, action_count=2, llm_call_count=1, tool_call_count=1,
            total_tokens=300, cost_usd=0.002, models_used={"gpt-x": 1},
            caller_agent_id="agent-1", a2a_task_state="completed", outcome="answered",
        ))
        s.add(AISystemDependency(
            id="dep-smoke", tenant_id=TENANT, ai_system_id=SYS, role="shared_service",
            depends_on_system_id=SVC, criticality="critical", contributes_to_decision=True,
            assumptions={}, declared_by="admin",
        ))
        # An external dependency with its supplier, the
        # written agreement on file, and a boundary call with a task state.
        s.add(AgentCard(id=EXT_CARD, tenant_id=TENANT, name="External scorer",
                        version="1.0.0", card_json={"name": "ext", "capabilities": []}))
        s.flush()
        s.add(AISystemDependency(
            id="dep-smoke-ext", tenant_id=TENANT, ai_system_id=SYS, role="external",
            agent_card_id=EXT_CARD, criticality="degradable", fallback="manual review",
            contributes_to_decision=False, separability_rationale="advisory only",
            separability_record_id="ab" * 32, separability_sealed_at=now,
            supplier_legal_entity="Acme Scoring GmbH",
            assumptions={"expected_availability_pct": 99.5, "evidence_provided": ["contract"]},
            declared_by="admin",
        ))
        s.flush()
        ev = AISystemEvidence(id="ev-smoke-supplier", tenant_id=TENANT, ai_system_id=SYS,
                              title="MSA", dependency_id="dep-smoke-ext")
        ev.kind = "supplier_information"
        ev.sha256 = "cd" * 32
        s.add(ev)
        s.add(ProxyLog(
            id="log-smoke-ext", tenant_id=TENANT, timestamp=now - timedelta(minutes=1),
            proxy_type="a2a", http_method="POST", endpoint="/a2a", server_id=EXT_CARD,
            correlation_id="corr-smoke-ext", end_user_session_id="s", success=True,
            http_status_code=200, duration_ms=250, resource_group_id=SYS,
            a2a_task_state="completed",
        ))
    h.seed(seed)
    yield h
    h.close()


def _get_routes():
    for router in ROUTERS:
        for route in router.routes:
            if "GET" in getattr(route, "methods", set()):
                yield route.path


def test_the_router_has_get_routes():
    routes = set(_get_routes())
    assert len(routes) >= 5
    for route in ("/admin/ai-systems/{ai_system_id}/composition",
                    "/admin/ai-systems/{ai_system_id}/dependencies",
                    "/admin/ai-systems/{ai_system_id}/health",
                    "/admin/ai-systems/{ai_system_id}/assurance"):
        assert route in routes
    assert PG_ONLY <= routes, f"PG_ONLY names routes that no longer exist: {PG_ONLY - routes}"


@pytest.mark.parametrize("path", sorted(_get_routes()))
def test_get_route_does_not_500(harness, path):
    if path in PG_ONLY and not USE_PG:
        pytest.skip("Postgres-only SQL; runs with BRUTOR_PG_TESTS=1")
    params = re.findall(r"{(\w+)}", path)
    missing = [p for p in params if p not in PATH_VALUES]
    assert not missing, f"add a seeded value for {missing} to PATH_VALUES"
    url = "/v1" + path.format(**PATH_VALUES)
    r = harness.client.get(url, headers=harness.headers(TENANT))
    assert r.status_code < 500, f"GET {url} -> {r.status_code}: {r.text[:300]}"


def test_list_and_detail_serve_elapsed_active_and_awaiting(harness):
    """a182_run_active_duration — both durations reach the console: the list row AND the detail.

    Pydantic strips undeclared fields, so a column on the model and in
    `to_dict()` is not enough; the response models must declare them.
    """
    listing = harness.client.get(
        f"/v1/admin/ai-systems/{SYS}/runs", headers=harness.headers(TENANT))
    assert listing.status_code == 200, listing.text
    (row,) = [r for r in listing.json()["runs"] if r["id"] == RUN]
    detail = harness.client.get(f"/v1/admin/runs/{RUN}", headers=harness.headers(TENANT))
    assert detail.status_code == 200, detail.text
    for body in (row, detail.json()):
        assert body["duration_ms"] == 120_000
        assert body["awaiting_human_ms"] == 90_000
        assert body["active_duration_ms"] == 30_000


@pytest.mark.parametrize("sid", [SYS, SVC])
def test_health_and_assurance_serve_dependency_fields(harness, sid):
    """The root carries its dependency's health; the
    service is scored on the segment basis. Declared on the response model
    (Pydantic strips undeclared fields), so asserted over HTTP."""
    h = harness.client.get(
        f"/v1/admin/ai-systems/{sid}/health?refresh=true", headers=harness.headers(TENANT))
    assert h.status_code == 200, h.text
    body = h.json()
    report = harness.client.get(
        f"/v1/admin/ai-systems/{sid}/assurance", headers=harness.headers(TENANT))
    assert report.status_code == 200, report.text
    if sid == SYS:
        assert body["signal_basis"] == "runs"
        by_edge = {d["edge_id"]: d for d in body["dependency_health"]}
        dep = by_edge["dep-smoke"]
        assert (dep["system_id"], dep["basis"]) == (SVC, "tested")
        assert by_edge["dep-smoke-ext"]["basis"] == "assumed"  # external edge
        assert {d["edge_id"] for d in report.json()["dependency_health"]} == set(by_edge)
    else:
        assert body["signal_basis"] == "segments"
        assert body["dependency_health"] == [] and body["dependency_cap"] is None
        assert body["signal_levels"]["oversight"]["level"] == "not_applicable"
        assert report.json()["service"]["served_calls"] == 1


def test_health_serves_its_materialised_row_on_a_second_read(harness):
    """The second read takes the staleness path over a stored row (SQLite
    reads timestamps back naive — this 500'd before)."""
    for _ in range(2):
        r = harness.client.get(f"/v1/admin/ai-systems/{SVC}/health",
                               headers=harness.headers(TENANT))
        assert r.status_code == 200, r.text


def test_composite_risk_fields_reach_the_console(harness):
    """Every composite-risk field is DECLARED on its response model
    (Pydantic strips undeclared fields), so asserted over HTTP: the
    service's effective tier in the registry, the sealed separability claim
    on both systems, supplier information on the edge, the boundary
    task-state mix, and the report's composition section."""
    get = lambda url: harness.client.get(url, headers=harness.headers(TENANT))  # noqa: E731
    obligations = get(f"/v1/admin/ai-systems/{SVC}/obligations").json()
    assert obligations["eu_ai_act_risk_tier"] == "minimal"
    assert obligations["risk_tier_evaluated"] == "high"
    assert [s["system_id"] for s in obligations["effective_tier_sources"]] == [SYS]
    for sid in (SYS,):
        comp = get(f"/v1/admin/ai-systems/{sid}/composition").json()
        (decl,) = comp["system"]["separability_declarations"]
        assert decl["sealed"] and decl["record_id"] == "ab" * 32
        ext = next(d for d in comp["dependencies"] if d["id"] == "dep-smoke-ext")
        assert ext["supplier_legal_entity"] == "Acme Scoring GmbH"
        assert ext["separability_record_id"] == "ab" * 32
    deps = get(f"/v1/admin/ai-systems/{SYS}/dependencies").json()["dependencies"]
    assert {d["id"]: d["supplier_legal_entity"] for d in deps}["dep-smoke-ext"] == "Acme Scoring GmbH"
    evidence = get(f"/v1/admin/ai-systems/{SYS}/evidence").json()["evidence"]
    assert [e["dependency_id"] for e in evidence] == ["dep-smoke-ext"]
    health = get(f"/v1/admin/ai-systems/{SYS}/health?refresh=true").json()
    ext_health = next(d for d in health["dependency_health"] if d["edge_id"] == "dep-smoke-ext")
    assert ext_health["boundary"]["task_states"] == {"completed": 1}
    report = get(f"/v1/admin/ai-systems/{SYS}/assurance").json()
    assert {e["edge_id"] for e in report["composition"]["edges"]} == {"dep-smoke", "dep-smoke-ext"}
    listing = {r["id"]: r for r in get("/v1/admin/ai-systems?limit=100").json()["systems"]}
    assert listing[SYS]["separability_sealed"] is True
    assert listing[SVC]["risk_tier_effective"] == "high"


def test_agent_implementation_fields_reach_the_console(harness):
    """Every agent-implementation field is DECLARED on its response model
    (Pydantic strips undeclared fields), so asserted over HTTP: the run's
    release, the identity's declaration and release timeline, each member
    agent's current release on the Composition tab and in the report, and
    the new-release lifecycle event."""
    get = lambda url: harness.client.get(url, headers=harness.headers(TENANT))  # noqa: E731
    detail = get(f"/v1/admin/runs/{RUN}").json()
    (row,) = [r for r in get(f"/v1/admin/ai-systems/{SYS}/runs").json()["runs"] if r["id"] == RUN]
    for body in (detail, row):
        assert (body["agent_impl_name"], body["agent_impl_version"], body["agent_impl_trust"]) == (
            "smoke-agent", "2.0.0", "verified")
        assert body["agent_impl_build"] == "sha256:" + "e" * 64
        assert body["implementation_fingerprint"] == "sha256:" + "f" * 64
        # The normalised client label is on the LIST row as well as the detail.
        assert body["client_label"] == "python-httpx"
    identity = get(f"/v1/admin/agent-identities/{AGENT}").json()
    assert (identity["implementation_name"], identity["approved_versions"],
            identity["require_release"]) == ("smoke-agent", ["1.*"], True)
    rel = get(f"/v1/admin/agent-identities/{AGENT}/releases").json()
    assert rel["current_release_id"] == "rel-smoke"
    (r,) = rel["releases"]
    assert (r["approved"], r["trust"], r["fingerprint_count"], r["current"]) == (
        False, "verified", 1, True)
    (member,) = get(f"/v1/admin/ai-systems/{SYS}/composition").json()["agents"]
    assert member["release"] == {
        "name": "smoke-agent", "version": "2.0.0", "build": "sha256:" + "e" * 64,
        "trust": "verified", "approved": False, "last_seen_at": member["release"]["last_seen_at"],
    }
    report = get(f"/v1/admin/ai-systems/{SYS}/assurance").json()
    (ra,) = report["composition"]["agents"]
    assert ra["release"]["version"] == "2.0.0" and ra["release"]["approved"] is False
    assert [f["code"] for f in report["composition"]["agent_findings"]] == [
        "agent.unapproved_release"]
    lifecycle = get(f"/v1/admin/ai-systems/{SYS}/lifecycle").json()
    assert [e["subject_id"] for e in lifecycle["events"]] == ["rel-smoke"]
    assert lifecycle["history"] == []


def test_composition_connections_reach_the_console(harness):
    """The Simple graph's connector lines — `system_status` and a
    `connection` on every resource, delegation node and observed-undeclared
    dependency — are DECLARED on the response models (Pydantic strips
    undeclared fields), so asserted over HTTP, on Postgres too in the
    fresh-install job (the per-connection query uses a window function)."""
    comp = harness.client.get(
        f"/v1/admin/ai-systems/{SYS}/composition", headers=harness.headers(TENANT)).json()
    fields = {"status", "reason", "calls", "errors", "blocked", "held", "held_lapsed",
              "last_call_at"}
    assert set(comp["system_status"]) == fields
    deps = {d["id"]: d["connection"] for d in comp["dependencies"]}
    for conn in deps.values():
        assert set(conn) == fields
    # The shared service answered its one call in the run's segment.
    assert (deps["dep-smoke"]["status"], deps["dep-smoke"]["reason"]) == ("ok", "1 call, 0 errors")
    for refs in comp["resources"].values():
        for ref in refs:
            assert set(ref["connection"]) == fields

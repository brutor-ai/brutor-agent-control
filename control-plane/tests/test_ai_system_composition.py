# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composite AI Systems — HTTP behavioural spec

Real app, real auth, full-schema SQLite (the shared admin harness). One
class per rule; every rule is a class of bug, not one instance:

  * Dependency edges — each validation rule is one parametrised case; 409 on
    duplicates; authorship is the acting admin, never the body; the response
    carries every declared field (Pydantic strips undeclared ones).
  * Composition findings — each code fires exactly once for its fact and
    clears when the fact is declared.
  * Effective risk tier — carried along contributing edges, transitively;
    a non-contributing edge carries nothing and is shown as separability.
  * The Composition view is the closure: resources (knowledge bases
    included) with inheritance, the gateway's policies/limits, delegation
    nodes joined with declared edges and observed segments.
  * The AI Systems list — composition fields, role filter, and the
    governance-gap filter counting composition findings.
  * Conformance — a root's "models used" are its OWN rows; delegated models
    are reported per dependency; callers are judged against
    `intended_client_systems`.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest
from ulid import ULID

from app.api.v1.endpoints.admin.ai_system_composition import (
    create_ai_system_composition_router,
)
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.api.v1.models.admin.ai_system_composition import (
    CompositionDependency,
    CompositionResponse,
    DependencyEdge,
)
from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_identity import (
    AgentGrant,
    AgentIdentity,
    ResourceGroupAgentMember,
)
from app.database.models.ai_system_contract import AISystemContract
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.kb_collection import KbCollection, resource_group_kb_collections
from app.database.models.llm_model import LLModel
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import (
    GroupType,
    ResourceGroup,
    resource_group_llm_models,
)
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests.admin_endpoint_harness import build_admin_harness

T1, T2 = "t1", "t2"


def _now():
    """Seed-time now — never import time (the full suite collects long before it runs)."""
    return datetime.now(timezone.utc)
_n = count(1)


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("composition-tests")
    harness = build_admin_harness(
        [
            create_ai_system_composition_router(log),
            create_ai_systems_router(log),
            create_resource_groups_router(UsageLimitEnforcer(logger=log), log),
        ],
        logger_name="composition-tests",
    )
    yield harness
    harness.close()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def system(h, gid, *, tenant=T1, tier=None, parent=None, group_type=GroupType.AI_SYSTEM,
           clients=None, owner="team", intended_use="x", stage="active"):
    def fn(s):
        s.add(ResourceGroup(
            id=gid, tenant_id=tenant, name=gid, description=gid,
            hierarchy_level=1 if parent else 0, parent_group_id=parent,
            inherit_resources=True, is_active=True, is_system=False, group_type=group_type,
            system_kind="agent" if group_type == GroupType.AI_SYSTEM else None,
            eu_ai_act_risk_tier=tier, intended_client_systems=clients,
            owner=owner, intended_use=intended_use,
            lifecycle_stage=stage if group_type == GroupType.AI_SYSTEM else None,
        ))
    h.seed(fn)
    return gid


def card(h, cid, tenant=T1):
    h.seed(lambda s: s.add(AgentCard(
        id=cid, tenant_id=tenant, name=f"card {cid}", version="1.0.0",
        card_json={"name": cid, "capabilities": []},
    )))
    return cid


def own_run(h, sid, *, hours_ago=1.0):
    rid = f"run-{next(_n)}"
    h.seed(lambda s: s.add(AISystemRun(
        id=rid, tenant_id=T1, root_task_id=f"root-{rid}", ai_system_id=sid,
        started_at=_now() - timedelta(hours=hours_ago), ended_at=_now() - timedelta(hours=hours_ago),
        terminal_state="completed", finalized_by="explicit", is_replay=False,
        action_count=3, llm_call_count=3,
    )))
    return rid


def delegated(h, root, target, *, outcome="answered", hours_ago=1.0, models=None,
              error_cause=None, held_approval_request_id=None):
    """A run of `root` whose segment passed through `target`."""
    rid = own_run(h, root, hours_ago=hours_ago)
    h.seed(lambda s: s.add(AISystemRunSegment(
        id=f"seg-{next(_n)}", run_id=rid, tenant_id=T1, root_task_id=f"root-{rid}",
        root_system_id=root, caller_system_id=root, segment_system_id=target,
        first_action_at=_now() - timedelta(hours=hours_ago),
        last_action_at=_now() - timedelta(hours=hours_ago) + timedelta(seconds=5),
        action_count=1, llm_call_count=1, outcome=outcome, models_used=models or {},
        error_cause=error_cause, held_approval_request_id=held_approval_request_id,
    )))
    return rid


def held_delegation(h, root, target, *, apr, state, expires_minutes=60, hours_ago=1.0):
    """A delegation stopped only by a hold (a206_segment_held_approval): the stored gateway stop
    that names its approval request. `state=None`: no request on record."""
    from app.database.models.tool_approval_request import ToolApprovalRequest

    if state is not None:
        h.seed(lambda s: s.add(ToolApprovalRequest(
            id=apr, tenant_id=T1, capability_type="a2a", capability_name="card",
            requester_type="agent", requester_group_id=root, status=state,
            timeout_seconds=3600, expires_at=_now() + timedelta(minutes=expires_minutes),
        )))
    return delegated(h, root, target, outcome="failed", hours_ago=hours_ago,
                     error_cause="blocked_by_policy", held_approval_request_id=apr)


def edge(h, root, **body):
    # An external dependency names its supplier (Art 25(4)).
    if body.get("role") == "external":
        body.setdefault("supplier_legal_entity", "Supplier Ltd")
    r = h.client.post(f"/v1/admin/ai-systems/{root}/dependencies", json=body,
                      headers=h.headers(T1))
    assert r.status_code == 201, r.text
    return r.json()


def composition(h, sid, tenant=T1):
    r = h.client.get(f"/v1/admin/ai-systems/{sid}/composition", headers=h.headers(tenant))
    assert r.status_code == 200, r.text
    return r.json()


def codes(body, code):
    return [f for f in body["findings"] if f["code"] == code]


def contract(h, sid, cid, version, status="active"):
    h.seed(lambda s: s.add(AISystemContract(
        id=cid, tenant_id=T1, ai_system_id=sid, version=version,
        contract_hash="sha256:" + "a" * 64, resolved_closure={"schema_version": 3},
        status=status,
    )))


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


# ---------------------------------------------------------------------------
# Dependency edges
# ---------------------------------------------------------------------------


class TestDependencyEdges:
    def test_declare_returns_every_field_with_names_and_authorship(self, h):
        root, svc = system(h, "dp-root"), system(h, "dp-svc")
        c = card(h, "dp-card")
        contract(h, svc, "ctr-dp-1", 1)
        body = edge(h, root, role="shared_service", depends_on_system_id=svc,
                    agent_card_id=c, contract_pin="ctr-dp-1",
                    assumptions={"retention_at_delegate": "30d"},
                    declared_by="mallory")  # never taken from the body
        assert set(body) == set(DependencyEdge.model_fields)
        assert body["id"].startswith("dep-")
        assert body["depends_on_system_name"] == svc
        assert body["agent_card_name"] == f"card {c}"
        assert body["criticality"] == "critical"
        assert body["contributes_to_decision"] is True
        assert body["assumptions"] == {"retention_at_delegate": "30d"}
        assert body["declared_by"] == "admin_t1"
        assert body["updated_by"] == "admin_t1"
        assert body["declared_at"] and body["updated_at"]

        listing = h.client.get(f"/v1/admin/ai-systems/{root}/dependencies",
                               headers=h.headers(T1)).json()
        assert listing["total"] == 1 and listing["dependencies"][0]["id"] == body["id"]

    @pytest.mark.parametrize("case", [
        "self_edge", "shared_without_target", "target_not_ai_system",
        "target_other_tenant", "external_without_card", "external_with_target",
        "degradable_without_fallback", "non_contributing_without_rationale",
        "pin_not_a_contract_of_target", "pin_on_external", "card_other_tenant",
        "assumptions_not_object",
    ])
    def test_each_rule_is_a_422(self, h, case):
        p = f"v-{case}"
        root, svc = system(h, f"{p}-root"), system(h, f"{p}-svc")
        plain = system(h, f"{p}-plain", group_type=GroupType.TEAM)
        foreign = system(h, f"{p}-foreign", tenant=T2)
        c, foreign_card = card(h, f"{p}-card"), card(h, f"{p}-fcard", tenant=T2)
        contract(h, root, f"{p}-ctr-root", 1)  # a contract, but not of the target
        base = {"role": "shared_service", "depends_on_system_id": svc}
        body = {
            "self_edge": {**base, "depends_on_system_id": root},
            "shared_without_target": {"role": "shared_service"},
            "target_not_ai_system": {**base, "depends_on_system_id": plain},
            "target_other_tenant": {**base, "depends_on_system_id": foreign},
            "external_without_card": {"role": "external"},
            "external_with_target": {"role": "external", "agent_card_id": c,
                                     "depends_on_system_id": svc},
            "degradable_without_fallback": {**base, "criticality": "degradable"},
            "non_contributing_without_rationale": {**base, "contributes_to_decision": False},
            "pin_not_a_contract_of_target": {**base, "contract_pin": f"{p}-ctr-root"},
            "pin_on_external": {"role": "external", "agent_card_id": c,
                                "contract_pin": f"{p}-ctr-root"},
            "card_other_tenant": {"role": "external", "agent_card_id": foreign_card},
            "assumptions_not_object": {**base, "assumptions": ["x"]},
        }[case]
        r = h.client.post(f"/v1/admin/ai-systems/{root}/dependencies", json=body,
                          headers=h.headers(T1))
        assert r.status_code == 422, r.text
        # Each case must be refused by ITS rule, not incidentally by another.
        expected = {
            "self_edge": "cannot depend on itself",
            "shared_without_target": "requires depends_on_system_id",
            "target_not_ai_system": "is not an AI System in this tenant",
            "target_other_tenant": "is not an AI System in this tenant",
            "external_without_card": "requires agent_card_id",
            "external_with_target": "declare it as a shared_service",
            "degradable_without_fallback": "requires a fallback",
            "non_contributing_without_rationale": "requires a separability_rationale",
            "pin_not_a_contract_of_target": "is not a contract of",
            "pin_on_external": "shared_service dependencies only",
            "card_other_tenant": "is not an agent card in this tenant",
            "assumptions_not_object": "assumptions",
        }[case]
        assert expected in r.text, r.text

    def test_valid_variants_are_accepted(self, h):
        root, svc = system(h, "ok-root"), system(h, "ok-svc")
        c = card(h, "ok-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc,
             criticality="degradable", fallback="decline to manual review",
             contributes_to_decision=False, separability_rationale="advisory only")
        edge(h, root, role="external", agent_card_id=c)

    def test_duplicates_are_409(self, h):
        root, svc = system(h, "dup-root"), system(h, "dup-svc")
        c = card(h, "dup-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        edge(h, root, role="external", agent_card_id=c)
        for body in ({"role": "shared_service", "depends_on_system_id": svc},
                     {"role": "external", "agent_card_id": c,
                      "supplier_legal_entity": "Supplier Ltd"}):
            r = h.client.post(f"/v1/admin/ai-systems/{root}/dependencies", json=body,
                              headers=h.headers(T1))
            assert r.status_code == 409, r.text

    def test_patch_revalidates_the_merged_edge(self, h):
        root, svc = system(h, "pt-root"), system(h, "pt-svc")
        e = edge(h, root, role="shared_service", depends_on_system_id=svc)
        url = f"/v1/admin/ai-systems/{root}/dependencies/{e['id']}"
        assert h.client.patch(url, json={"criticality": "degradable"},
                              headers=h.headers(T1)).status_code == 422
        assert h.client.patch(url, json={"depends_on_system_id": root},
                              headers=h.headers(T1)).status_code == 422
        r = h.client.patch(url, json={"criticality": "degradable", "fallback": "retry later"},
                           headers=h.headers(T1))
        assert r.status_code == 200, r.text
        assert r.json()["criticality"] == "degradable"
        assert r.json()["fallback"] == "retry later"
        assert r.json()["updated_by"] == "admin_t1"

    def test_delete_and_tenant_isolation(self, h):
        root, svc = system(h, "del-root"), system(h, "del-svc")
        e = edge(h, root, role="shared_service", depends_on_system_id=svc)
        url = f"/v1/admin/ai-systems/{root}/dependencies/{e['id']}"
        assert h.client.get(f"/v1/admin/ai-systems/{root}/dependencies",
                            headers=h.headers(T2)).status_code == 404
        assert h.client.delete(url, headers=h.headers(T2)).status_code == 404
        assert h.client.delete(url, headers=h.headers(T1)).status_code == 204
        assert h.client.get(f"/v1/admin/ai-systems/{root}/dependencies",
                            headers=h.headers(T1)).json()["total"] == 0

    def test_a_plain_group_has_no_dependencies(self, h):
        plain = system(h, "dp-plain", group_type=GroupType.TEAM)
        r = h.client.get(f"/v1/admin/ai-systems/{plain}/dependencies", headers=h.headers(T1))
        assert r.status_code == 422


class TestIntendedClientSystems:
    """The serving side accepts callers by AI System id."""

    def _patch(self, h, gid, value):
        return h.client.patch(f"/v1/admin/resource-groups/{gid}",
                              json={"intended_client_systems": value}, headers=h.headers(T1))

    def test_accepts_ai_systems_normalised(self, h):
        svc, a, b = system(h, "ic-svc"), system(h, "ic-a"), system(h, "ic-b")
        r = self._patch(h, svc, [b, a, b])
        assert r.status_code == 200, r.text
        assert r.json()["intended_client_systems"] == [a, b]
        assert "intended_clients" not in r.json()

    @pytest.mark.parametrize("bad", ["self", "plain", "other_tenant", "missing"])
    def test_rejects_non_systems(self, h, bad):
        svc = system(h, f"icx-{bad}")
        target = {
            "self": svc,
            "plain": system(h, f"icx-{bad}-p", group_type=GroupType.TEAM),
            "other_tenant": system(h, f"icx-{bad}-o", tenant=T2),
            "missing": "no-such-system",
        }[bad]
        assert self._patch(h, svc, [target]).status_code == 422


# ---------------------------------------------------------------------------
# Findings — each fires exactly once and clears when declared
# ---------------------------------------------------------------------------


class TestFindings:
    def test_undeclared_dependency(self, h):
        root, other = system(h, "f1-root"), system(h, "f1-other", clients=["f1-root"])
        delegated(h, root, other)
        found = codes(composition(h, root), "composition.undeclared_dependency")
        assert [f["subject_id"] for f in found] == [other]
        assert found[0]["severity"] == "warning"
        edge(h, root, role="shared_service", depends_on_system_id=other)
        assert codes(composition(h, root), "composition.undeclared_dependency") == []

    def test_unaccepted_caller(self, h):
        root, svc = system(h, "f2-root"), system(h, "f2-svc")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        delegated(h, root, svc)
        found = codes(composition(h, svc), "composition.unaccepted_caller")
        assert [f["subject_id"] for f in found] == [root]
        r = h.client.patch(f"/v1/admin/resource-groups/{svc}",
                           json={"intended_client_systems": [root]}, headers=h.headers(T1))
        assert r.status_code == 200, r.text
        assert codes(composition(h, svc), "composition.unaccepted_caller") == []

    def test_declared_never_observed_only_when_the_system_ran(self, h):
        root, svc = system(h, "f3-root"), system(h, "f3-svc", clients=["f3-root"])
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        # Never ran: an unexercised edge says nothing.
        assert codes(composition(h, root), "composition.declared_never_observed") == []
        own_run(h, root)
        found = codes(composition(h, root), "composition.declared_never_observed")
        assert [f["subject_id"] for f in found] == [svc]
        assert found[0]["severity"] == "info"
        delegated(h, root, svc)
        assert codes(composition(h, root), "composition.declared_never_observed") == []

    def test_role_mismatch(self, h):
        root, other_root = system(h, "f4-root"), system(h, "f4-oroot")
        svc = system(h, "f4-svc")
        c = card(h, "f4-card")
        e = edge(h, root, role="external", agent_card_id=c)
        assert codes(composition(h, root), "composition.role_mismatch") == []
        # Another system identifies the card as an AI System here.
        edge(h, other_root, role="shared_service", depends_on_system_id=svc, agent_card_id=c)
        found = codes(composition(h, root), "composition.role_mismatch")
        assert [f["subject_id"] for f in found] == [c]
        r = h.client.patch(f"/v1/admin/ai-systems/{root}/dependencies/{e['id']}",
                           json={"role": "shared_service", "depends_on_system_id": svc},
                           headers=h.headers(T1))
        assert r.status_code == 200, r.text
        assert codes(composition(h, root), "composition.role_mismatch") == []

    def test_dependency_cycle(self, h):
        a, b, c = system(h, "f5-a"), system(h, "f5-b"), system(h, "f5-c")
        edge(h, a, role="shared_service", depends_on_system_id=b)
        edge(h, b, role="shared_service", depends_on_system_id=c)
        assert codes(composition(h, a), "composition.dependency_cycle") == []
        back = edge(h, c, role="shared_service", depends_on_system_id=a)
        for sid, succ in ((a, b), (b, c), (c, a)):
            found = codes(composition(h, sid), "composition.dependency_cycle")
            assert [f["subject_id"] for f in found] == [succ], sid
        h.client.delete(f"/v1/admin/ai-systems/{c}/dependencies/{back['id']}",
                        headers=h.headers(T1))
        assert codes(composition(h, a), "composition.dependency_cycle") == []


# ---------------------------------------------------------------------------
# Effective risk tier
# ---------------------------------------------------------------------------


class TestEffectiveRiskTier:
    def test_contributing_edges_carry_the_tier_transitively(self, h):
        root = system(h, "rt-root", tier="high")
        svc = system(h, "rt-svc", tier="minimal")
        leaf = system(h, "rt-leaf", tier="limited")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        edge(h, svc, role="shared_service", depends_on_system_id=leaf)
        s = composition(h, svc)["system"]
        assert (s["risk_tier_declared"], s["risk_tier_effective"]) == ("minimal", "high")
        assert s["effective_tier_sources"] == [
            {"system_id": root, "system_name": root, "tier": "high"}]
        assert composition(h, leaf)["system"]["risk_tier_effective"] == "high"
        r = composition(h, root)["system"]
        assert (r["risk_tier_effective"], r["effective_tier_sources"]) == ("high", [])

    def test_a_separable_dependency_keeps_its_own_tier(self, h):
        root = system(h, "rs-root", tier="high")
        svc = system(h, "rs-svc", tier="minimal")
        edge(h, root, role="shared_service", depends_on_system_id=svc,
             contributes_to_decision=False, separability_rationale="logs only")
        s = composition(h, svc)["system"]
        assert s["risk_tier_effective"] == "minimal"
        assert s["effective_tier_sources"] == []
        assert s["separability_declared"] is True
        # Shown on BOTH systems, and the list reads the same value.
        assert composition(h, root)["system"]["separability_declared"] is True
        rows = {r["id"]: r for r in h.client.get(
            "/v1/admin/ai-systems?limit=500", headers=h.headers(T1)).json()["systems"]}
        assert rows[svc]["separability_declared"] is True
        assert rows[root]["separability_declared"] is True
        unrelated = system(h, "rs-unrelated")
        assert composition(h, unrelated)["system"]["separability_declared"] is False
        rows = {r["id"]: r for r in h.client.get(
            "/v1/admin/ai-systems?limit=500", headers=h.headers(T1)).json()["systems"]}
        assert rows[unrelated]["separability_declared"] is False


# ---------------------------------------------------------------------------
# The view — built on the closure
# ---------------------------------------------------------------------------


def _provider(s):
    from app.database.models.llm_provider import LlmProvider

    if s.get(LlmProvider, "prov-comp") is None:
        s.add(LlmProvider(id="prov-comp", tenant_id=T1, key="openai", display_name="OpenAI",
                          enabled=True, is_system=False, import_enabled=False))
        s.flush()
    return "prov-comp"


class TestCompositionView:
    def test_shape_location_resources_and_delegation(self, h):
        org = system(h, "cv-org", group_type=GroupType.ORGANIZATION)
        root = system(h, "cv-root", parent=org, tier="high")
        svc = system(h, "cv-svc", parent=org, clients=["cv-root"])
        c_svc, c_ext, c_undecl, c_denied = (card(h, f"cv-{x}") for x in
                                            ("csvc", "cext", "cundecl", "cdenied"))

        def bind(s):
            s.add(LLModel(id="cv-model", tenant_id=T1, name="cv-model", model_name="cv-model",
                          provider_id=_provider(s)))
            s.add(KbCollection(id="cv-kb", tenant_id=T1, name="Handbook"))
            s.flush()
            s.execute(resource_group_llm_models.insert().values(
                id="cv-root-model", resource_group_id=root, llm_model_id="cv-model"))
            # Bound on the PARENT: the view must say where it comes from.
            s.execute(resource_group_kb_collections.insert().values(
                resource_group_id=org, kb_collection_id="cv-kb"))
            s.get(ResourceGroup, org).llm_global_limits = {"budget": {"daily_limit_usd": 50}}
        h.seed(bind)
        agent_with_grants(h, root, "cv-agent", [
            (c_svc, "allow"), (c_undecl, "approval_required"), (c_denied, "deny")])
        contract(h, svc, "cv-ctr-1", 1, status="superseded")
        contract(h, svc, "cv-ctr-2", 2)
        edge(h, root, role="shared_service", depends_on_system_id=svc, agent_card_id=c_svc)
        edge(h, root, role="external", agent_card_id=c_ext)
        delegated(h, root, svc, outcome="answered")

        body = composition(h, root)
        CompositionResponse.model_validate(body)
        assert set(body) == set(CompositionResponse.model_fields)
        assert [x["id"] for x in body["location"]] == [org, root]
        # The declaration (none) and the current release (never
        # declared one → null) travel with every member agent.
        assert body["agents"] == [{
            "id": "cv-agent", "name": "cv-agent", "implementation_name": None,
            "approved_versions": [], "require_release": False, "release": None,
        }]
        # Every resource carries its connection (no traffic here: idle).
        for key in ("models", "knowledge_bases"):
            for ref in body["resources"][key]:
                assert ref.pop("connection")["status"] == "idle"
        assert body["resources"]["models"] == [
            {"id": "cv-model", "name": "cv-model", "inherited_from": None}]
        assert body["resources"]["knowledge_bases"] == [
            {"id": "cv-kb", "name": "Handbook", "inherited_from": {"id": org, "name": org}}]
        (budget,) = [x for x in body["gateway"]["limits"] if x["kind"] == "budget"]
        assert budget["key"] == "llm.budget.daily_limit_usd" and budget["value"] == 50
        assert budget["inherited_from"] == {"id": org, "name": org}

        deps = {d["agent_card_id"]: d for d in body["dependencies"]}
        for d in body["dependencies"]:
            assert set(d) == set(CompositionDependency.model_fields)
        assert set(deps) == {c_svc, c_ext, c_undecl}  # denied grant reaches nothing
        shared = deps[c_svc]
        assert (shared["declared"], shared["granted"], shared["health"]) == (True, True, "healthy")
        assert shared["observed"]["calls"] == 1 and shared["observed"]["answered"] == 1
        assert shared["target_contract"] == {"id": "cv-ctr-2", "version": 2, "pinned": False}
        assert deps[c_ext]["health"] == "assumed" and deps[c_ext]["granted"] is False
        und = deps[c_undecl]
        assert (und["declared"], und["id"], und["health"]) == (False, None, "unknown")

        dependents = composition(h, svc)["dependents"]
        assert dependents == [{
            "system_id": root, "system_name": root, "declared": True, "edge_declared": True,
            "observed_calls": 1, "last_seen_at": dependents[0]["last_seen_at"]}]
        assert dependents[0]["last_seen_at"]

    def test_mcp_refs_mark_system_servers_and_system_carries_its_kind(self, h):
        """The Composition view's simple graph leaves the platform's own
        system MCP servers (Agent Skills) out of the MCP lane; it can only
        do that if the payload says which binding is one. The rule is the
        proxy's own (`is_system_server`), not a name match."""
        from app.constants.system_server import AgentSkillServer
        from app.database.models.mcp_server import MCPServer
        from app.database.models.resource_group import resource_group_server_configs
        from app.database.models.server_config import ServerConfig

        root = system(h, "sm-root")

        def bind(s):
            for sid, name, url in (
                ("sm-srv-sys", AgentSkillServer.NAME, AgentSkillServer.BASE_URL),
                ("sm-srv-ext", "GitHub", "https://mcp.example.com"),
            ):
                s.add(MCPServer(id=sid, tenant_id=T1, name=name, base_url=url, enabled=True))
                s.flush()
                s.add(ServerConfig(id=f"{sid}-cfg", tenant_id=T1, mcp_server_id=sid,
                                   name=name, enabled=True))
                s.flush()
                s.execute(resource_group_server_configs.insert().values(
                    id=f"{root}-{sid}", resource_group_id=root, server_config_id=f"{sid}-cfg"))
        h.seed(bind)

        body = composition(h, root)
        CompositionResponse.model_validate(body)
        assert body["system"]["system_kind"] == "agent"
        refs = {r["id"]: r for r in body["resources"]["mcp_servers"]}
        for ref in refs.values():
            ref.pop("connection")
        assert refs == {
            # `mcp_server_id` — the server a proxy-log row's server_id names,
            # so the run overlay can place a run's MCP actions on the binding.
            "sm-srv-sys-cfg": {"id": "sm-srv-sys-cfg", "name": AgentSkillServer.NAME,
                               "inherited_from": None, "system": True,
                               "mcp_server_id": "sm-srv-sys"},
            "sm-srv-ext-cfg": {"id": "sm-srv-ext-cfg", "name": "GitHub",
                               "inherited_from": None, "system": False,
                               "mcp_server_id": "sm-srv-ext"},
        }
        # Only MCP refs carry the flag — the other lanes have no system rows.
        for key in ("models", "skills", "knowledge_bases"):
            for ref in body["resources"][key]:
                assert "system" not in ref

    def test_pinned_contract_and_degraded_health(self, h):
        root, svc = system(h, "pd-root"), system(h, "pd-svc", clients=["pd-root"])
        contract(h, svc, "pd-ctr-1", 1, status="superseded")
        contract(h, svc, "pd-ctr-2", 2)
        edge(h, root, role="shared_service", depends_on_system_id=svc, contract_pin="pd-ctr-1")
        delegated(h, root, svc, outcome="answered", hours_ago=3)
        delegated(h, root, svc, outcome="timed_out", hours_ago=1)
        (dep,) = composition(h, root)["dependencies"]
        assert dep["target_contract"] == {"id": "pd-ctr-1", "version": 1, "pinned": True}
        assert dep["health"] == "degraded"
        assert dep["observed"]["timed_out"] == 1 and dep["observed"]["answered"] == 1

    def test_observed_undeclared_and_client_labels(self, h):
        root, other = system(h, "ou-root"), system(h, "ou-other")
        delegated(h, root, other)
        body = composition(h, root)
        assert [x["system_id"] for x in body["observed_undeclared"]] == [other]
        assert body["observed_undeclared"][0]["calls"] == 1


# ---------------------------------------------------------------------------
# The AI Systems list
# ---------------------------------------------------------------------------


class TestFleetList:
    def _list(self, h, **params):
        q = "&".join(f"{k}={v}" for k, v in {"limit": 500, **params}.items())
        r = h.client.get(f"/v1/admin/ai-systems?{q}", headers=h.headers(T1))
        assert r.status_code == 200, r.text
        return {row["id"]: row for row in r.json()["systems"]}

    def test_composition_fields_role_filter_and_gap_filter(self, h):
        root = system(h, "fl-root", tier="high")
        svc = system(h, "fl-svc", tier="minimal", clients=["fl-root"])
        idle = system(h, "fl-idle", clients=["fl-root"])
        c = card(h, "fl-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        edge(h, root, role="shared_service", depends_on_system_id=idle)
        edge(h, root, role="external", agent_card_id=c)
        delegated(h, root, svc)

        rows = self._list(h)
        r, s, i = rows[root], rows[svc], rows[idle]
        assert r["role"] == "root" and s["role"] == "shared_service"
        assert r["composition"]["depends_on"] == 3
        assert r["composition"]["worst_dependency_health"] == "assumed"
        assert r["has_external_dependencies"] is True
        assert s["composition"]["used_by_declared"] == 1
        assert s["composition"]["used_by_observed"] == 1
        assert s["composition"]["idle"] is False and i["composition"]["idle"] is True
        assert set(r["composition"]["members"]) == {"agents", "models", "mcp_servers", "skills"}
        assert (s["risk_tier_effective"], s["effective_tier_sources"][0]["system_id"]) == (
            "high", root)
        assert s["liveness"]["state"] in ("alive", "not_configured")
        # The root declared fl-idle but never exercised it, and
        # declares the external card with no a2a_call grant to reach it.
        assert r["composition_finding_count"] == 2

        assert svc in self._list(h, role="shared_service")
        assert root not in self._list(h, role="shared_service")
        assert root in self._list(h, role="root") and svc not in self._list(h, role="root")
        ext = self._list(h, role="external_deps")
        assert root in ext and svc not in ext
        assert h.client.get("/v1/admin/ai-systems?role=bogus",
                            headers=h.headers(T1)).status_code == 422

        gaps = self._list(h, undocumented_only="true")
        assert root in gaps  # fully documented, but has a composition finding
        assert svc not in gaps

    def test_a_service_answering_callers_is_never_silent_or_unmeasured(self, h):
        root, svc = system(h, "fs-root"), system(h, "fs-svc", clients=["fs-root"])
        h.seed(lambda s: s.add(__import__(
            "app.database.models.ai_system_liveness", fromlist=["x"]
        ).AISystemLivenessExpectation(
            id="live-fs-svc", tenant_id=T1, ai_system_id=svc, mode="on_demand",
        )))
        delegated(h, root, svc)
        assert self._list(h)[svc]["liveness"]["state"] == "alive"


# ---------------------------------------------------------------------------
# Conformance — own rows vs delegated
# ---------------------------------------------------------------------------


class TestConformanceAttribution:
    def test_root_models_are_its_own_and_delegated_models_are_separate(self, h):
        from app.services import ai_system_conformance as conf

        root, svc = system(h, "cf-root"), system(h, "cf-svc")
        child = system(h, "cf-root-team", parent=root, group_type=GroupType.TEAM)
        rid = delegated(h, root, svc, models={"svc-model": 1})

        def logs(s):
            for rg, model in ((root, "root-model"), (child, "team-model"),
                              (svc, "svc-model"), (None, "keyless-model")):
                s.add(ProxyLog(
                    id=f"log-{ULID()}", tenant_id=T1, timestamp=_now() - timedelta(minutes=50),
                    proxy_type="llm", http_method="POST", endpoint="/v1/chat",
                    correlation_id=f"corr-{rid}", end_user_session_id=f"corr-{rid}",
                    success=True, root_task_id=f"root-{rid}", resource_group_id=rg,
                    llm_model_requested=model,
                ))
        h.seed(logs)

        s = h.Factory()
        try:
            used = conf.used_models(s, T1, root, 720)
            via = conf.models_used_via_dependencies(s, T1, root, 720)
        finally:
            s.close()
        # The delegate's model is not the root's; the root's own sub-groups
        # and group-less steps are.
        assert used == ["keyless-model", "root-model", "team-model"]
        assert via == {svc: ["svc-model"]}

    def test_a_granted_model_is_used_when_its_model_name_is_logged(self, h):
        """A grant is recorded by display name ("OpenAI GPT-Test"), a step by
        the model string it sent ("gpt-test"). Compared raw, every model a
        system really uses read "granted but never used" (Brutor Demo
        System: both OpenAI models, conformance 90%, 2026-10-25/26)."""
        from app.services import ai_system_conformance as conf

        root, svc = system(h, "cg-root"), system(h, "cg-svc")

        def bind(s):
            s.add(LLModel(id="cg-model", tenant_id=T1, name="OpenAI GPT-Test", model_name="gpt-test",
                          provider_id=_provider(s)))
            s.add(LLModel(id="cg-idle", tenant_id=T1, name="Idle Model", model_name="idle-model",
                          provider_id=_provider(s)))
            s.flush()
            for mid in ("cg-model", "cg-idle"):
                s.execute(resource_group_llm_models.insert().values(
                    id=f"cg-root-{mid}", resource_group_id=root, llm_model_id=mid))
        h.seed(bind)
        rid = delegated(h, root, svc)
        h.seed(lambda s: s.add(ProxyLog(
            id=f"log-{ULID()}", tenant_id=T1, timestamp=_now() - timedelta(minutes=50),
            proxy_type="llm", http_method="POST", endpoint="/v1/chat",
            correlation_id=f"corr-{rid}", end_user_session_id=f"corr-{rid}",
            success=True, root_task_id=f"root-{rid}", resource_group_id=root,
            llm_model_requested="gpt-test",
        )))
        s = h.Factory()
        try:
            report = conf.for_system(s, s.get(ResourceGroup, root))
            assert "OpenAI GPT-Test" in report.granted_resources
            assert "OpenAI GPT-Test" in report.used_resources
            # The model that really was idle is still reported.
            assert report.unused_resources == ["Idle Model"]
        finally:
            s.rollback()
            s.close()

    def test_callers_are_judged_against_intended_client_systems(self, h):
        from app.services import ai_system_conformance as conf

        root, svc = system(h, "cc-root"), system(h, "cc-svc")
        delegated(h, root, svc)
        s = h.Factory()
        try:
            system_row = s.get(ResourceGroup, svc)
            report = conf.for_system(s, system_row, granted_resources=[])
            assert report.observed_client_systems == {root: 1}
            assert report.undeclared_client_systems == [root]
            assert report.score is not None  # a service with segments is measured
            assert [f.kind for f in report.findings] == ["undeclared_client_system"]

            system_row.intended_client_systems = [root]
            s.flush()
            report = conf.for_system(s, system_row, granted_resources=[])
            assert report.undeclared_client_systems == []
            assert report.findings == []
        finally:
            s.rollback()
            s.close()


# ---------------------------------------------------------------------------
# Knowledge bases in the contract closure
# ---------------------------------------------------------------------------


class TestKnowledgeBasesInClosure:
    def test_conditional_key_and_hash_moves_with_a_kb(self, h):
        from app.services.ai_system_closure import resolve_and_hash

        sid = system(h, "kb-sys")
        s = h.Factory()
        try:
            system_row = s.get(ResourceGroup, sid)
            closure, before = resolve_and_hash(s, system_row)
            assert "knowledge_bases" not in closure["composition"]
            s.add(KbCollection(id="kb-closure", tenant_id=T1, name="Policies"))
            s.flush()
            s.execute(resource_group_kb_collections.insert().values(
                resource_group_id=sid, kb_collection_id="kb-closure"))
            s.flush()
            closure, after = resolve_and_hash(s, system_row)
            assert closure["composition"]["knowledge_bases"] == [{
                "id": "kb-closure", "name": "Policies", "enabled": True,
                "bound_via": sid, "inherited": False}]
            assert before != after
        finally:
            s.rollback()
            s.close()

    def test_a_kb_addition_is_a_widening_in_the_contract_diff(self):
        from app.services.ai_system_contract_service import diff_closures

        before = {"composition": {}}
        after = {"composition": {"knowledge_bases": [{"id": "kb-1"}]}}
        d = diff_closures(before, after)
        assert [w["section"] for w in d["widened"]] == ["knowledge_bases"]


def test_dependency_rows_cascade_with_the_system(h):
    root, svc = system(h, "cas-root"), system(h, "cas-svc")
    edge(h, root, role="shared_service", depends_on_system_id=svc)
    s = h.Factory()
    try:
        s.delete(s.get(ResourceGroup, svc))
        s.commit()
        assert s.query(AISystemDependency).filter_by(ai_system_id=root).count() == 0
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Connection status — the Simple graph's connector lines
# ---------------------------------------------------------------------------


def _log(s, rid, *, rg, proxy_type="llm", server_id=None, skill_id=None, rag=None,
         success=True, status=200, minutes_ago=30, **extra):
    s.add(ProxyLog(
        id=f"log-{ULID()}", tenant_id=T1, timestamp=_now() - timedelta(minutes=minutes_ago),
        proxy_type=proxy_type, http_method="POST", endpoint="/x",
        correlation_id=f"corr-{rid}", end_user_session_id=f"corr-{rid}",
        success=success, http_status_code=status, root_task_id=f"root-{rid}",
        resource_group_id=rg, server_id=server_id, skill_id=skill_id,
        rag_collection_ids=rag, **extra,
    ))


def _hold(s, rid, *, rg, apr, state, expires_minutes=60, proxy_type="mcp", minutes_ago=30,
          **extra):
    """A call held for human approval, linked (as the core links it) by
    `custom_metadata.approval_request_id`. `state=None`: no request on record."""
    from app.database.models.tool_approval_request import ToolApprovalRequest

    if state is not None:
        s.add(ToolApprovalRequest(
            id=apr, tenant_id=T1, capability_type="tool", capability_name="x",
            requester_type="agent", requester_group_id=rg, status=state,
            timeout_seconds=3600,
            expires_at=_now() + timedelta(minutes=expires_minutes),
        ))
    _log(s, rid, rg=rg, proxy_type=proxy_type, proxy_subtype="approval_required",
         success=False, status=202, minutes_ago=minutes_ago,
         custom_metadata={"approval_request_id": apr}, **extra)


class TestConnectionStatus:
    """The wiring of `ai_system_connection_status`: the rules themselves are
    pinned in tests/test_ai_system_connection_status.py."""

    def test_every_connection_in_the_payload_carries_a_status(self, h):
        from app.api.v1.models.admin.ai_system_composition import CompositionConnection

        root, svc = system(h, "cs-shape-root"), system(h, "cs-shape-svc")
        c = card(h, "cs-shape-card")
        edge(h, root, role="external", agent_card_id=c)
        delegated(h, svc, root)  # svc → root: root is a service of svc
        delegated(h, root, svc)  # observed, undeclared
        body = composition(h, root)
        CompositionResponse.model_validate(body)
        conns = [body["system_status"]]
        conns += [r["connection"] for rs in body["resources"].values() for r in rs]
        conns += [d["connection"] for d in body["dependencies"]]
        conns += [o["connection"] for o in body["observed_undeclared"]]
        assert len(conns) >= 3
        for conn in conns:
            assert set(conn) == set(CompositionConnection.model_fields)
            assert conn["status"] in ("ok", "degraded", "down", "idle")

    def test_models_count_only_the_systems_own_rows(self, h):
        root, svc = system(h, "cs-own-root"), system(h, "cs-own-svc")

        def bind(s):
            s.add(LLModel(id="cs-own-model", tenant_id=T1, name="cs-own-model",
                          model_name="cs-own-model", provider_id=_provider(s)))
            s.flush()
            s.execute(resource_group_llm_models.insert().values(
                id="cs-own-rgm", resource_group_id=root, llm_model_id="cs-own-model"))
        h.seed(bind)
        rid = delegated(h, root, svc)

        def logs(s):
            for i in range(12):
                _log(s, rid, rg=root, server_id="cs-own-model", minutes_ago=30 + i)
            # The delegate's failing calls to the same model, inside the
            # root's run, are the delegate's — not the root's.
            for i in range(5):
                _log(s, rid, rg=svc, server_id="cs-own-model", success=False, status=500,
                     minutes_ago=i)
        h.seed(logs)
        (model,) = composition(h, root)["resources"]["models"]
        assert model["connection"]["status"] == "ok"
        assert model["connection"]["reason"] == "12 calls, 0 errors"
        assert (model["connection"]["calls"], model["connection"]["errors"]) == (12, 0)
        assert model["connection"]["last_call_at"]

    def test_mcp_last_three_blocked_is_down_and_keys_through_the_server_config(self, h):
        from app.database.models.mcp_server import MCPServer
        from app.database.models.resource_group import resource_group_server_configs
        from app.database.models.server_config import ServerConfig

        root = system(h, "cs-mcp-root")

        def bind(s):
            s.add(MCPServer(id="cs-mcp-srv", tenant_id=T1, name="Loan Core",
                            base_url="https://mcp.example.com", enabled=True))
            s.flush()
            s.add(ServerConfig(id="cs-mcp-cfg", tenant_id=T1, mcp_server_id="cs-mcp-srv",
                               name="Loan Core", enabled=True))
            s.flush()
            s.execute(resource_group_server_configs.insert().values(
                id="cs-mcp-rg", resource_group_id=root, server_config_id="cs-mcp-cfg"))
        h.seed(bind)
        rid = own_run(h, root)

        def logs(s):
            for i in range(10):
                _log(s, rid, rg=root, proxy_type="mcp", server_id="cs-mcp-srv", minutes_ago=60 + i)
            for i in range(3):
                _log(s, rid, rg=root, proxy_type="mcp", server_id="cs-mcp-srv", success=False,
                     status=403, guardrail_blocked=True, minutes_ago=i)
        h.seed(logs)
        (mcp,) = composition(h, root)["resources"]["mcp_servers"]
        assert mcp["connection"]["status"] == "down"
        assert mcp["connection"]["reason"] == "Last 3 calls blocked by policy"
        assert (mcp["connection"]["calls"], mcp["connection"]["blocked"]) == (13, 3)

    def test_skill_error_rate_degrades_and_kbs_split_their_rows(self, h):
        from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills

        root = system(h, "cs-sk-root")

        def bind(s):
            s.add(AgentSkill(id="cs-skill", tenant_id=T1, name="affordability",
                             description="d", skill_md_content="# x", skill_md_hash="h"))
            for kb, enabled in (("cs-kb-a", True), ("cs-kb-b", False), ("cs-kb-c", True)):
                s.add(KbCollection(id=kb, tenant_id=T1, name=kb, enabled=enabled))
            s.flush()
            s.execute(resource_group_agent_skills.insert().values(
                id="cs-sk-rg", resource_group_id=root, agent_skill_id="cs-skill"))
            for kb in ("cs-kb-a", "cs-kb-b", "cs-kb-c"):
                s.execute(resource_group_kb_collections.insert().values(
                    resource_group_id=root, kb_collection_id=kb))
        h.seed(bind)
        rid = own_run(h, root)

        def logs(s):
            # Skill runs: the executor's own rows, and runs through the
            # Agent Skills system MCP server (subtype skill_run). A model
            # call the skill initiated carries skill_id but is not a run.
            for i in range(10):
                _log(s, rid, rg=root, proxy_type="skill" if i % 2 else "mcp",
                     proxy_subtype=None if i % 2 else "skill_run",
                     server_id=None if i % 2 else "cs-agent-skills-srv", skill_id="cs-skill",
                     success=i != 5, status=500 if i == 5 else 200, minutes_ago=i)
            _log(s, rid, rg=root, proxy_type="llm", server_id="cs-any-model",
                 skill_id="cs-skill", minutes_ago=20)
            for i in range(4):
                _log(s, rid, rg=root, rag="cs-kb-a,cs-kb-b", minutes_ago=i)
        h.seed(logs)
        body = composition(h, root)
        (skill,) = body["resources"]["skills"]
        assert (skill["connection"]["status"], skill["connection"]["reason"]) == (
            "degraded", "1 of 10 calls failed")
        kbs = {k["id"]: k["connection"] for k in body["resources"]["knowledge_bases"]}
        assert (kbs["cs-kb-a"]["status"], kbs["cs-kb-a"]["calls"]) == ("ok", 4)
        # Disabled wins over traffic, and says what the traffic was.
        assert kbs["cs-kb-b"]["status"] == "down"
        assert kbs["cs-kb-b"]["reason"] == "Knowledge base disabled · 4 calls, 0 errors"
        assert kbs["cs-kb-c"]["status"] == "idle"

    def test_a_held_skill_run_is_counted_on_the_skill_as_a_hold(self, h):
        """A call to the Agent Skills run verb names the skill it runs, so a
        hold on it is the SKILL's hold — not only the hidden system
        server's (otherwise the Simple graph shows the skill OK while every
        run of it is held). A hold is counted on its own and is not
        degradation: decided, or still inside its timeout, is all good."""
        from app.constants.system_server import SKILL_RUN_TOOL
        from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills

        root = system(h, "cs-held-root")

        def bind(s):
            s.add(AgentSkill(id="cs-held-skill", tenant_id=T1, name="held-affordability",
                             description="d", skill_md_content="# x", skill_md_hash="h"))
            s.flush()
            s.execute(resource_group_agent_skills.insert().values(
                id="cs-held-rg", resource_group_id=root, agent_skill_id="cs-held-skill"))
        h.seed(bind)
        rid = own_run(h, root)

        def logs(s):
            for i in range(5):
                _log(s, rid, rg=root, proxy_type="mcp", proxy_subtype="skill_run",
                     server_id="cs-agent-skills-srv", skill_id="cs-held-skill",
                     mcp_capability_name=SKILL_RUN_TOOL, minutes_ago=60 + i)
            for i, state in enumerate(("consumed", "rejected", "pending")):
                _hold(s, rid, rg=root, apr=f"cs-held-apr-{i}", state=state,
                      server_id="cs-agent-skills-srv", skill_id="cs-held-skill",
                      mcp_capability_name=SKILL_RUN_TOOL, minutes_ago=i)
            # Loading the skill's manifest names the skill but is not a run.
            _log(s, rid, rg=root, proxy_type="mcp", server_id="cs-agent-skills-srv",
                 skill_id="cs-held-skill", mcp_capability_name="skills__load", minutes_ago=30)
        h.seed(logs)
        (skill,) = composition(h, root)["resources"]["skills"]
        c = skill["connection"]
        assert (c["status"], c["reason"]) == ("ok", "8 calls, 0 errors, 3 held")
        assert (c["calls"], c["blocked"], c["held"], c["held_lapsed"]) == (8, 0, 3, 0)
        assert skill["name"] == "held-affordability"

    def test_a_hold_nobody_decided_before_its_timeout_is_a_warning(self, h):
        """Expired, pending past its deadline (before the core's sweeper
        rewrites it), or with no approval request on record: nobody sorted
        it in time — a warning, never down."""
        from app.database.models.mcp_server import MCPServer
        from app.database.models.resource_group import resource_group_server_configs
        from app.database.models.server_config import ServerConfig

        root = system(h, "cs-lapse-root")

        def bind(s):
            s.add(MCPServer(id="cs-lapse-srv", tenant_id=T1, name="Applications",
                            base_url="https://mcp.example.com", enabled=True))
            s.flush()
            s.add(ServerConfig(id="cs-lapse-cfg", tenant_id=T1, mcp_server_id="cs-lapse-srv",
                               name="Applications", enabled=True))
            s.flush()
            s.execute(resource_group_server_configs.insert().values(
                id="cs-lapse-rg", resource_group_id=root, server_config_id="cs-lapse-cfg"))
        h.seed(bind)
        rid = own_run(h, root)

        def logs(s):
            for i in range(10):
                _log(s, rid, rg=root, proxy_type="mcp", server_id="cs-lapse-srv",
                     minutes_ago=60 + i)
            _hold(s, rid, rg=root, apr="cs-lapse-a", state="expired",
                  server_id="cs-lapse-srv", minutes_ago=3)
            _hold(s, rid, rg=root, apr="cs-lapse-b", state="pending", expires_minutes=-1,
                  server_id="cs-lapse-srv", minutes_ago=2)
            _hold(s, rid, rg=root, apr="cs-lapse-missing", state=None,
                  server_id="cs-lapse-srv", minutes_ago=1)
        h.seed(logs)
        (server,) = composition(h, root)["resources"]["mcp_servers"]
        c = server["connection"]
        assert (c["status"], c["reason"]) == (
            "degraded", "3 holds expired without a decision · 13 calls, 0 errors, 3 held")
        assert (c["blocked"], c["held"], c["held_lapsed"]) == (0, 3, 3)

    def test_a_held_delegation_is_counted_as_a_hold_not_a_block(self, h):
        """The same on the delegation line: a delegation
        held for a person, decided or still inside its timeout, is all good —
        even three in a row. A refusal is still a block."""
        root = system(h, "cs-dh-root")
        svc = system(h, "cs-dh-svc", clients=["cs-dh-root"])
        refused = system(h, "cs-dh-refused", clients=["cs-dh-root"])
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        edge(h, root, role="shared_service", depends_on_system_id=refused)
        for i in range(4):
            delegated(h, root, svc, hours_ago=5 + i)
        for i, state in enumerate(("consumed", "rejected", "pending")):
            held_delegation(h, root, svc, apr=f"cs-dh-apr-{i}", state=state, hours_ago=1 + i)
        for i in range(3):
            delegated(h, root, refused, outcome="failed", error_cause="blocked_by_policy",
                      hours_ago=1 + i)
        deps = {d["depends_on_system_id"]: d for d in composition(h, root)["dependencies"]}
        c = deps[svc]["connection"]
        assert (c["status"], c["reason"]) == ("ok", "7 calls, 0 errors, 3 held")
        assert (c["blocked"], c["held"], c["held_lapsed"]) == (0, 3, 0)
        r = deps[refused]["connection"]
        assert (r["status"], r["blocked"], r["held"]) == ("down", 3, 0)

    def test_a_held_delegation_nobody_decided_in_time_is_a_warning(self, h):
        root = system(h, "cs-dhl-root")
        svc = system(h, "cs-dhl-svc", clients=["cs-dhl-root"])
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        for i in range(10):
            delegated(h, root, svc, hours_ago=5 + i)
        held_delegation(h, root, svc, apr="cs-dhl-expired", state="expired", hours_ago=3)
        held_delegation(h, root, svc, apr="cs-dhl-lapsed", state="pending", expires_minutes=-1,
                        hours_ago=2)
        held_delegation(h, root, svc, apr="cs-dhl-missing", state=None, hours_ago=1)
        deps = {d["depends_on_system_id"]: d for d in composition(h, root)["dependencies"]}
        c = deps[svc]["connection"]
        assert (c["status"], c["reason"]) == (
            "degraded", "3 holds expired without a decision · 13 calls, 0 errors, 3 held")
        assert (c["blocked"], c["held"], c["held_lapsed"]) == (0, 3, 3)

    def test_an_unhealthy_model_with_no_calls_is_down_not_idle(self, h):
        root = system(h, "cs-unh-root")

        def bind(s):
            s.add(LLModel(id="cs-unh-model", tenant_id=T1, name="cs-unh-model",
                          model_name="cs-unh-model", provider_id=_provider(s),
                          health_status="unhealthy", health_check_error="Circuit breaker open"))
            s.flush()
            s.execute(resource_group_llm_models.insert().values(
                id="cs-unh-rgm", resource_group_id=root, llm_model_id="cs-unh-model"))
        h.seed(bind)
        (model,) = composition(h, root)["resources"]["models"]
        assert model["connection"]["status"] == "down"
        assert model["connection"]["reason"] == (
            "Unhealthy: Circuit breaker open · no calls in the last 7 days")

    def test_delegation_reuses_the_dependency_health_and_segments(self, h):
        root, svc = system(h, "cs-dl-root"), system(h, "cs-dl-svc", clients=["cs-dl-root"])
        ok_svc = system(h, "cs-dl-ok", clients=["cs-dl-root"])
        dead = system(h, "cs-dl-dead", clients=["cs-dl-root"])
        ext = card(h, "cs-dl-ext")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        edge(h, root, role="shared_service", depends_on_system_id=ok_svc)
        edge(h, root, role="shared_service", depends_on_system_id=dead)
        edge(h, root, role="external", agent_card_id=ext)
        delegated(h, root, svc, outcome="answered", hours_ago=3)
        delegated(h, root, svc, outcome="timed_out", hours_ago=1)
        for i in range(3):
            delegated(h, root, dead, outcome="timed_out", hours_ago=1 + i)
        for i in range(4):
            delegated(h, root, ok_svc, outcome="answered", hours_ago=1 + i)
        rid = own_run(h, root)
        h.seed(lambda s: [_log(s, rid, rg=root, proxy_type="a2a", server_id=ext, minutes_ago=i)
                          for i in range(3)])
        deps = {d["depends_on_system_id"] or d["agent_card_id"]: d
                for d in composition(h, root)["dependencies"]}
        # THE down rule (one fact, one computation): one unanswered call among the latest
        # is degraded(reliability) → the line is degraded, never down.
        assert deps[svc]["health"] == "degraded"
        assert deps[svc]["connection"]["status"] == "degraded"
        assert deps[svc]["connection"]["reason"].startswith("Failing signal: reliability")
        assert (deps[svc]["connection"]["calls"], deps[svc]["connection"]["errors"]) == (2, 1)
        # The latest three all unanswered: degraded(liveness) → the line is down.
        assert deps[dead]["health"] == "degraded"
        assert deps[dead]["connection"]["status"] == "down"
        assert deps[dead]["connection"]["reason"].startswith("Not answering (liveness)")
        assert (deps[ok_svc]["connection"]["status"], deps[ok_svc]["connection"]["reason"]) == (
            "ok", "4 calls, 0 errors")
        # External: assumed (no signal); the boundary calls decide.
        assert deps[ext]["health"] == "assumed"
        assert (deps[ext]["connection"]["status"], deps[ext]["connection"]["calls"]) == ("ok", 3)

    def test_system_status_is_the_stored_health(self, h):
        from app.database.models.ai_system_health import AISystemHealth

        root = system(h, "cs-sys-root")
        assert composition(h, root)["system_status"]["status"] == "idle"
        h.seed(lambda s: s.add(AISystemHealth(
            id="health-cs-sys", tenant_id=T1, ai_system_id=root, health_state="drifting",
            verdict_status="assured", reasons=["An open behavioural finding"],
        )))
        st = composition(h, root)["system_status"]
        assert st["status"] == "degraded"
        assert st["reason"].startswith("Health: drifting — An open behavioural finding")

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composite AI Systems — risk, compliance, external dependencies

Real app, real auth, full-schema SQLite (the shared admin harness). One class
per rule, each guarding the CLASS of bug:

  * Effective tier drives the registry — a dependency contributing to
    a high-risk decision is evaluated at high by every registry surface
    (applicability, retention floor, board, matrix); separability takes it
    back to its declared tier; a framework without composite classification
    is untouched.
  * Separability is sealed — declaring non-contribution seals an
    `ai.brutor.declaration` (state `separability`, authority = the edge);
    the record id/at is on the edge and on BOTH systems; a changed claim is
    re-sealed, a withdrawn one cleared, an unsealable one saved unsealed
    with a capture-gap item and retried on the next save.
  * Registry resolvers — contributing dependencies assessed / sealed
    separability; supplier information on file; cited by the EU AI Act and
    (the supplier statement) ISO/IEC 42001.
  * `supplier_information` evidence — names one of the system's edges and
    is pinned by hash; no other kind names an edge.
  * External role — strict assumptions schema; supplier entity required;
    boundary metrics carry the A2A task-state mix.
  * Accepted-caller enforcement — AI Systems only; in the closure
    and the diff; the finding says enforced vs advisory and counts refusals.
  * The dependency cap is stored with the state and read back.
  * DC3 and the assurance report's "Composition and dependencies".
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import dataclasses
from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from tests.run_outcome_defaults import work_outcome
from ulid import ULID

from app.api.v1.endpoints.admin.ai_system_composition import (
    create_ai_system_composition_router,
)
from app.api.v1.endpoints.admin.ai_system_evidence import create_ai_system_evidence_router
from app.api.v1.endpoints.admin.ai_system_response import create_ai_system_response_router
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from tests import compliance_evidence_fakes as fakes
from tests.admin_endpoint_harness import build_admin_harness

T1 = "t1"
_n = count(1)
SHA = "ab" * 32


def _now():
    """Read inside a test — never at import."""
    return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("composite-risk-tests")
    harness = build_admin_harness(
        [
            create_ai_system_composition_router(log),
            create_ai_systems_router(log),
            create_ai_system_response_router(log),
            create_ai_system_evidence_router(log),
            create_resource_groups_router(UsageLimitEnforcer(logger=log), log),
        ],
        logger_name="composite-risk-tests",
    )
    yield harness
    harness.close()


@pytest.fixture()
def core(monkeypatch):
    return fakes.install(monkeypatch)


# ---------------------------------------------------------------------------
# Seeding / HTTP helpers
# ---------------------------------------------------------------------------


def system(h, gid, *, tier=None, clients=None, group_type=GroupType.AI_SYSTEM, enforce=False):
    h.seed(lambda s: s.add(ResourceGroup(
        id=gid, tenant_id=T1, name=gid, description=gid, hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False, group_type=group_type,
        system_kind="agent" if group_type == GroupType.AI_SYSTEM else None,
        owner="team", intended_use="x", lifecycle_stage="active",
        eu_ai_act_risk_tier=tier, eu_ai_act_role="provider_and_deployer" if tier else None,
        intended_client_systems=clients, enforce_accepted_callers=enforce,
    )))
    return gid


def card(h, cid):
    h.seed(lambda s: s.add(AgentCard(
        id=cid, tenant_id=T1, name=f"card {cid}", version="1.0.0",
        card_json={"name": cid, "capabilities": []},
    )))
    return cid


def post_edge(h, root, **body):
    return h.client.post(f"/v1/admin/ai-systems/{root}/dependencies", json=body,
                         headers=h.headers(T1))


def edge(h, root, **body):
    r = post_edge(h, root, **body)
    assert r.status_code == 201, r.text
    return r.json()


def patch_edge(h, root, edge_id, **body):
    return h.client.patch(f"/v1/admin/ai-systems/{root}/dependencies/{edge_id}", json=body,
                          headers=h.headers(T1))


def composition(h, sid):
    r = h.client.get(f"/v1/admin/ai-systems/{sid}/composition", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def listing(h):
    r = h.client.get("/v1/admin/ai-systems?limit=500", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["systems"]}


def evidence(h, sid, **body):
    return h.client.post(f"/v1/admin/ai-systems/{sid}/evidence", json=body, headers=h.headers(T1))


def supplier_info(h, sid, dep_id, *, valid_days=365):
    r = evidence(h, sid, kind="supplier_information", title="MSA + DPA", sha256=SHA,
                 dependency_id=dep_id,
                 valid_until=(_now() + timedelta(days=valid_days)).isoformat())
    assert r.status_code == 201, r.text
    return r.json()


def served(h, root, svc, *, outcome="answered", minutes_ago=60.0, caller=None, cause=None):
    """One run of `root` whose segment in `svc` was entered by `caller`
    (default: the root itself — a single hop)."""
    rid = f"run-{next(_n)}"
    at = _now() - timedelta(minutes=minutes_ago)

    def fn(s):
        s.add(AISystemRun(
            id=rid, tenant_id=T1, root_task_id=f"root-{rid}", ai_system_id=root,
            started_at=at, ended_at=at, terminal_state="completed", **work_outcome("completed"), finalized_by="explicit",
            is_replay=False, action_count=3, llm_call_count=3, chain_integrity="intact",
        ))
        s.flush()
        s.add(AISystemRunSegment(
            id=f"seg-{next(_n)}", run_id=rid, tenant_id=T1, root_task_id=f"root-{rid}",
            root_system_id=root, caller_system_id=root if caller is None else (caller or None),
            segment_system_id=svc, min_depth=1,
            first_action_at=at, last_action_at=at + timedelta(seconds=5),
            action_count=2, llm_call_count=1, cost_usd=0.001, outcome=outcome, error_cause=cause,
        ))
    h.seed(fn)


def a2a_row(h, rg, card_id, *, subtype=None, task_state=None, status=200, duration=100):
    h.seed(lambda s: s.add(ProxyLog(
        id=f"log-{ULID()}", tenant_id=T1, timestamp=_now() - timedelta(minutes=30),
        proxy_type="a2a", proxy_subtype=subtype, http_method="POST", endpoint="/a2a",
        server_id=card_id, correlation_id=f"corr-{next(_n)}", end_user_session_id="s",
        success=status < 400, http_status_code=status, duration_ms=duration,
        resource_group_id=rg, a2a_task_state=task_state,
    )))


def session(h):
    return h.Factory()


def eu():
    from app.services.compliance.catalogs import CATALOGS

    return CATALOGS["eu-ai-act"]


def context(h, sid, framework=None):
    from app.services.ai_obligations import system_context

    s = session(h)
    try:
        group = s.get(ResourceGroup, sid)
        return system_context(s, group, framework or eu())
    finally:
        s.close()


def applicability(ctx, key, framework=None):
    from app.services.compliance.catalogs.base import applicability as appl

    return appl((framework or eu()).obligation(key), ctx)[0]


def resolve(h, sid, resolver_id):
    from app.services.compliance.resolvers import ResolverContext, run

    spec = next(
        s for ob in eu().obligations for s in ob.evidence if s.resolver == resolver_id
    )
    s = session(h)
    try:
        now = _now()
        ctx = ResolverContext(db=s, tenant_id=T1, system=s.get(ResourceGroup, sid),
                              window_start=now - timedelta(days=30), window_end=now,
                              framework_id="eu-ai-act", now=now)
        return run(ctx, spec)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Effective tier drives the registry
# ---------------------------------------------------------------------------


class TestEffectiveTierDrivesTheRegistry:
    def test_a_contributing_dependency_of_a_high_risk_system_is_evaluated_at_high(self, h):
        root = system(h, "et-root", tier="high")
        svc = system(h, "et-svc", tier="minimal")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        ctx = context(h, svc)
        assert (ctx.risk_tier, ctx.risk_tier_declared) == ("high", "minimal")
        assert [s["system_id"] for s in ctx.risk_tier_sources] == [root]
        # A high-risk-only obligation now reaches the dependency.
        assert applicability(ctx, "art14-human-oversight") != "not_applicable"
        # The retention floor is registry data too: the effective tier's floor.
        from app.services.compliance.retention_floors import floor_sources
        from app.services.ai_system_composition import load_tier_facts
        from app.database.models.compliance_framework import ComplianceFramework

        s = session(h)
        try:
            fw_row = ComplianceFramework(id="eu-ai-act", title="EU AI Act")
            group = s.get(ResourceGroup, svc)
            with_facts = floor_sources(group, None, [fw_row], load_tier_facts(s, T1))
            declared_only = floor_sources(group, None, [fw_row])
        finally:
            s.close()
        assert [(f.classification, f.floor_days) for f in with_facts] == [("high", 183)]
        assert declared_only == []

    def test_separability_returns_it_to_its_declared_tier(self, h, core):
        root = system(h, "es-root", tier="high")
        svc = system(h, "es-svc", tier="minimal")
        edge(h, root, role="shared_service", depends_on_system_id=svc,
             contributes_to_decision=False, separability_rationale="advisory score only")
        ctx = context(h, svc)
        assert (ctx.risk_tier, ctx.risk_tier_sources) == ("minimal", [])
        assert applicability(ctx, "art14-human-oversight") == "not_applicable"

    def test_the_rule_is_catalog_data_not_code(self, h):
        """A framework without `composite_classification` evaluates the
        declared tier — the rule lives in the registry."""
        root = system(h, "ed-root", tier="high")
        svc = system(h, "ed-svc", tier="minimal")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        plain = dataclasses.replace(eu(), composite_classification=False)
        assert eu().composite_classification is True
        ctx = context(h, svc, plain)
        assert (ctx.risk_tier, ctx.risk_tier_sources) == ("minimal", [])

    def test_every_registry_surface_shows_the_evaluated_tier(self, h):
        from app.services.compliance.evaluation import SystemEvaluator

        root = system(h, "ev-root", tier="high")
        svc = system(h, "ev-svc", tier="limited")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        s = session(h)
        try:
            now = _now()
            ev = SystemEvaluator(s, T1, eu(), s.get(ResourceGroup, svc), None,
                                 (now - timedelta(days=1), now), now=now)
            assert (ev.context.risk_tier, ev.context.risk_tier_declared) == ("high", "limited")
            assert ev.obligation(eu().obligation("art14-human-oversight"),
                                 with_statements=False)["applicability"] != "not_applicable"
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Sealed separability
# ---------------------------------------------------------------------------


class TestSeparabilityIsSealed:
    def test_declaring_non_contribution_seals_a_declaration_shown_on_both_systems(self, h, core):
        root, svc = system(h, "ss-root", tier="high"), system(h, "ss-svc")
        body = edge(h, root, role="shared_service", depends_on_system_id=svc,
                    contributes_to_decision=False, separability_rationale="advisory only")
        (rec,) = core.records("ai.brutor.declaration")
        assert rec["context"]["state"] == "separability"
        assert rec["disposition"]["authority"] == body["id"]
        assert rec["ai_system_id"] == root
        assert rec["payload"]["separability_rationale"] == "advisory only"
        assert body["separability_record_id"] and body["separability_sealed_at"]
        assert body["separability_seal"]["sealed"] is True
        assert body["separability_seal"]["record_id"] == body["separability_record_id"]
        for sid, side in ((root, "declares"), (svc, "declared_on")):
            (decl,) = composition(h, sid)["system"]["separability_declarations"]
            assert (decl["edge_id"], decl["side"], decl["sealed"]) == (body["id"], side, True)
            assert decl["record_id"] == body["separability_record_id"]
        rows = listing(h)
        assert rows[root]["separability_sealed"] and rows[svc]["separability_sealed"]

    def test_a_changed_claim_is_resealed_and_a_withdrawn_one_cleared(self, h, core):
        root, svc = system(h, "sc-root"), system(h, "sc-svc")
        first = edge(h, root, role="shared_service", depends_on_system_id=svc,
                     contributes_to_decision=False, separability_rationale="v1")
        # A save that changes nothing about the claim seals nothing.
        r = patch_edge(h, root, first["id"], criticality="critical")
        assert r.status_code == 200 and r.json()["separability_seal"] is None
        assert r.json()["separability_record_id"] == first["separability_record_id"]
        assert len(core.records("ai.brutor.declaration")) == 1
        r = patch_edge(h, root, first["id"], separability_rationale="v2")
        second = r.json()
        assert second["separability_record_id"] not in (None, first["separability_record_id"])
        assert len(core.records("ai.brutor.declaration")) == 2
        r = patch_edge(h, root, first["id"], contributes_to_decision=True)
        withdrawn = r.json()
        assert withdrawn["separability_record_id"] is None
        assert withdrawn["separability_sealed_at"] is None
        assert len(core.records("ai.brutor.declaration")) == 2

    def test_an_unsealable_claim_is_saved_unsealed_raised_and_retried(self, h, core):
        root, svc = system(h, "su-root"), system(h, "su-svc")
        core.mode = fakes.MODE_UNREACHABLE
        body = edge(h, root, role="shared_service", depends_on_system_id=svc,
                    contributes_to_decision=False, separability_rationale="r")
        assert body["separability_record_id"] is None
        assert body["separability_seal"]["sealed"] is False
        assert body["separability_seal"]["seal_error"] == fakes.MODE_UNREACHABLE
        s = session(h)
        try:
            gaps = s.query(AssuranceInboxItem).filter_by(
                tenant_id=T1, category="capture_gap", ai_system_id=root).all()
        finally:
            s.close()
        assert gaps, "an unsealed declaration must be a visible finding"
        assert composition(h, svc)["system"]["separability_declarations"][0]["sealed"] is False
        core.mode = fakes.MODE_OK
        again = patch_edge(h, root, body["id"], fallback=None).json()
        assert again["separability_record_id"] and again["separability_seal"]["sealed"]


# ---------------------------------------------------------------------------
# Registry resolvers
# ---------------------------------------------------------------------------


class TestContributingDependenciesAssessed:
    RID = "dependencies.contributing_assessed"

    def test_each_edge_kind_is_counted_by_its_rule(self, h, core):
        root = system(h, "ca-root", tier="high")
        svc = system(h, "ca-svc", tier="minimal")
        sep = system(h, "ca-sep", tier="minimal")
        c = card(h, "ca-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc)          # met: effective high
        edge(h, root, role="shared_service", depends_on_system_id=sep,
             contributes_to_decision=False, separability_rationale="r")        # met: sealed
        ext = edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        r = resolve(h, root, self.RID)
        assert (r.status, r.n, r.m) == ("not_met", 2, 3)
        assert r.facts["external_without_supplier_information"] == 1
        assert r.facts["assessed_at_tier"] == 1 and r.facts["separable_sealed"] == 1
        assert len(r.citations) == 1  # the sealed separability declaration
        supplier_info(h, root, ext["id"])
        r = resolve(h, root, self.RID)
        assert (r.status, r.n, r.m) == ("met", 3, 3)

    def test_an_unsealed_separability_claim_is_not_accepted(self, h, core):
        root, svc = system(h, "cu-root", tier="high"), system(h, "cu-svc")
        core.mode = fakes.MODE_DISABLED
        edge(h, root, role="shared_service", depends_on_system_id=svc,
             contributes_to_decision=False, separability_rationale="r")
        r = resolve(h, root, self.RID)
        assert (r.status, r.n, r.m) == ("not_met", 0, 1)
        assert r.facts["separable_unsealed"] == 1

    def test_no_edges_is_not_a_clean_result(self, h):
        root = system(h, "cn-root", tier="high")
        assert resolve(h, root, self.RID).status == "not_evaluable"


class TestSupplierInformationOnFile:
    RID = "dependencies.supplier_information_on_file"

    def test_external_and_other_entity_services_need_it_same_entity_does_not(self, h):
        root = system(h, "si-root", tier="high")
        own, other = system(h, "si-own"), system(h, "si-other")
        c = card(h, "si-card")
        edge(h, root, role="shared_service", depends_on_system_id=own)  # same entity: out of scope
        svc_edge = edge(h, root, role="shared_service", depends_on_system_id=other,
                        supplier_legal_entity="Sister Co B.V.")
        ext = edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        r = resolve(h, root, self.RID)
        assert (r.status, r.n, r.m) == ("not_met", 0, 2)
        supplier_info(h, root, ext["id"])
        supplier_info(h, root, svc_edge["id"], valid_days=-1)  # expired
        r = resolve(h, root, self.RID)
        assert (r.status, r.n, r.m) == ("not_met", 1, 2)
        assert r.facts["expired"] == ["si-other"]
        supplier_info(h, root, svc_edge["id"])
        assert resolve(h, root, self.RID).status == "met"

    def test_nothing_supplied_is_not_evaluable(self, h):
        root, own = system(h, "sn-root", tier="high"), system(h, "sn-own")
        edge(h, root, role="shared_service", depends_on_system_id=own)
        assert resolve(h, root, self.RID).status == "not_evaluable"


class TestCatalogsCiteTheResolvers:
    def test_eu_ai_act_and_iso_share_the_supplier_statement(self):
        from app.services.compliance.catalogs import CATALOGS
        from app.services.compliance.resolvers import resolver_exists

        fw = eu()
        art6 = fw.obligation("art6-composite-classification")
        art25 = fw.obligation("art25-4-supplier-agreements")
        assert [s.resolver for s in art6.evidence] == ["dependencies.contributing_assessed"]
        assert [s.resolver for s in art25.evidence] == ["dependencies.supplier_information_on_file"]
        assert art6.risk_tiers == art25.risk_tiers == frozenset({"high"})
        assert art6.needs_review, "rests on DRAFT Commission guidance"
        iso = CATALOGS["iso-42001"].obligation("a10-third-parties")
        assert art25.evidence[0].identity in {s.identity for s in iso.evidence}
        for spec in art6.evidence + art25.evidence:
            assert resolver_exists(spec.resolver)


# ---------------------------------------------------------------------------
# supplier_information evidence
# ---------------------------------------------------------------------------


class TestSupplierInformationEvidence:
    def test_it_names_one_of_the_systems_edges_and_is_pinned_by_hash(self, h):
        root, other = system(h, "ev-sroot"), system(h, "ev-sother")
        c = card(h, "ev-scard")
        ext = edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        foreign = edge(h, other, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        cases = {
            "no edge": {"kind": "supplier_information", "title": "t", "sha256": SHA},
            "another system's edge": {"kind": "supplier_information", "title": "t", "sha256": SHA,
                                      "dependency_id": foreign["id"]},
            "no hash": {"kind": "supplier_information", "title": "t", "dependency_id": ext["id"]},
            "edge on another kind": {"kind": "dpia", "title": "t", "dependency_id": ext["id"]},
        }
        for label, body in cases.items():
            r = evidence(h, root, **body)
            assert r.status_code == 422, (label, r.text)
        row = supplier_info(h, root, ext["id"])
        assert row["kind"] == "supplier_information" and row["dependency_id"] == ext["id"]
        listed = h.client.get(f"/v1/admin/ai-systems/{root}/evidence", headers=h.headers(T1)).json()
        assert listed["evidence"][0]["dependency_id"] == ext["id"]

    def test_removing_the_edge_keeps_the_pointer_detached(self, h):
        root = system(h, "ev-droot")
        c = card(h, "ev-dcard")
        ext = edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        row = supplier_info(h, root, ext["id"])
        r = h.client.delete(f"/v1/admin/ai-systems/{root}/dependencies/{ext['id']}",
                            headers=h.headers(T1))
        assert r.status_code == 204
        s = session(h)
        try:
            kept = s.get(AISystemEvidence, row["id"])
            assert kept is not None and kept.dependency_id is None
        finally:
            s.close()


# ---------------------------------------------------------------------------
# External role: assumptions, supplier, boundary
# ---------------------------------------------------------------------------


class TestExternalAssumptionsAndSupplier:
    FULL = {
        "data_classes_shared": ["personal_data", "financial"],
        "retention_at_delegate": "30 days",
        "human_oversight_at_delegate": True,
        "expected_availability_pct": 99.5,
        "expected_p95_latency_ms": 1200,
        "evidence_provided": ["signed_agent_card", "contract"],
    }

    def test_the_full_schema_round_trips(self, h):
        root = system(h, "as-root")
        body = edge(h, root, role="external", agent_card_id=card(h, "as-card"),
                    supplier_legal_entity="Acme", assumptions=self.FULL)
        assert body["assumptions"] == self.FULL
        assert body["supplier_legal_entity"] == "Acme"

    @pytest.mark.parametrize("bad", [
        {"retention": "30d"},
        {"expected_availability_pct": 101},
        {"expected_availability_pct": -1},
        {"expected_p95_latency_ms": 1.5},
        {"expected_p95_latency_ms": -3},
        {"human_oversight_at_delegate": "yes"},
        {"data_classes_shared": "pii"},
        {"evidence_provided": ["hearsay"]},
    ], ids=lambda b: next(iter(b)))
    def test_every_violation_is_a_422_on_create_and_patch(self, h, bad):
        root = system(h, f"av-{next(_n)}")
        c = card(h, f"av-card-{next(_n)}")
        r = post_edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme",
                      assumptions=bad)
        assert r.status_code == 422, r.text
        ok = edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        r = patch_edge(h, root, ok["id"], assumptions=bad)
        assert r.status_code == 422, r.text

    def test_the_service_layer_rejects_what_the_api_model_rejects(self, h):
        """Every writer goes through `validate_dependency` — not only HTTP, so
        the rule cannot live in the request model alone."""
        from app.services.ai_system_composition import (
            DependencyValidationError,
            validate_assumptions,
            validate_dependency,
        )

        assert validate_assumptions({"expected_availability_pct": 99}) == {
            "expected_availability_pct": 99.0}
        root, svc = system(h, "sl-root"), system(h, "sl-svc")
        s = session(h)
        try:
            with pytest.raises(DependencyValidationError, match="assumptions.retention"):
                validate_dependency(s, tenant_id=T1, ai_system_id=root, fields={
                    "role": "shared_service", "depends_on_system_id": svc,
                    "assumptions": {"retention": "x"},
                })
            ok = validate_dependency(s, tenant_id=T1, ai_system_id=root, fields={
                "role": "shared_service", "depends_on_system_id": svc,
                "assumptions": {"expected_p95_latency_ms": 900, "retention_at_delegate": None},
            })
            assert ok["assumptions"] == {"expected_p95_latency_ms": 900}
        finally:
            s.close()

    def test_an_external_dependency_names_its_supplier(self, h):
        root = system(h, "sup-root")
        r = post_edge(h, root, role="external", agent_card_id=card(h, "sup-card"))
        assert r.status_code == 422 and "supplier_legal_entity" in r.text
        svc = system(h, "sup-svc")
        assert edge(h, root, role="shared_service", depends_on_system_id=svc)[
            "supplier_legal_entity"] is None

    def test_boundary_metrics_carry_the_task_state_mix(self, h):
        from app.services.ai_system_dependency_health import dependency_health

        root = system(h, "bt-root")
        c = card(h, "bt-card")
        edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        for state in ("completed", "completed", "failed", "input_required", None):
            a2a_row(h, root, c, task_state=state)
        s = session(h)
        try:
            (entry,) = dependency_health(s, s.get(ResourceGroup, root))
        finally:
            s.close()
        assert entry["basis"] == "assumed"
        assert entry["boundary"]["calls"] == 5
        assert entry["boundary"]["task_states"] == {
            "completed": 2, "failed": 1, "input_required": 1}
        r = h.client.get(f"/v1/admin/ai-systems/{root}/health?refresh=true", headers=h.headers(T1))
        assert r.json()["dependency_health"][0]["boundary"]["task_states"]["completed"] == 2


# ---------------------------------------------------------------------------
# Accepted-caller enforcement
# ---------------------------------------------------------------------------


class TestEnforceAcceptedCallers:
    def _patch(self, h, gid, **body):
        return h.client.patch(f"/v1/admin/resource-groups/{gid}", json=body, headers=h.headers(T1))

    def test_ai_systems_only(self, h):
        from pydantic import ValidationError

        from app.api.v1.models.admin.resource_group import (
            ResourceGroupType as ApiGroupType,
            ResourceGroupCreate,
        )

        team = system(h, "en-team", group_type=GroupType.TEAM)
        r = self._patch(h, team, enforce_accepted_callers=True)
        assert r.status_code == 422 and "AI System" in r.text
        with pytest.raises(ValidationError, match="enforce_accepted_callers"):
            ResourceGroupCreate(name="x", group_type=ApiGroupType.TEAM, enforce_accepted_callers=True)
        svc = system(h, "en-svc")
        r = self._patch(h, svc, enforce_accepted_callers=True)
        assert r.status_code == 200, r.text
        assert r.json()["enforce_accepted_callers"] is True
        assert listing(h)[svc]["enforce_accepted_callers"] is True
        assert composition(h, svc)["system"]["enforce_accepted_callers"] is True
        # Demoted away from AI System: the flag goes with it.
        r = self._patch(h, svc, group_type="team")
        assert r.status_code == 200, r.text
        assert r.json()["enforce_accepted_callers"] is False

    def test_it_is_a_signed_term_and_the_diff_classifies_it(self, h):
        from app.services.ai_system_closure import resolve_closure
        from app.services.ai_system_contract_service import diff_closures

        svc = system(h, "ec-svc")
        s = session(h)
        try:
            before = resolve_closure(s, s.get(ResourceGroup, svc))
        finally:
            s.close()
        assert "enforce_accepted_callers" not in before["ai_system"]  # conditional key
        assert self._patch(h, svc, enforce_accepted_callers=True).status_code == 200
        s = session(h)
        try:
            after = resolve_closure(s, s.get(ResourceGroup, svc))
        finally:
            s.close()
        assert after["ai_system"]["enforce_accepted_callers"] is True
        on = diff_closures(before, after)
        assert [e["kind"] for e in on["narrowed"]] == ["caller_enforcement"]
        off = diff_closures(after, before)
        assert [e["kind"] for e in off["widened"]] == ["caller_enforcement"]

    def test_the_finding_says_advisory_or_enforced_and_counts_refusals(self, h):
        root, svc = system(h, "ef-root"), system(h, "ef-svc")
        c = card(h, "ef-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc, agent_card_id=c)
        served(h, root, svc)
        (f,) = [x for x in composition(h, svc)["findings"]
                if x["code"] == "composition.unaccepted_caller"]
        assert "Advisory" in f["message"] and "in 1 run(s)" in f["message"]
        assert self._patch(h, svc, enforce_accepted_callers=True).status_code == 200
        a2a_row(h, root, c, subtype="unaccepted_caller", status=403)
        a2a_row(h, root, c, subtype="unaccepted_caller", status=403)
        (f,) = [x for x in composition(h, svc)["findings"]
                if x["code"] == "composition.unaccepted_caller"]
        assert "Enforced" in f["message"] and "2 call(s) refused" in f["message"]
        assert self._patch(h, svc, intended_client_systems=[root]).status_code == 200
        assert not [x for x in composition(h, svc)["findings"]
                    if x["code"] == "composition.unaccepted_caller"]

    def test_a_refusal_seen_as_both_a_segment_and_a_log_row_is_counted_once(self, h):
        """The core writes a refused segment (`failed`, cause
        blocked_by_policy) for a refused caller that
        declared its own edge, AND the `unaccepted_caller` proxy_logs row:
        one caller, one finding, and a refused call never reads as served."""
        root, svc = system(h, "eb-root"), system(h, "eb-svc", enforce=True)
        c = card(h, "eb-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc, agent_card_id=c)
        served(h, root, svc, outcome="failed", cause="blocked_by_policy")
        a2a_row(h, root, c, subtype="unaccepted_caller", status=403)
        (f,) = [x for x in composition(h, svc)["findings"]
                if x["code"] == "composition.unaccepted_caller"]
        assert "1 call(s) refused" in f["message"] and "served" not in f["message"]

    def test_a_refused_caller_that_was_never_served_is_still_a_finding(self, h):
        root, svc = system(h, "er-root"), system(h, "er-svc", enforce=True)
        c = card(h, "er-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc, agent_card_id=c)
        a2a_row(h, root, c, subtype="unaccepted_caller", status=403)
        found = [x for x in composition(h, svc)["findings"]
                 if x["code"] == "composition.unaccepted_caller"]
        assert [x["subject_id"] for x in found] == [root]


# ---------------------------------------------------------------------------
# The dependency cap is stored with the state
# ---------------------------------------------------------------------------


class TestDependencyCapIsStored:
    def test_the_endpoint_reads_the_cap_the_state_was_scored_under(self, h):
        root = system(h, "dc-root", clients=None)
        svc = system(h, "dc-svc", clients=["dc-root"])
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        served(h, root, svc, outcome="failed")
        r = h.client.get(f"/v1/admin/ai-systems/{root}/health?refresh=true", headers=h.headers(T1))
        body = r.json()
        assert body["health_state"] == "degraded"
        assert body["dependency_cap"]["system_id"] == svc
        s = session(h)
        try:
            row = s.query(AISystemHealth).filter_by(tenant_id=T1, ai_system_id=root).one()
            assert row.dependency_cap["system_id"] == svc
        finally:
            s.close()
        # The dependency answers again, but the row has not been re-scored:
        # the endpoint shows the stored state AND the stored cap together.
        served(h, root, svc, outcome="answered", minutes_ago=1)
        r = h.client.get(f"/v1/admin/ai-systems/{root}/health", headers=h.headers(T1))
        body = r.json()
        assert body["health_state"] == "degraded"
        assert body["dependency_cap"] and body["dependency_cap"]["system_id"] == svc


# ---------------------------------------------------------------------------
# DC3 and the assurance report
# ---------------------------------------------------------------------------


class TestDocumentationListsComponentsAndDependencies:
    def test_dc3_and_annex_iv_list_every_edge_with_its_terms(self, h, core):
        from app.services.compliance.documentation import render_content

        root = system(h, "dd-root", tier="high")
        svc = system(h, "dd-svc")
        c = card(h, "dd-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc,
             contributes_to_decision=False, separability_rationale="advisory")
        ext = edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        supplier_info(h, root, ext["id"])
        s = session(h)
        try:
            group = s.get(ResourceGroup, root)
            soc2 = render_content(s, group, "soc2")
            annex = render_content(s, group, "eu-ai-act")
        finally:
            s.close()
        dc3 = next(x for x in soc2["sections"] if x["id"] == "DC3")
        block = dc3["filled"]["components_and_dependencies"]
        assert "member_components" in block
        by_role = {d["role"]: d for d in block["dependencies"]}
        assert by_role["shared_service"]["assurance_basis"] == "tested"
        assert by_role["shared_service"]["contributes_to_decision"] is False
        assert by_role["shared_service"]["separability"]["sealed"] is True
        assert by_role["external"]["assurance_basis"] == "assumed"
        assert by_role["external"]["supplier_legal_entity"] == "Acme"
        assert by_role["external"]["supplier_information"]["unexpired"] == 1
        for d in block["dependencies"]:
            assert {"criticality", "contract", "contributes_to_decision"} <= set(d)
        dc7 = next(x for x in soc2["sections"] if x["id"] == "DC7")
        assert [x["treatment"] for x in dc7["filled"]["dependency_suppliers"]] == ["carve-out"]
        sec2 = next(x for x in annex["sections"] if x["id"] == "2")
        assert len(sec2["filled"]["components_and_dependencies"]["dependencies"]) == 2


class TestAssuranceReportCompositionSection:
    def test_the_section_joins_the_register_with_the_same_status_and_findings(self, h):
        import jsonschema

        from app.services.ai_system_assurance import build_report
        from app.services.ai_system_composition import CompositionIndex
        from app.services.assurance_checks.composition import composition_findings
        from app.standards import REPORT_SPEC, REPORT_SPEC_VERSION, load_schema

        root = system(h, "rp-root", tier="high")
        svc = system(h, "rp-svc", tier="minimal")
        other = system(h, "rp-other")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        served(h, root, svc)
        served(h, root, other)  # undeclared → a composition finding
        s = session(h)
        try:
            report = build_report(s, s.get(ResourceGroup, root))
            direct = composition_findings(CompositionIndex(s, T1), root)
        finally:
            s.close()
        assert REPORT_SPEC_VERSION == "1.10"  # composition.agents and judgement_exclusions
        sec = report["composition"]
        assert sec["title"] == "Composition and dependencies"
        (e,) = sec["edges"]
        health = {d["edge_id"]: d for d in report["dependency_health"]}
        assert e["status"] == health[e["edge_id"]]["status"]
        assert e["assurance_basis"] == "tested" and e["role"] == "shared_service"
        assert sec["findings"] == direct and any(
            f["code"] == "composition.undeclared_dependency" for f in sec["findings"])
        assert sec["risk_tier_effective"] == "high"
        jsonschema.validate(report, load_schema(REPORT_SPEC))


class TestSupplierIsAContractTerm:
    def test_the_closure_carries_the_supplier_conditionally_and_the_diff_names_it(self, h):
        from app.services.ai_system_closure import resolve_closure
        from app.services.ai_system_contract_service import diff_closures

        root, svc = system(h, "cs-root"), system(h, "cs-svc")
        c = card(h, "cs-card")
        edge(h, root, role="shared_service", depends_on_system_id=svc)
        ext = edge(h, root, role="external", agent_card_id=c, supplier_legal_entity="Acme")
        s = session(h)
        try:
            before = resolve_closure(s, s.get(ResourceGroup, root))
        finally:
            s.close()
        by_role = {d["role"]: d for d in before["dependencies"]}
        assert by_role["external"]["supplier_legal_entity"] == "Acme"
        assert "supplier_legal_entity" not in by_role["shared_service"]  # conditional
        assert patch_edge(h, root, ext["id"], supplier_legal_entity="Acme B.V.").status_code == 200
        s = session(h)
        try:
            after = resolve_closure(s, s.get(ResourceGroup, root))
        finally:
            s.close()
        changed = diff_closures(before, after)["changed"]
        assert [e.get("field") for e in changed if e["kind"] == "dependency_terms"] == [
            "supplier_legal_entity"]



# ---------------------------------------------------------------------------
# The proxies are told (cache invalidation)
# ---------------------------------------------------------------------------


@pytest.fixture()
def published(monkeypatch):
    from app.services import cache_invalidation

    calls = []

    async def fake(_redis, resource_type, tenant_id, resource_id, action="update"):
        calls.append((resource_type, tenant_id, resource_id, action))
        return True

    monkeypatch.setattr(cache_invalidation, "publish_invalidation", fake)
    return calls


class TestProxiesAreToldAboutEnforcementInputs:
    def test_every_edge_write_publishes_ai_system_dependency(self, h, published):
        root, svc = system(h, "ci-root"), system(h, "ci-svc")
        body = edge(h, root, role="shared_service", depends_on_system_id=svc)
        assert ("ai_system_dependency", T1, body["id"], "create") in published
        published.clear()
        assert patch_edge(h, root, body["id"], criticality="critical").status_code == 200
        assert published == [], "a no-op save changes nothing the proxy caches"
        assert patch_edge(h, root, body["id"], fallback="manual", criticality="degradable").status_code == 200
        assert published == [("ai_system_dependency", T1, body["id"], "update")]
        published.clear()
        r = h.client.delete(f"/v1/admin/ai-systems/{root}/dependencies/{body['id']}",
                            headers=h.headers(T1))
        assert r.status_code == 204
        assert published == [("ai_system_dependency", T1, body["id"], "delete")]

    @pytest.mark.parametrize("field,value", [
        ("enforce_accepted_callers", True), ("intended_client_systems", "self-caller"),
    ])
    def test_changing_who_may_call_publishes_resource_group(self, h, published, field, value):
        svc, caller = system(h, f"cr-svc-{field}"), system(h, f"cr-caller-{field}")
        body = {field: [caller] if value == "self-caller" else value}
        r = h.client.patch(f"/v1/admin/resource-groups/{svc}", json=body, headers=h.headers(T1))
        assert r.status_code == 200, r.text
        assert ("resource_group", T1, svc, "update") in published


# ---------------------------------------------------------------------------
# "Caller" = the IMMEDIATE calling AI System, everywhere
# ---------------------------------------------------------------------------


class TestTheCallerIsTheImmediateCallingSystem:
    def test_a_chain_names_the_middle_system_as_the_callers_caller(self, h):
        """A → B → C: C's caller is B (who declares the edge and whom C must
        accept), never A (whose run it is)."""
        from app.services.ai_system_conformance import observed_client_systems

        a = system(h, "ch-a")
        b = system(h, "ch-b", clients=["ch-a"])
        c = system(h, "ch-c")
        edge(h, a, role="shared_service", depends_on_system_id=b)
        served(h, a, b)                 # A calls B
        served(h, a, c, caller=b)       # inside A's run, B calls C
        comp_c = composition(h, c)
        assert [d["system_id"] for d in comp_c["dependents"]] == [b]
        unaccepted = [f["subject_id"] for f in comp_c["findings"]
                      if f["code"] == "composition.unaccepted_caller"]
        assert unaccepted == [b]
        undeclared_a = [f["subject_id"] for f in composition(h, a)["findings"]
                        if f["code"] == "composition.undeclared_dependency"]
        assert c not in undeclared_a, "A never called C; B did"
        undeclared_b = [f["subject_id"] for f in composition(h, b)["findings"]
                        if f["code"] == "composition.undeclared_dependency"]
        assert undeclared_b == [c]
        s = session(h)
        try:
            assert observed_client_systems(s, T1, c, 24) == {b: 1}
        finally:
            s.close()
        assert listing(h)[c]["composition"]["used_by_observed"] == 1

    def test_a_segment_with_no_calling_system_names_no_caller(self, h):
        a, svc = system(h, "nc-a"), system(h, "nc-svc")
        served(h, a, svc, caller="")  # the entering call was in no AI System
        s = session(h)
        try:
            s.query(AISystemRunSegment).filter_by(segment_system_id=svc).update(
                {"caller_system_id": None})
            s.commit()
        finally:
            s.close()
        comp = composition(h, svc)
        assert comp["dependents"] == []
        assert not [f for f in comp["findings"] if f["code"] == "composition.unaccepted_caller"]

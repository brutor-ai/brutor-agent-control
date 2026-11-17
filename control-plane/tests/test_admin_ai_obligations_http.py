# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Obligations API + the register's edge view — HTTP behavioural spec

On the shared admin harness: real auth, full-schema SQLite. Covers the
a150_eu_ai_act_fields surfaces end to end:

  * the obligation board aggregates the fleet per obligation, keeping
    `undetermined` visible (an undeclared role is a to-do, not a pass);
  * the per-system endpoint reflects the (risk × role × GPAI) matrix;
  * component assets expose `owning_systems` + `derived_risk_tier`
    (edge view) while rejecting a stored tier of their own;
  * the eu_ai_act_role round-trips through the resource-group API.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid

import pytest

from app.core.logging_system import BrutorLogger
from app.api.v1.endpoints.admin.ai_obligations import create_ai_obligations_router
from app.api.v1.endpoints.admin.ai_assets import create_ai_assets_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
from app.services.usage_limit_enforcer import UsageLimitEnforcer
from app.database.models.ai_asset import AiAsset
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import (
    GroupType,
    ResourceGroup,
    resource_group_llm_models,
)
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
OTHER = "t2"

COPILOT = "sys-copilot"      # high risk, provider_and_deployer, GPAI model
TRIAGE = "sys-triage"        # limited risk, deployer
UNDECLARED = "sys-undeclared"  # high risk, role not declared
MODEL_ID = "model-gpai"


def seed(session):
    def system(gid, name, risk, role):
        session.add(ResourceGroup(
            id=gid, tenant_id=TENANT, name=name, description=name,
            hierarchy_level=0, inherit_resources=True, is_active=True,
            is_system=False, group_type=GroupType.AI_SYSTEM,
            eu_ai_act_risk_tier=risk, eu_ai_act_role=role,
        ))

    system(COPILOT, "Copilot", "high", "provider_and_deployer")
    system(TRIAGE, "Triage", "limited", "deployer")
    system(UNDECLARED, "Undeclared", "high", None)
    session.add(ResourceGroup(
        id="sys-other-tenant", tenant_id=OTHER, name="other", description="o",
        hierarchy_level=0, inherit_resources=True, is_active=True,
        is_system=False, group_type=GroupType.AI_SYSTEM,
        eu_ai_act_risk_tier="high", eu_ai_act_role="provider",
    ))
    session.flush()

    # A GPAI-flagged model bound to the copilot (the register edge).
    from app.database.models.llm_provider import LlmProvider

    session.add(LlmProvider(
        id="prov-1", tenant_id=TENANT, key="openai",
        display_name="Frontier Labs", enabled=True,
    ))
    session.flush()
    session.add(LLModel(
        id=MODEL_ID, tenant_id=TENANT, name="Frontier GPT",
        model_name="frontier-gpt", provider_id="prov-1", enabled=True,
    ))
    session.flush()
    session.add(AiAsset(
        id=f"aiasset-{uuid.uuid4().hex[:10]}", tenant_id=TENANT,
        asset_kind="llm_model", ref_id=MODEL_ID, display_name="Frontier GPT",
        lifecycle_stage="active", enforcement_mode="off", status="ok",
        source="discovered", gpai_classification="gpai",
    ))
    session.execute(resource_group_llm_models.insert().values(
        id=str(uuid.uuid4()), resource_group_id=COPILOT, llm_model_id=MODEL_ID,
    ))


@pytest.fixture(scope="module")
def harness():
    logger = BrutorLogger("obligations-tests")
    h = build_admin_harness([
        create_ai_obligations_router(logger),
        create_ai_assets_router(logger),
        create_resource_groups_router(UsageLimitEnforcer(logger=logger), logger),
    ])
    h.seed(seed)
    yield h
    h.close()


def get(harness, path, tenant=TENANT, **params):
    r = harness.client.get(path, params=params, headers=harness.headers(tenant))
    assert r.status_code == 200, r.text
    return r.json()


class TestFrameworkCatalog:
    def test_the_catalog_lists_the_eu_ai_act(self, harness):
        fws = get(harness, "/v1/admin/compliance/obligation-frameworks")
        eu = next(f for f in fws if f["id"] == "eu-ai-act")
        assert eu["jurisdiction"] == "EU"
        assert "2026/1744" in eu["reference"]
        assert eu["obligation_count"] >= 11

    def test_an_unknown_framework_is_404_with_the_available_list(self, harness):
        r = harness.client.get(
            "/v1/admin/compliance/obligations",
            params={"framework": "narnia-ai-act"},
            headers=harness.headers(),
        )
        assert r.status_code == 404
        assert "eu-ai-act" in r.json()["detail"]


class TestBoard:
    def test_the_board_aggregates_the_fleet_per_obligation(self, harness):
        board = get(harness, "/v1/admin/compliance/obligations", framework="eu-ai-act")
        assert board["system_count"] == 3
        by_key = {o["key"]: o for o in board["obligations"]}

        # Provider duties: copilot applies; undeclared-role system is
        # UNDETERMINED and stays visible — it is a to-do, not a pass.
        prov = by_key["art12-record-keeping"]
        names = {s["name"]: s["status"] for s in prov["systems"]}
        assert names["Copilot"] == "applies"
        assert names["Undeclared"] == "undetermined"
        assert "Triage" not in names  # deployer → provider duties N/A
        assert prov["undetermined_count"] == 1

        # Art 50(1) binds PROVIDERS (checked against the published
        # text): the provider system and the undeclared-role one are
        # listed; a pure deployer (Triage) is not.
        art50 = by_key["art50-1-ai-interaction-notice"]
        # No profile declares the interaction flag yet: undetermined, visible.
        assert {s["name"] for s in art50["systems"]} >= {"Copilot", "Undeclared"}
        assert "Triage" not in {s["name"] for s in art50["systems"]}
        assert {s["status"] for s in art50["systems"]} == {"undetermined"}

        # The GPAI track flows from the bound model, only for the binder.
        gpai = by_key["art53-gpai"]
        assert {s["name"] for s in gpai["systems"] if s["status"] == "applies"} == {"Copilot"}

    def test_applicability_is_dated_never_boolean(self, harness):
        board = get(harness, "/v1/admin/compliance/obligations")
        by_key = {o["key"]: o for o in board["obligations"]}
        # As of 2026: transparency in force, Annex III still upcoming.
        assert by_key["art50-1-ai-interaction-notice"]["in_force"] is True
        assert by_key["art12-record-keeping"]["in_force"] is False
        assert by_key["art12-record-keeping"]["applies_from"] == "2027-12-02"
        for o in board["obligations"]:
            assert o["applies_from"], "every obligation carries its date"

    def test_tenant_isolation(self, harness):
        board = get(harness, "/v1/admin/compliance/obligations", tenant=OTHER)
        assert board["system_count"] == 1
        for o in board["obligations"]:
            assert all(s["name"] == "other" for s in o["systems"])


class TestPerSystem:
    def test_the_copilot_matrix(self, harness):
        res = get(harness, f"/v1/admin/ai-systems/{COPILOT}/obligations")
        assert res["eu_ai_act_role"] == "provider_and_deployer"
        by_key = {o["key"]: o for o in res["obligations"]}
        assert by_key["art12-record-keeping"]["status"] == "applies"
        assert by_key["art26-6-log-retention-deployer"]["status"] == "applies"
        assert by_key["art53-gpai"]["status"] == "applies"
        assert "Frontier GPT" in by_key["art53-gpai"]["reason"]
        assert by_key["art55-gpai-systemic"]["status"] == "not_applicable"

    def test_the_undeclared_system_says_declare_not_no(self, harness):
        res = get(harness, f"/v1/admin/ai-systems/{UNDECLARED}/obligations")
        by_key = {o["key"]: o for o in res["obligations"]}
        assert by_key["art12-record-keeping"]["status"] == "undetermined"
        assert "declare" in by_key["art12-record-keeping"]["reason"]

    def test_unknown_system_is_404(self, harness):
        r = harness.client.get(
            "/v1/admin/ai-systems/no-such/obligations",
            headers=harness.headers(),
        )
        assert r.status_code == 404

    def test_every_obligation_carries_its_evidence_specs(self, harness):
        """The specs: a list of statements with a basis and a
        resolver, and never a status (statuses live in the matrix)."""
        res = get(harness, f"/v1/admin/ai-systems/{COPILOT}/obligations")
        by_key = {o["key"]: o for o in res["obligations"]}
        for o in res["obligations"]:
            assert isinstance(o["evidence"], list)
        provider = by_key["art12-record-keeping"]["evidence"]
        assert [e["key"] for e in provider] == [
            "records.every_verdict_sealed", "records.witnessed", "records.chain_complete",
        ]
        assert {e["basis"] for e in provider} == {"recomputed"}
        assert "status" not in provider[0]
        deployer = {e["key"]: e for e in by_key["art26-6-log-retention-deployer"]["evidence"]}
        assert deployer["retention.floor"]["params"] == {"floor_days": 183}
        art50 = {e["key"]: e for e in by_key["art50-1-ai-interaction-notice"]["evidence"]}
        assert art50["notice.text_declared"]["basis"] == "declared"
        assert [e["basis"] for e in by_key["art53-gpai"]["evidence"]] == ["declared"]
        # The board carries them too.
        board = get(harness, "/v1/admin/compliance/obligations")
        board_by_key = {o["key"]: o for o in board["obligations"]}
        assert [e["key"] for e in board_by_key["art72-post-market-monitoring"]["evidence"]] == ["monitoring.daily_health"]


class TestRegisterEdgeView:
    def test_a_component_asset_lists_its_owning_systems(self, harness):
        assets = get(harness, "/v1/admin/assets", asset_kind="llm_model")["assets"]
        model = next(a for a in assets if a["ref_id"] == MODEL_ID)
        assert [o["id"] for o in model["owning_systems"]] == [COPILOT]
        # Derived, worst-of, from the owning system's declared tier.
        assert model["derived_risk_tier"] == "high"
        assert model["risk_tier"] is None  # nothing stored on the component

    def test_writing_a_tier_onto_a_component_is_rejected_with_the_lesson(self, harness):
        assets = get(harness, "/v1/admin/assets", asset_kind="llm_model")["assets"]
        model = next(a for a in assets if a["ref_id"] == MODEL_ID)
        r = harness.client.patch(
            f"/v1/admin/assets/{model['id']}",
            json={"risk_tier": "high"},
            headers=harness.headers(),
        )
        assert r.status_code in (400, 422)
        assert "Annex III" in r.json()["detail"]

    def test_gpai_flag_is_llm_model_only(self, harness):
        r = harness.client.post(
            "/v1/admin/assets",
            json={"asset_kind": "mcp_server", "ref_id": "ext-mcp",
                  "gpai_classification": "gpai"},
            headers=harness.headers(),
        )
        assert r.status_code in (400, 422)
        assert "llm_model" in r.json()["detail"]


class TestRoleRoundTrip:
    def test_role_survives_the_resource_group_api(self, harness):
        r = harness.client.patch(
            f"/v1/admin/resource-groups/{TRIAGE}",
            json={"eu_ai_act_role": "provider"},
            headers=harness.headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["eu_ai_act_role"] == "provider"

        # The obligations recompute from the new declaration immediately.
        res = get(harness, f"/v1/admin/ai-systems/{TRIAGE}/obligations")
        assert res["eu_ai_act_role"] == "provider"


class TestMatrix:
    """The board carries the system × obligation matrix; each
    cell the worst statement status and n-of-m per basis, never summed."""

    def test_the_board_carries_the_matrix(self, harness):
        from app.services.compliance.resolvers import STATEMENT_STATUSES

        board = get(harness, "/v1/admin/compliance/obligations")
        m = board["matrix"]
        assert m["window"]["from"] < m["window"]["to"]
        assert m["catalog_versions"] == ["2026-09"]
        rows = {r["ai_system_id"]: r for r in m["systems"]}
        assert set(rows) == {COPILOT, TRIAGE, UNDECLARED}
        cells = {c["obligation_key"]: c for c in rows[COPILOT]["cells"]}
        assert set(cells) == {o["key"] for o in board["obligations"]}
        assert cells["art12-record-keeping"]["applicability"] == "applies"
        assert cells["art12-record-keeping"]["worst_status"] in STATEMENT_STATUSES
        # Not applicable ⇒ no statements evaluated, no status implied.
        assert cells["art55-gpai-systemic"]["applicability"] == "not_applicable"
        assert cells["art55-gpai-systemic"]["worst_status"] is None
        for cell in cells.values():
            assert set(cell["by_basis"]) <= {"recomputed", "judged"}
            assert "%" not in cell["summary"]

    def test_the_matrix_can_be_skipped_and_scoped(self, harness):
        assert get(harness, "/v1/admin/compliance/obligations", include_matrix="false")["matrix"] is None
        scoped = get(harness, "/v1/admin/compliance/obligations", ai_system_id=TRIAGE)["matrix"]
        assert [r["ai_system_id"] for r in scoped["systems"]] == [TRIAGE]

    def test_a_bad_window_is_422(self, harness):
        r = harness.client.get(
            "/v1/admin/compliance/obligations",
            params={"from": "2026-09-10T00:00:00Z", "to": "2026-09-01T00:00:00Z"},
            headers=harness.headers(),
        )
        assert r.status_code == 422

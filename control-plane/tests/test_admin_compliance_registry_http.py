# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Framework registry + compliance profiles — HTTP behavioural spec

On the shared admin harness. The registry rows come from the same boot
seed a fresh install runs, so the test also proves that seed populates an
empty database.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.api.v1.endpoints.admin.compliance_registry import create_compliance_registry_router
from app.core.logging_system import BrutorLogger
from app.database.models.compliance_framework import ComplianceFramework
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.database.seed_compliance_frameworks import seed_compliance_frameworks
from tests.admin_endpoint_harness import build_admin_harness
from tests.compliance_offered import offer


@pytest.fixture(autouse=True)
def _sample_frameworks_offered(monkeypatch):
    """SOC 2 and ISO 42001 are this module's sample frameworks; they are not
    offered in the product (their texts are licensed). The gate is pinned by
    test_compliance_offering.py; here they stand in for any offered one."""
    offer(monkeypatch, "soc2", "iso-42001")

TENANT = "t1"
OTHER = "t2"
COPILOT = "sys-copilot"
FRESH = "sys-fresh"
ACCESS_GROUP = "grp-access"

EXPECTED_ORDER = [
    "eu-ai-act", "iso-42001", "soc2", "hipaa", "gdpr", "dora", "nis2", "nist-ai-rmf",
    "colorado", "korea", "brazil",
]


def seed(session):
    assert seed_compliance_frameworks(session) == len(EXPECTED_ORDER)
    # Idempotent: a second run changes nothing.
    assert seed_compliance_frameworks(session) == 0

    t1 = session.get(Tenant, TENANT)
    t1.compliance_profile = {"frameworks": ["hipaa", "soc2"]}

    def group(gid, tenant, gtype, **kw):
        session.add(ResourceGroup(
            id=gid, tenant_id=tenant, name=gid, description=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False, group_type=gtype, **kw,
        ))

    group(COPILOT, TENANT, GroupType.AI_SYSTEM, eu_ai_act_risk_tier="high", eu_ai_act_role="deployer")
    group(FRESH, TENANT, GroupType.AI_SYSTEM)
    group(ACCESS_GROUP, TENANT, GroupType.CUSTOM)
    group("sys-other", OTHER, GroupType.AI_SYSTEM, eu_ai_act_risk_tier="limited")


@pytest.fixture(autouse=True)
def _seal(monkeypatch):
    """Profile saves seal a declaration through the core; never reach a
    real core from unit tests. Default: sealing succeeds."""
    calls = []

    def fake_seal(tenant_id, records, **_kw):
        calls.append((tenant_id, records))
        return {"sealed": [{"record_id": f"{len(calls):064x}", "log_id": "l", "seq": len(calls)}]}

    monkeypatch.setattr("app.services.evidence.core_client.seal", fake_seal)
    return calls


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_compliance_registry_router(BrutorLogger("registry-tests"))])
    h.seed(seed)
    yield h
    h.close()


def get(harness, path, tenant=TENANT, expect=200, **params):
    r = harness.client.get(path, params=params, headers=harness.headers(tenant))
    assert r.status_code == expect, r.text
    return r.json()


def profile_of(harness, tenant):
    s = harness.Factory()
    try:
        return dict(s.get(Tenant, tenant).compliance_profile or {})
    finally:
        s.close()


def group_of(harness, gid):
    s = harness.Factory()
    try:
        g = s.get(ResourceGroup, gid)
        return g.eu_ai_act_role, g.eu_ai_act_risk_tier
    finally:
        s.close()


class TestFrameworks:
    def test_registry_rows_sorted_with_enablement_and_counts(self, harness):
        items = get(harness, "/v1/admin/compliance/frameworks")["items"]
        assert [f["id"] for f in items] == EXPECTED_ORDER
        by_id = {f["id"]: f for f in items}
        assert by_id["eu-ai-act"]["enabled"] is False
        assert by_id["hipaa"]["enabled"] is True and by_id["soc2"]["enabled"] is True
        assert by_id["eu-ai-act"]["obligation_count"] >= 11
        # Every registry row now has a catalog; the count is the catalog's.
        from app.services.compliance.catalogs import CATALOGS

        assert by_id["dora"]["obligation_count"] == len(CATALOGS["dora"].obligations)
        # Checked against the published text.
        assert by_id["colorado"]["verified_against_instrument"] is True
        assert by_id["eu-ai-act"]["retention_floor_days"] == 183
        assert by_id["eu-ai-act"]["deadline_rules"]["serious_incident"]["standard_days"] == 15
        assert by_id["dora"]["verified_against_instrument"] is True
        # A bill, not law: never verified, never offered.
        assert by_id["brazil"]["verified_against_instrument"] is False and by_id["brazil"]["offered"] is False
        assert by_id["gdpr"]["tag_key"] == "gdpr_art_30" and by_id["nis2"]["tag_key"] is None
        assert by_id["eu-ai-act"]["catalog_version"] == "2026-09"

    def test_enabling_a_tagging_framework_writes_the_a070_key(self, harness):
        r = harness.client.patch(
            "/v1/admin/compliance/frameworks/eu-ai-act", json={"enabled": True}, headers=harness.headers()
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True and r.json()["id"] == "eu-ai-act"
        assert "eu_ai_act" in profile_of(harness, TENANT)["frameworks"]
        # Still enabled through the list; the other two untouched.
        by_id = {f["id"]: f for f in get(harness, "/v1/admin/compliance/frameworks")["items"]}
        assert by_id["eu-ai-act"]["enabled"] and by_id["hipaa"]["enabled"] and by_id["soc2"]["enabled"]

        r = harness.client.patch(
            "/v1/admin/compliance/frameworks/eu-ai-act", json={"enabled": False}, headers=harness.headers()
        )
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert "eu_ai_act" not in profile_of(harness, TENANT)["frameworks"]
        assert sorted(profile_of(harness, TENANT)["frameworks"]) == ["hipaa", "soc2"]

    def test_enabling_an_untagged_framework_uses_registry_enabled(self, harness):
        r = harness.client.patch(
            "/v1/admin/compliance/frameworks/dora", json={"enabled": True}, headers=harness.headers()
        )
        assert r.status_code == 200 and r.json()["enabled"] is True
        prof = profile_of(harness, TENANT)
        assert prof["registry_enabled"] == ["dora"]
        assert "dora" not in prof["frameworks"], "an untagged framework must not reach the tagging engine"
        r = harness.client.patch(
            "/v1/admin/compliance/frameworks/dora", json={"enabled": False}, headers=harness.headers()
        )
        assert r.status_code == 200 and profile_of(harness, TENANT)["registry_enabled"] == []

    def test_unknown_framework_is_404(self, harness):
        r = harness.client.patch(
            "/v1/admin/compliance/frameworks/narnia", json={"enabled": True}, headers=harness.headers()
        )
        assert r.status_code == 404 and "eu-ai-act" in r.json()["detail"]

    def test_tenant_isolation(self, harness):
        by_id = {f["id"]: f for f in get(harness, "/v1/admin/compliance/frameworks", tenant=OTHER)["items"]}
        assert not any(f["enabled"] for f in by_id.values())


class TestProfiles:
    def test_default_prefills_the_eu_block_from_the_group(self, harness):
        body = get(harness, f"/v1/admin/compliance/ai-systems/{COPILOT}/profile")
        assert body["exists"] is False
        assert body["core"] == {}
        assert body["frameworks"] == {"eu-ai-act": {"role": "deployer", "risk_tier": "high"}}
        assert body["declaration_record_id"] is None

    def test_put_round_trips_and_writes_through_to_the_group(self, harness):
        payload = {
            "core": {
                "operator_role": "operator",
                "jurisdictions": ["EU", "UK"],
                "interacts_with_natural_persons": True,
                "automated_decisions_about_persons": False,
                "sensitivity": {"personal_data": True, "phi": False},
                "notes": "internal copilot",
            },
            "frameworks": {
                "eu-ai-act": {"role": "provider_and_deployer", "risk_tier": "high", "annex_iii_area": "8"},
                "hipaa": {"covered_entity": False, "phi_in_scope": False},
            },
        }
        r = harness.client.put(
            f"/v1/admin/compliance/ai-systems/{COPILOT}/profile", json=payload, headers=harness.headers()
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["exists"] is True
        assert body["sealed"] is True and len(body["declaration_record_id"]) == 64
        assert body["core"] == payload["core"]
        assert body["frameworks"] == payload["frameworks"]
        assert body["updated_at"] is not None
        assert group_of(harness, COPILOT) == ("provider_and_deployer", "high")

        again = get(harness, f"/v1/admin/compliance/ai-systems/{COPILOT}/profile")
        assert again["exists"] is True and again["frameworks"]["hipaa"]["phi_in_scope"] is False

        # Update: a changed tier reaches the group; the row is replaced, not merged.
        payload["frameworks"]["eu-ai-act"]["risk_tier"] = "limited"
        del payload["frameworks"]["hipaa"]
        r = harness.client.put(
            f"/v1/admin/compliance/ai-systems/{COPILOT}/profile", json=payload, headers=harness.headers()
        )
        assert r.status_code == 200, r.text
        assert "hipaa" not in r.json()["frameworks"]
        assert group_of(harness, COPILOT) == ("provider_and_deployer", "limited")

    def test_declaring_a_fresh_system_creates_the_row(self, harness):
        r = harness.client.put(
            f"/v1/admin/compliance/ai-systems/{FRESH}/profile",
            json={"core": {"operator_role": "builder"}, "frameworks": {}},
            headers=harness.headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["exists"] is True and r.json()["core"] == {"operator_role": "builder"}
        assert group_of(harness, FRESH) == (None, None)

    @pytest.mark.parametrize(
        "body",
        [
            {"core": {"operator_role": "overlord"}},
            {"core": {"jurisdictions": "EU"}},
            {"core": {"interacts_with_natural_persons": "yes"}},
            {"core": {"made_up": 1}},
            {"frameworks": {"narnia": {}}},
            {"frameworks": {"eu-ai-act": {"role": "bystander"}}},
            {"frameworks": {"eu-ai-act": {"risk_tier": "extreme"}}},
            {"frameworks": {"soc2": "yes"}},
        ],
    )
    def test_invalid_declarations_are_422(self, harness, body):
        r = harness.client.put(
            f"/v1/admin/compliance/ai-systems/{COPILOT}/profile", json=body, headers=harness.headers()
        )
        assert r.status_code == 422, r.text
        # And nothing moved on the group.
        assert group_of(harness, COPILOT) == ("provider_and_deployer", "limited")

    def test_non_ai_system_group_is_422(self, harness):
        get(harness, f"/v1/admin/compliance/ai-systems/{ACCESS_GROUP}/profile", expect=422)
        r = harness.client.put(
            f"/v1/admin/compliance/ai-systems/{ACCESS_GROUP}/profile",
            json={"core": {}, "frameworks": {}},
            headers=harness.headers(),
        )
        assert r.status_code == 422

    def test_unknown_and_foreign_systems_are_404(self, harness):
        get(harness, "/v1/admin/compliance/ai-systems/no-such/profile", expect=404)
        get(harness, f"/v1/admin/compliance/ai-systems/{COPILOT}/profile", tenant=OTHER, expect=404)
        body = get(harness, "/v1/admin/compliance/ai-systems/sys-other/profile", tenant=OTHER)
        assert body["frameworks"]["eu-ai-act"] == {"role": None, "risk_tier": "limited"}


class TestSeedIsCodeOwned:
    def test_a_drifted_row_is_repaired_on_the_next_boot(self, harness):
        s = harness.Factory()
        try:
            # Settle the rows under this module's offered set first, so the
            # count below is exactly the one hand-edited row.
            seed_compliance_frameworks(s)
            s.commit()
            row = s.get(ComplianceFramework, "soc2")
            row.title = "hand-edited"
            row.retention_floor_days = 1
            s.commit()
            assert seed_compliance_frameworks(s) == 1
            s.commit()
            row = s.get(ComplianceFramework, "soc2")
            assert row.title == "SOC 2 (AICPA Trust Services Criteria)"
            assert row.retention_floor_days == 365
        finally:
            s.close()


class TestDeclarationSealing:
    """Every save seals an `ai.brutor.declaration` whose
    payload is the profile (digest only); an unsealed save is still saved."""

    BODY = {"core": {"operator_role": "operator", "interacts_with_natural_persons": True},
            "frameworks": {"eu-ai-act": {"role": "deployer", "risk_tier": "high"}}}

    def put(self, harness):
        r = harness.client.put(
            f"/v1/admin/compliance/ai-systems/{COPILOT}/profile", json=self.BODY, headers=harness.headers()
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_success_stores_the_record_id_and_seals_the_profile_payload(self, harness, _seal):
        body = self.put(harness)
        assert body["sealed"] is True and body["seal_error"] is None
        tenant, records = _seal[-1]
        assert tenant == TENANT and len(records) == 1
        rec = records[0]
        assert rec["record_type"] == "ai.brutor.declaration" and rec["verdict"] == "recorded"
        assert rec["kind"] == "fyi" and rec["ai_system_id"] == COPILOT
        assert rec["disposition"] == {"decision": "accept", "approver": "human", "human_disposed": True}
        # The profile goes in as the payload (the core digests it); nothing
        # identifying the admin enters the record.
        assert rec["payload"]["core"]["operator_role"] == "operator"
        assert "admin" not in str(rec["context"])
        again = get(harness, f"/v1/admin/compliance/ai-systems/{COPILOT}/profile")
        assert again["declaration_record_id"] == body["declaration_record_id"] and again["sealed"] is True

    @pytest.mark.parametrize("error", ["evidence_disabled", "core_unreachable"])
    def test_an_unsealed_save_is_saved_and_says_so(self, harness, monkeypatch, error):
        monkeypatch.setattr(
            "app.services.evidence.core_client.seal",
            lambda tenant_id, records, **_kw: {"error": error, "detail": "x"},
        )
        body = self.put(harness)
        assert body["exists"] is True and body["core"]["operator_role"] == "operator"
        assert body["sealed"] is False and body["seal_error"] == error
        # A stale id would cite a declaration that no longer matches the save.
        assert body["declaration_record_id"] is None
        s = harness.Factory()
        try:
            from app.database.models.assurance_inbox import AssuranceInboxItem

            items = s.query(AssuranceInboxItem).filter(
                AssuranceInboxItem.tenant_id == TENANT, AssuranceInboxItem.category == "capture_gap"
            ).all()
            assert any(i.fingerprint == f"capture_gap:cp_seal:ai.brutor.declaration:{COPILOT}" for i in items)
        finally:
            s.close()

    @pytest.mark.parametrize(
        "body",
        [
            {"core": {"oversight_assignment": "x" * 2001}},
            {"core": {"external_registrations": [{"framework_id": "narnia", "id": "1"}]}},
            {"core": {"dates": {"placed_on_market": "not-a-date"}}},
            {"frameworks": {"eu-ai-act": {"made_up": "x"}}},
            {"frameworks": {"hipaa": {"nested": {"a": 1}}}},
            {"frameworks": {"soc2": {"Bad Key": 1}}},
        ],
    )
    def test_pydantic_validation(self, harness, body):
        r = harness.client.put(
            f"/v1/admin/compliance/ai-systems/{COPILOT}/profile", json=body, headers=harness.headers()
        )
        assert r.status_code == 422, r.text

    def test_legacy_columns_are_read_into_the_eu_block(self, harness):
        # A classification changed on the group (the a150_eu_ai_act_fields board path) shows
        # in the profile view even though the row was saved earlier.
        s = harness.Factory()
        try:
            g = s.get(ResourceGroup, COPILOT)
            g.eu_ai_act_risk_tier = "limited"
            s.commit()
        finally:
            s.close()
        body = get(harness, f"/v1/admin/compliance/ai-systems/{COPILOT}/profile")
        assert body["frameworks"]["eu-ai-act"]["risk_tier"] == "limited"

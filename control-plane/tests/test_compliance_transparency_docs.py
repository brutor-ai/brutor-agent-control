# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Notice configs, the transparency summary and documentation skeletons

* Notice texts are stored with SHA-256 digests of their UTF-8 bytes —
  byte-for-byte what the portal hashes; every save seals an
  `ai.brutor.notice-config` record that references each digest; an
  asserted exemption seals its own `ai.brutor.declaration`.
* The transparency summary reads the notice evidence through the shared
  resolver engine.
* A documentation export is a skeleton that says so, has a stable digest
  (re-requesting an unchanged document reuses the export, no new record)
  and is sealed as `ai.brutor.documentation`.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import hashlib

import pytest

from app.api.v1.endpoints.admin.compliance_documentation import create_compliance_documentation_router
from app.api.v1.endpoints.admin.compliance_transparency import create_compliance_transparency_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.ai_system_evidence import AISystemEvidence
from app.database.models.compliance_documentation_export import ComplianceDocumentationExport
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import resolvers, sealing
from app.services.compliance.resolvers._base import MET, Outcome
from tests import compliance_evidence_fakes as fakes
from tests.admin_endpoint_harness import build_admin_harness
from tests.compliance_offered import offer


@pytest.fixture(autouse=True)
def _sample_frameworks_offered(monkeypatch):
    """SOC 2 and ISO 42001 are this module's sample frameworks; they are not
    offered in the product (licensed texts). The gate is pinned by
    test_compliance_offering.py; here they stand in for any offered one."""
    offer(monkeypatch, "soc2", "iso-42001")

SV = "Du interagerar med ett AI-system — svaren genereras automatiskt."
EN = "You are chatting with an AI system."


def seed(session):
    for gid, tenant in (("sys-a", "t1"), ("sys-b", "t1"), ("sys-x", "t2")):
        session.add(ResourceGroup(
            id=gid, tenant_id=tenant, name=gid, description=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
            owner="Risk team", intended_use="Answer policy questions for employees.",
            eu_ai_act_risk_tier="high" if gid == "sys-a" else None,
        ))
    session.flush()
    session.add(AISystemComplianceProfile(
        ai_system_id="sys-a", tenant_id="t1",
        core={"interacts_with_natural_persons": True, "generates_synthetic_content": False},
        frameworks={"eu-ai-act": {"role": "deployer", "risk_tier": "high"}},
    ))
    session.add(AISystemEvidence(
        id="ev-1", tenant_id="t1", ai_system_id="sys-a", kind="impact_assessment", title="FRIA 2026",
        uri="https://docs.example/fria.pdf", sha256="ab" * 32,
    ))


@pytest.fixture(scope="module")
def harness():
    logger = BrutorLogger("transparency-tests")
    h = build_admin_harness([
        create_compliance_transparency_router(logger),
        create_compliance_documentation_router(logger),
    ])
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(autouse=True)
def core(monkeypatch):
    return fakes.install(monkeypatch)


def put(harness, body, tenant="t1", **params):
    return harness.client.put("/v1/admin/compliance/notice-configs", json=body, params=params,
                              headers=harness.headers(tenant))


class TestNoticeConfigs:
    def test_digests_are_sha256_of_utf8_text(self, harness, core):
        r = put(harness, {"surface": "banner", "texts": {"sv": SV, "en": EN}})
        assert r.status_code == 200, r.text
        cfg = r.json()["config"]
        assert cfg["text_digests"] == {
            "sv": hashlib.sha256(SV.encode("utf-8")).hexdigest(),
            "en": hashlib.sha256(EN.encode("utf-8")).hexdigest(),
        }
        assert cfg["ai_system_id"] is None and cfg["surface"] == "banner"
        assert r.json()["seal"]["sealed"] is True and cfg["record_id"] == r.json()["seal"]["record_id"]
        assert r.json()["declaration"] is None
        (rec,) = core.records("ai.brutor.notice-config")
        assert sorted(ref["digest"] for ref in rec["references"]) == sorted(cfg["text_digests"].values())
        assert all(ref["type"] == "ai.brutor.notice-text" for ref in rec["references"])
        # The text itself never leaves as anything but a payload digest input.
        assert SV not in str({k: v for k, v in rec.items() if k != "payload"})
        assert SV not in str(rec["payload"])

    def test_system_override_and_effective_resolution(self, harness):
        put(harness, {"surface": "both", "texts": {"en": EN}})
        r = put(harness, {"surface": "inline", "texts": {"en": "System-specific notice."}}, ai_system_id="sys-a")
        assert r.status_code == 200, r.text
        eff = harness.client.get("/v1/admin/compliance/notice-configs/effective",
                                 params={"ai_system_id": "sys-a"}, headers=harness.headers()).json()
        assert eff["source"] == "system" and eff["surface"] == "inline"
        eff_b = harness.client.get("/v1/admin/compliance/notice-configs/effective",
                                   params={"ai_system_id": "sys-b"}, headers=harness.headers()).json()
        assert eff_b["source"] == "tenant"
        eff_t2 = harness.client.get("/v1/admin/compliance/notice-configs/effective",
                                    headers=harness.headers("t2")).json()
        assert eff_t2["source"] == "default" and eff_t2["enabled"] is True and eff_t2["texts"] == {}
        items = harness.client.get("/v1/admin/compliance/notice-configs", headers=harness.headers()).json()["items"]
        assert [i["ai_system_id"] for i in items] == [None, "sys-a"]

    def test_exemption_is_a_sealed_declaration(self, harness, core):
        body = {"surface": "both", "texts": {"en": EN},
                "exemption": {"asserted": True, "basis": "obvious_from_context",
                              "rationale": "Internal developers testing the model directly."}}
        r = put(harness, body, ai_system_id="sys-b")
        assert r.status_code == 200, r.text
        decl = r.json()["declaration"]
        assert decl["sealed"] is True
        assert r.json()["config"]["exemption"]["declaration_record_id"] == decl["record_id"]
        (rec,) = core.records("ai.brutor.declaration")
        assert rec["payload"]["basis"] == "obvious_from_context"
        assert rec["context"]["state"] == "notice_exemption_asserted"
        # Saving the same assertion again does not re-declare it.
        core.reset()
        r = put(harness, body, ai_system_id="sys-b")
        assert r.json()["declaration"] is None and not core.records("ai.brutor.declaration")
        assert r.json()["config"]["exemption"]["declaration_record_id"] == decl["record_id"]

    @pytest.mark.parametrize("bad", [
        {"surface": "popup"},
        {"texts": {"not a locale!": "x"}},
        {"texts": {"en": "   "}},
        {"exemption": {"asserted": True, "basis": "because"}},
        {"exemption": {"asserted": True, "basis": "other", "rationale": ""}},
    ])
    def test_validation(self, harness, bad):
        assert put(harness, {"texts": {"en": EN}, **bad}).status_code == 422

    def test_unknown_or_foreign_system_is_404(self, harness):
        assert put(harness, {"texts": {"en": EN}}, ai_system_id="sys-x").status_code == 404

    @pytest.mark.parametrize("mode", [fakes.MODE_DISABLED, fakes.MODE_UNREACHABLE])
    def test_unsealed_save_is_kept(self, harness, core, mode):
        core.mode = mode
        r = put(harness, {"surface": "both", "texts": {"en": "Changed while the core is down."}})
        assert r.status_code == 200
        assert r.json()["seal"]["sealed"] is False and r.json()["seal"]["seal_error"] == mode
        assert r.json()["config"]["record_id"] is None
        assert r.json()["config"]["texts"]["en"] == "Changed while the core is down."

    def test_delete_seals_and_falls_back(self, harness, core):
        put(harness, {"texts": {"en": EN}})
        r = put(harness, {"texts": {"en": "temp"}}, ai_system_id="sys-b")
        cid = r.json()["config"]["id"]
        assert harness.client.delete(f"/v1/admin/compliance/notice-configs/{cid}",
                                     headers=harness.headers("t2")).status_code == 404
        r = harness.client.delete(f"/v1/admin/compliance/notice-configs/{cid}", headers=harness.headers())
        assert r.status_code == 200 and r.json()["seal"]["sealed"] is True
        assert core.records("ai.brutor.notice-config")[-1]["context"]["state"] == "deleted"
        eff = harness.client.get("/v1/admin/compliance/notice-configs/effective",
                                 params={"ai_system_id": "sys-b"}, headers=harness.headers()).json()
        assert eff["source"] == "tenant"


class TestTransparencySummary:
    def test_notice_evidence_comes_from_the_resolver_engine(self, harness, monkeypatch):
        calls = []

        def fake_notice(ctx, spec):
            calls.append(ctx.system_id)
            return Outcome(MET, 3, 3, "3 of 3 conversations.", citations=["aa" * 32])

        monkeypatch.setitem(resolvers.RESOLVERS, "notice.before_first_turn", fake_notice)
        monkeypatch.setitem(resolvers.RESOLVERS, "notice.text_declared",
                            lambda ctx, spec: Outcome(MET, detail="declared"))
        monkeypatch.setitem(resolvers.RESOLVERS, "synthetic.marking_declared",
                            lambda ctx, spec: Outcome("undetermined", detail="not declared"))
        r = harness.client.get("/v1/admin/compliance/transparency", params={"ai_system_id": "sys-a"},
                               headers=harness.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["notice_evidence"]["n"] == 3 and body["notice_evidence"]["m"] == 3
        assert body["notice_text"]["status"] == "declared"  # basis discipline: declared never 'met'
        assert body["synthetic_content"]["status"] == "undetermined"
        assert body["interacts_with_natural_persons"] is True
        assert body["generates_synthetic_content"] is False
        assert any("C2PA" in line for line in body["limits"])

        estate = harness.client.get("/v1/admin/compliance/transparency", headers=harness.headers()).json()
        assert estate["notice_evidence"]["n"] == 6 and estate["notice_evidence"]["m"] == 6  # two systems
        assert estate["notice_text"] is None


class TestDocumentation:
    URL = "/v1/admin/ai-systems/{}/documentation"

    def test_eu_annex_iv_skeleton_sealed_with_stable_digest(self, harness, core):
        r = harness.client.get(self.URL.format("sys-a"), params={"framework": "eu-ai-act"}, headers=harness.headers())
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["template"] == "eu-annex-iv" and doc["skeleton"] is True and doc["reused"] is False
        content = doc["content"]
        assert "skeleton" in content["notice"] and "not legal advice" in content["notice"]
        assert [s["id"] for s in content["sections"]] == [str(i) for i in range(1, 10)]
        assert content["sections"][0]["filled"]["intended_purpose"] == "Answer policy questions for employees."
        assert doc["digest"] == sealing.json_digest(content)
        assert doc["seal"]["sealed"] is True and doc["record_id"] == doc["seal"]["record_id"]
        (rec,) = core.records("ai.brutor.documentation")
        assert rec["context"]["framework_id"] == "eu-ai-act"
        assert rec["references"][0]["digest"] == doc["digest"]

        # Unchanged inputs ⇒ same export, no new record.
        again = harness.client.get(self.URL.format("sys-a"), params={"framework": "eu-ai-act"},
                                   headers=harness.headers()).json()
        assert again["id"] == doc["id"] and again["reused"] is True and again["digest"] == doc["digest"]
        assert len(core.records("ai.brutor.documentation")) == 1

        # A changed input ⇒ new digest, new record.
        s = harness.Factory()
        s.get(ResourceGroup, "sys-a").owner = "Model risk committee"
        s.commit()
        s.close()
        changed = harness.client.get(self.URL.format("sys-a"), params={"framework": "eu-ai-act"},
                                     headers=harness.headers()).json()
        assert changed["digest"] != doc["digest"] and changed["reused"] is False
        assert len(core.records("ai.brutor.documentation")) == 2

    @pytest.mark.parametrize("fid,template", [("iso-42001", "iso-a6-2-7"), ("soc2", "soc2-system-description"),
                                              ("gdpr", "generic")])
    def test_templates(self, harness, fid, template):
        doc = harness.client.get(self.URL.format("sys-b"), params={"framework": fid},
                                 headers=harness.headers()).json()
        assert doc["template"] == template and doc["framework_id"] == fid

    def test_html_rendering_says_skeleton(self, harness):
        r = harness.client.get(self.URL.format("sys-a"), params={"framework": "soc2", "format": "html"},
                               headers=harness.headers())
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "Skeleton, not a claim." in r.text and "To complete" in r.text

    def test_unsealed_export_is_kept(self, harness, core):
        core.mode = fakes.MODE_UNREACHABLE
        doc = harness.client.get(self.URL.format("sys-b"), params={"framework": "hipaa"},
                                 headers=harness.headers()).json()
        assert doc["seal"]["sealed"] is False and doc["record_id"] is None
        s = harness.Factory()
        assert s.get(ComplianceDocumentationExport, doc["id"]) is not None
        s.close()

    def test_errors_and_isolation(self, harness):
        assert harness.client.get(self.URL.format("sys-a"), params={"framework": "nope"},
                                  headers=harness.headers()).status_code == 404
        assert harness.client.get(self.URL.format("sys-x"), headers=harness.headers()).status_code == 404
        lst = harness.client.get(self.URL.format("sys-a") + "/exports", headers=harness.headers()).json()
        assert lst["total"] >= 1 and all(i["ai_system_id"] == "sys-a" for i in lst["items"])

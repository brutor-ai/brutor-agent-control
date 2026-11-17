# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence Reports, oversight, evidence findings, scheduler

The resolvers themselves are exercised against Postgres in
`test_compliance_resolvers_pg.py`; here they are replaced by deterministic
fakes keyed on `(resolver, params)` — exactly the identity the RFC says
must give one answer under every framework — so the report pipeline can be
pinned on SQLite:

* idempotent on the period key; `force` regenerates in place;
* recomputed and judged are never merged; judged rows carry agreement;
* the same fixture month under EU AI Act, ISO/IEC 42001 and SOC 2 gives
  identical rows wherever they cite the same statement;
* a monthly report requests a bundle with the evidence-report extension
  (framework id inside); unsealed ⇒ saved, `record_id` NULL, `sealed: false`;
* the Assurance Report cites the latest report;
* `evidence_unwitnessed` and `capture_gap` inbox findings.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.compliance_reports import create_compliance_reports_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.compliance_report import ComplianceReport
from app.database.models.evidence_bundle import EvidenceBundle
from app.database.models.evidence_log import EvidenceLog, EvidenceTreeHead
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.database.seed_compliance_frameworks import seed_compliance_frameworks
from app.services.compliance import evidence_findings, reports, resolvers
from app.services.compliance import scheduler as compliance_scheduler
from app.services.compliance.resolvers import _judge
from app.services.compliance.resolvers._base import INDICATOR, MET, NOT_MET, Outcome
from tests import compliance_evidence_fakes as fakes
from tests.admin_endpoint_harness import build_admin_harness
from tests.compliance_offered import offer


@pytest.fixture(autouse=True)
def _sample_frameworks_offered(monkeypatch):
    """SOC 2 and ISO 42001 are this module's sample frameworks; they are not
    offered in the product (licensed texts). The gate is pinned by
    test_compliance_offering.py; here they stand in for any offered one."""
    offer(monkeypatch, "soc2", "iso-42001")

AUG = date(2026, 8, 12)
BASE = "/v1/admin/compliance/reports"


def seed(session):
    seed_compliance_frameworks(session)
    session.get(Tenant, "t1").compliance_profile = {"frameworks": ["eu_ai_act", "iso_42001", "soc2"]}
    for gid, tenant in (("sys-a", "t1"), ("sys-b", "t1"), ("sys-x", "t2")):
        session.add(ResourceGroup(
            id=gid, tenant_id=tenant, name=gid, description=gid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False, group_type=GroupType.AI_SYSTEM,
            eu_ai_act_risk_tier="high" if gid == "sys-a" else "limited",
            eu_ai_act_role="provider_and_deployer",
        ))
    session.flush()
    session.add(AISystemComplianceProfile(
        ai_system_id="sys-a", tenant_id="t1",
        core={"interacts_with_natural_persons": True, "generates_synthetic_content": True,
              "oversight_assignment": "Model risk desk"},
        frameworks={"eu-ai-act": {"role": "provider_and_deployer", "risk_tier": "high"}, "soc2": {"in_scope": True}},
    ))


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_compliance_reports_router(BrutorLogger("report-tests"))])
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture(autouse=True)
def core(monkeypatch):
    return fakes.install(monkeypatch)


def _fake_outcome(spec):
    digest = hashlib.sha256(repr(spec.identity).encode()).digest()
    m = 3 + digest[0] % 5
    n = m if digest[1] % 2 else m - 1
    return Outcome(MET if n == m else NOT_MET, n, m, f"{n} of {m} (fake)", citations=[digest.hex()])


@pytest.fixture(autouse=True)
def fake_resolvers(monkeypatch):
    calls = []

    def fake(ctx, spec):
        calls.append((ctx.system_id, spec.resolver))
        if spec.basis == "indicator":
            return Outcome(INDICATOR, detail="movement: none")
        return _fake_outcome(spec)

    for rid in list(resolvers.RESOLVERS):
        monkeypatch.setitem(resolvers.RESOLVERS, rid, fake)

    def fake_judge(ctx, spec):
        calls.append((ctx.system_id, spec.resolver))
        return Outcome(MET, 57, 60, "Judged: met 57 of 60.",
                       facts={"agreement": {"chk-1": {"n": 34, "observed_agreement": 0.92, "blind": True}}})

    monkeypatch.setattr(_judge, "judge", fake_judge)
    return calls


@pytest.fixture
def db(harness):
    s = harness.Factory()
    yield s
    s.rollback()
    s.close()


class TestPeriods:
    def test_period_arithmetic(self):
        assert reports.period_for("daily", AUG) == (AUG, AUG)
        assert reports.period_for("weekly", AUG) == (date(2026, 8, 10), date(2026, 8, 16))
        assert reports.period_for("monthly", AUG) == (date(2026, 8, 1), date(2026, 8, 31))
        assert reports.previous_period("monthly", date(2026, 9, 19)) == (date(2026, 8, 1), date(2026, 8, 31))
        assert reports.previous_period("weekly", date(2026, 9, 16)) == (date(2026, 9, 7), date(2026, 9, 13))
        assert reports.previous_period("daily", date(2026, 9, 19)) == (date(2026, 9, 18), date(2026, 9, 18))


class TestGeneration:
    def test_monthly_report_sealed_and_bundled(self, db, core):
        g = reports.generate(db, "t1", "eu-ai-act", cadence="monthly", day=AUG, ai_system_id="sys-a", actor="t")
        db.commit()
        row = g.report
        assert g.created and row.status == "ready" and row.catalog_version == "2026-09"
        assert (row.period_start, row.period_end) == (date(2026, 8, 1), date(2026, 8, 31))
        keys = {r["obligation_key"] for r in row.rows}
        assert "art50-1-ai-interaction-notice" in keys and "art4-ai-literacy" in keys
        assert "art12-record-keeping" not in keys, "high-risk duties are not in force yet (in force from: 2027-12-02)"
        assert all(r["provision_note"] == "Not legal advice." for r in row.rows)
        (rec,) = core.records("ai.brutor.report")
        assert rec["context"] == {
            "record_type": "ai.brutor.report", "framework_id": "eu-ai-act", "catalog_version": "2026-09",
            "report_id": row.id, "cadence": "monthly", "period_start": "2026-08-01", "period_end": "2026-08-31",
        }
        assert rec["payload"]["rows"] == row.rows and rec["ai_system_id"] == "sys-a"
        assert row.record_id == g.seal.record_id
        (bundle_call,) = core.bundle_calls
        ext = bundle_call["extensions"][reports.REPORT_EXTENSION]
        assert ext["framework_id"] == "eu-ai-act" and ext["report_id"] == row.id and ext["rows"] == row.rows
        assert bundle_call["selector"]["record_ids"][0] == row.record_id
        assert bundle_call["framework_id"] == "eu-ai-act"
        assert row.bundle_id == "bdl-fake-%d" % core._n
        census = row.summary["scope_census"]
        assert census["human_signed"] is False and "never proven" in census["note"]

    def test_idempotent_on_the_period_key_and_force(self, db, core):
        first = reports.generate(db, "t1", "iso-42001", cadence="weekly", day=AUG)
        db.commit()
        again = reports.generate(db, "t1", "iso-42001", cadence="weekly", day=AUG + timedelta(days=2))
        assert again.report.id == first.report.id and not again.created and again.seal is None
        assert len(core.records("ai.brutor.report")) == 1
        forced = reports.generate(db, "t1", "iso-42001", cadence="weekly", day=AUG, force=True)
        db.commit()
        assert forced.report.id == first.report.id and forced.seal is not None and not forced.created
        assert len(core.records("ai.brutor.report")) == 2
        assert not core.bundle_calls, "only monthly reports are bundled"
        assert db.query(ComplianceReport).filter(ComplianceReport.framework_id == "iso-42001",
                                                 ComplianceReport.cadence == "weekly").count() == 1

    def test_recomputed_and_judged_never_merged(self, db):
        g = reports.generate(db, "t1", "eu-ai-act", cadence="daily", day=AUG, ai_system_id="sys-a")
        summary, rows = g.report.summary, g.report.rows
        judged_rows = [r for r in rows if r["basis"] == "judged"]
        assert judged_rows and all(r["human_agreement"]["chk-1"]["observed_agreement"] == 0.92 for r in judged_rows)
        assert all("human_agreement" not in r for r in rows if r["basis"] == "recomputed")
        recomputed_total = sum(summary["recomputed"].values())
        judged_total = sum(summary["judged"].values())
        assert recomputed_total == sum(1 for r in rows if r["basis"] == "recomputed")
        assert judged_total == len(judged_rows)
        assert "met" not in summary, "no cross-basis total exists"

    def test_same_month_consistent_under_three_frameworks(self, db):
        by_fw = {}
        for fid in ("eu-ai-act", "iso-42001", "soc2"):
            g = reports.generate(db, "t1", fid, cadence="monthly", day=AUG, ai_system_id="sys-b")
            db.commit()
            assert g.report.status == "ready"
            by_fw[fid] = {
                (r["ai_system_id"], r["resolver"], tuple(sorted((k, repr(v)) for k, v in r["params"].items()))):
                    (r["status"], r["n"], r["m"])
                for r in g.report.rows if r.get("resolver")
            }
        shared = set(by_fw["eu-ai-act"]) & set(by_fw["iso-42001"]) & set(by_fw["soc2"])
        pairwise = (set(by_fw["iso-42001"]) & set(by_fw["soc2"])) | shared
        assert pairwise, "the three catalogs share statements"
        for key in pairwise:
            values = {by_fw[f][key] for f in by_fw if key in by_fw[f]}
            assert len(values) == 1, f"{key} disagrees across frameworks: {values}"

    @pytest.mark.parametrize("mode,day", [(fakes.MODE_DISABLED, date(2026, 5, 3)),
                                          (fakes.MODE_UNREACHABLE, date(2026, 4, 3))])
    def test_unsealed_report_is_saved_and_says_so(self, db, core, mode, day):
        core.mode = mode
        g = reports.generate(db, "t1", "gdpr", cadence="monthly", day=day, ai_system_id="sys-a")
        db.commit()
        assert g.report.status == "ready" and g.report.record_id is None
        assert g.seal.status()["sealed"] is False and g.seal.error == mode
        assert g.bundle_error == "report_not_sealed" and g.report.bundle_id is None
        assert g.report.summary["bundle_error"] == "report_not_sealed"

    def test_bundle_failure_keeps_the_sealed_report(self, db, core, monkeypatch):
        monkeypatch.setattr("app.services.evidence.core_client.build_bundle",
                            lambda *a, **k: {"error": "core_error", "detail": "boom"})
        g = reports.generate(db, "t1", "hipaa", cadence="monthly", day=AUG)
        db.commit()
        assert g.report.record_id and g.report.bundle_id is None and g.bundle_error == "core_error"

    def test_unknown_framework_and_system(self, db):
        with pytest.raises(reports.ReportError):
            reports.generate(db, "t1", "nope", cadence="daily", day=AUG)
        with pytest.raises(reports.ReportError):
            reports.generate(db, "t1", "gdpr", cadence="daily", day=AUG, ai_system_id="sys-x")
        with pytest.raises(reports.ReportError):
            reports.generate(db, "t1", "gdpr", cadence="hourly", day=AUG)


class TestReportsHttp:
    def test_generate_list_get_and_bundle(self, harness, core):
        r = harness.client.post(BASE, json={"framework_id": "soc2", "cadence": "monthly",
                                            "ai_system_id": "sys-a", "period_start": "2026-07-10"},
                                headers=harness.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] and body["seal"]["sealed"] and body["report"]["rows"]
        rep = body["report"]
        assert rep["countersignature_status"] == "awaiting_countersignature"
        assert rep["permalink"] == f"{BASE}/{rep['id']}/bundle"

        again = harness.client.post(BASE, json={"framework_id": "soc2", "cadence": "monthly",
                                                "ai_system_id": "sys-a", "period_start": "2026-07-20"},
                                    headers=harness.headers()).json()
        assert again["created"] is False and again["regenerated"] is False and again["report"]["id"] == rep["id"]

        items = harness.client.get(BASE, params={"framework": "soc2", "cadence": "monthly"},
                                   headers=harness.headers()).json()["items"]
        assert rep["id"] in {i["id"] for i in items} and all("rows" not in i for i in items)
        full = harness.client.get(f"{BASE}/{rep['id']}", headers=harness.headers()).json()
        assert full["rows"] == rep["rows"]

        # The core persists the bundle; the CP proxies it.
        s = harness.Factory()
        s.add(EvidenceBundle(
            id=rep["bundle_id"], tenant_id="t1", log_id="log", selector={"record_ids": [rep["record_id"]]},
            payloads_mode="none", selection="producer-selected", framework_id="soc2", record_count=1,
            bundle_digest="ef" * 32, bundle={"manifest": {}, "countersignatures": [{"by": "auditor"}]},
            size_bytes=10,
        ))
        s.commit()
        s.close()
        b = harness.client.get(f"{BASE}/{rep['id']}/bundle", headers=harness.headers())
        assert b.status_code == 200 and b.headers["x-brutor-bundle-digest"] == "ef" * 32
        assert b.json()["countersignatures"] == [{"by": "auditor"}]
        full = harness.client.get(f"{BASE}/{rep['id']}", headers=harness.headers()).json()
        assert full["countersignature_status"] == "countersigned"

        assert harness.client.get(f"{BASE}/{rep['id']}", headers=harness.headers("t2")).status_code == 404
        assert harness.client.get(f"{BASE}/{rep['id']}/bundle", headers=harness.headers("t2")).status_code == 404

    def test_daily_report_has_no_bundle(self, harness):
        rep = harness.client.post(BASE, json={"framework_id": "eu-ai-act", "cadence": "daily"},
                                  headers=harness.headers()).json()["report"]
        assert rep["ai_system_id"] is None and {s["id"] for s in rep["summary"]["systems"]} == {"sys-a", "sys-b"}
        r = harness.client.get(f"{BASE}/{rep['id']}/bundle", headers=harness.headers())
        assert r.status_code == 404

    def test_validation(self, harness):
        assert harness.client.post(BASE, json={"framework_id": "eu-ai-act", "cadence": "hourly"},
                                   headers=harness.headers()).status_code == 422
        assert harness.client.post(BASE, json={"framework_id": "eu-ai-act", "ai_system_id": "sys-x"},
                                   headers=harness.headers()).status_code == 404

    def test_oversight_summary(self, harness):
        r = harness.client.get("/v1/admin/compliance/oversight", params={"ai_system_id": "sys-a"},
                               headers=harness.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        (sysrow,) = body["systems"]
        assert sysrow["oversight_assignment"] == "Model risk desk" and sysrow["assignment_basis"] == "declared"
        assert sysrow["movement"]["status"] == "indicator"
        assert sysrow["approvals"]["basis"] == "recomputed"
        assert "never thresholds" in body["movement_note"]
        assert harness.client.get("/v1/admin/compliance/oversight", params={"ai_system_id": "sys-x"},
                                  headers=harness.headers()).status_code == 404


class TestAssuranceReportSection:
    def test_cites_latest_report_per_framework(self, db):
        from app.services.ai_system_assurance import _obligation_evidence_or_unavailable

        reports.generate(db, "t1", "eu-ai-act", cadence="monthly", day=date(2026, 6, 5), ai_system_id="sys-a")
        reports.generate(db, "t1", "eu-ai-act", cadence="daily", day=date(2026, 6, 20), ai_system_id="sys-a")
        db.commit()
        section = _obligation_evidence_or_unavailable(db, tenant_id="t1", ai_system_id="sys-a")
        assert section["available"] is True
        eu = next(r for r in section["reports"] if r["framework_id"] == "eu-ai-act")
        assert eu["record_id"] and "recomputed" in eu and "judged" in eu
        assert "never summed" in section["note"]
        empty = _obligation_evidence_or_unavailable(db, tenant_id="t2", ai_system_id="sys-x")
        assert empty["reports"] == []


class TestEvidenceFindings:
    def _log(self, db, tenant, grade, age):
        log_id = hashlib.sha256(tenant.encode()).hexdigest()
        if db.get(EvidenceLog, log_id) is None:
            db.add(EvidenceLog(log_id=log_id, tenant_id=tenant, next_seq=1, tree_size=1))
        db.add(EvidenceTreeHead(
            id=f"head-{tenant}-{grade}", log_id=log_id, tenant_id=tenant, tree_size=5, root="00" * 32,
            prev_size=0, statement=b"h", issued_at=datetime.now(timezone.utc) - age, grade=grade,
        ))
        db.flush()

    def test_unwitnessed_head_for_high_risk_tenant(self, db):
        self._log(db, "t1", "standalone", timedelta(hours=2))
        self._log(db, "t2", "standalone", timedelta(hours=3))  # t2 has no high-risk system
        assert evidence_findings.scan_unwitnessed(db) == 1
        assert evidence_findings.scan_unwitnessed(db) == 1  # bumps the same item
        items = db.query(AssuranceInboxItem).filter(AssuranceInboxItem.category == "evidence_unwitnessed").all()
        assert len(items) == 1 and items[0].tenant_id == "t1" and items[0].occurrence_count == 2
        assert items[0].evidence["high_risk_system_ids"] == ["sys-a"]

    def test_witnessed_or_fresh_heads_raise_nothing(self, db):
        self._log(db, "t1", "witnessed", timedelta(hours=2))
        assert evidence_findings.scan_unwitnessed(db) == 0
        db.rollback()
        self._log(db, "t1", "standalone", timedelta(minutes=10))
        assert evidence_findings.scan_unwitnessed(db) == 0

    def test_capture_gap_counts_unsealed_governed_rows(self, db, monkeypatch):
        def sealed(ctx, spec):
            return Outcome(NOT_MET, 7, 9, "7 of 9") if ctx.system_id == "sys-a" else Outcome(MET, 4, 4, "4 of 4")

        monkeypatch.setitem(resolvers.RESOLVERS, "records.every_verdict_sealed", sealed)
        self._log(db, "t1", "witnessed", timedelta(hours=2))
        assert evidence_findings.scan_capture_gaps(db) == 1
        (item,) = db.query(AssuranceInboxItem).filter(AssuranceInboxItem.category == "capture_gap").all()
        assert item.ai_system_id == "sys-a" and item.evidence["unsealed"] == 2
        assert item.fingerprint == "capture_gap:sys-a"

    def test_capture_gap_skips_tenants_without_evidence(self, db, monkeypatch):
        monkeypatch.setitem(resolvers.RESOLVERS, "records.every_verdict_sealed",
                            lambda ctx, spec: Outcome(NOT_MET, 0, 5, "0 of 5"))
        assert evidence_findings.scan_capture_gaps(db) == 0  # no tenant log ⇒ evidence disabled


class TestScheduler:
    def test_run_scheduled_is_idempotent(self, db):
        stats = reports.run_scheduled(db, today=date(2026, 9, 19))
        assert stats["failed"] == 0 and stats["generated"] > 0
        again = reports.run_scheduled(db, today=date(2026, 9, 19))
        assert again["generated"] == 0 and again["existing"] == stats["generated"] + stats["existing"]
        targets = reports.scheduled_targets(db)
        assert ("t1", "soc2", "sys-a") in targets and ("t1", "eu-ai-act", None) in targets
        assert not any(t[0] == "t2" for t in targets)

    def test_run_once_runs_every_job(self, harness, monkeypatch):
        monkeypatch.setattr(compliance_scheduler, "SessionLocal", harness.Factory)
        compliance_scheduler._last_run.clear()
        stats = compliance_scheduler.run_once(now=datetime(2026, 9, 19, 12, tzinfo=timezone.utc))
        assert "incident_deadlines" in stats and "evidence_unwitnessed" in stats and "capture_gap" in stats
        assert any(k.startswith("reports.") for k in stats) and any(k.startswith("judged.") for k in stats)


class TestRepointedExports:
    """The legacy per-framework exports gain a `statements`
    block from the registry (their own fields are unchanged)."""

    def test_statements_block(self, db):
        from app.services.compliance.framework_statements import statements

        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, tzinfo=timezone.utc)
        block = statements(db, "t1", "gdpr", start, end, obligation_keys=["art30-records-of-processing"])
        assert block["available"] is True and block["provision_note"] == "Not legal advice."
        assert {r["obligation_key"] for r in block["rows"]} == {"art30-records-of-processing"}
        assert block["summary"]["recomputed"]
        assert statements(db, "t1", "nope", start, end)["available"] is False

    def test_iso_activity_is_repointed_at_annex_a(self, db):
        from app.services import iso42001_service

        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, tzinfo=timezone.utc)
        block = iso42001_service.annex_a_statements(db, "t1", start, end)
        assert block["framework_id"] == "iso-42001" and block["available"] is True
        assert {r["obligation_key"] for r in block["rows"]} >= {"a6-2-8-event-logs", "a6-2-6-operation-monitoring"}
        assert iso42001_service.annex_a_statements(db, "t1", start, end, include=False)["available"] is False

    def test_engine_failure_never_breaks_the_export(self, db, monkeypatch):
        from app.services.compliance import framework_statements

        def boom(*a, **k):
            raise RuntimeError("engine down")

        monkeypatch.setattr(reports, "evaluate_rows", boom)
        block = framework_statements.statements(
            db, "t1", "soc2", datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        assert block == {"available": False, "framework_id": "soc2", "reason": "RuntimeError"}

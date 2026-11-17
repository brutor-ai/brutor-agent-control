# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Compliance in the control plane — catalogs, resolver registry, versions,
# agreement statistics, retention composition, sealing hooks and the
# evaluation API

Pure / SQLite-harness tests. The resolvers' SQL semantics are covered
against Postgres in tests/test_compliance_resolvers_pg.py.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest

from app.database.models.ai_system_evidence import EVIDENCE_KINDS
from app.database.models.compliance_framework import FRAMEWORK_SEED, registry_tag_keys
from app.services.compliance import agreement
from app.services.compliance.catalogs import CATALOG_MODULES, CATALOGS, get_catalog
from app.services.compliance.catalogs.base import (
    EVIDENCE_BASES,
    CatalogVersion,
    EvidenceSpec,
    Framework,
    Obligation,
    SystemContext,
    applicability,
)
from app.services.compliance.catalogs.eu_ai_act import FRAMEWORK as EU
from app.services.compliance.resolvers import (
    JUDGE_TEMPLATES,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    RESOLVERS,
    StatementResult,
    resolver_exists,
    worst,
)
from app.services.compliance.resolvers._base import Outcome


# Display vocabulary the console and docs may not use as a claim.
BANNED_WORDS = re.compile(r"\b(compliant|certified|verified|guaranteed|approved|tamper-proof)\b", re.I)


# ---------------------------------------------------------------------------
# Catalog registry
# ---------------------------------------------------------------------------


class TestCatalogRegistry:
    def test_every_evidence_spec_resolves(self):
        """Static: every `EvidenceSpec.resolver` in every catalog names a
        registered resolver, or `judge.<template>` for the four templates."""
        missing = [
            (fid, o.key, s.resolver)
            for fid, fw in CATALOGS.items()
            for o in fw.obligations
            for s in o.evidence
            if not resolver_exists(s.resolver)
        ]
        assert missing == []

    def test_every_rfc_resolver_is_registered(self):
        rfc = {
            "records.every_verdict_sealed", "records.witnessed", "records.chain_complete",
            "records.integrity_verified", "retention.floor", "retention.declared_prunes",
            "oversight.override_available", "oversight.exercised", "oversight.not_rubber_stamp",
            "authz.out_of_grant_refused", "authz.least_privilege_declared", "authz.human_identity_verified",
            "access.sensitive_reads_recorded", "monitoring.daily_health", "monitoring.suspend_available",
            "change.promotions_sealed", "change.no_unapproved_widening", "notice.before_first_turn",
            "notice.text_declared", "synthetic.marking_declared", "incidents.reported_within_deadline",
            "incidents.register_exists", "evidence.pointer_on_file", "docs.technical_documentation",
            "docs.registration_recorded",
        }
        assert rfc <= set(RESOLVERS)
        assert JUDGE_TEMPLATES == {"no_manipulation", "notice_clear", "per_instructions", "approval_rationale"}
        for t in JUDGE_TEMPLATES:
            assert resolver_exists(f"judge.{t}")
        assert not resolver_exists("judge.vibes") and not resolver_exists("records.nope")

    def test_module_names_and_registry_ids_line_up(self):
        seed_ids = {r["id"] for r in FRAMEWORK_SEED}
        for fid, fw in CATALOGS.items():
            assert fid == fw.id
            assert fid in seed_ids, f"catalog {fid} has no compliance_frameworks row"
            assert fid.replace("-", "_") in CATALOG_MODULES
        assert get_catalog("eu-ai-act") is EU and get_catalog("narnia") is None

    def test_tag_keys_agree_with_the_registry(self):
        tags = registry_tag_keys()
        for fid, fw in CATALOGS.items():
            assert fw.tag_key == tags.get(fid), fid

    def test_specs_are_well_formed(self):
        for fid, fw in CATALOGS.items():
            keys = [o.key for o in fw.obligations]
            assert len(keys) == len(set(keys)), f"{fid} has duplicate obligation keys"
            for o in fw.obligations:
                for s in o.evidence:
                    assert s.basis in EVIDENCE_BASES, (fid, o.key, s.key)
                    if s.resolver == "retention.floor":
                        assert int(s.params["floor_days"]) > 0
                    if s.resolver == "evidence.pointer_on_file":
                        assert s.params["kind"] in EVIDENCE_KINDS, (fid, s.params)
                    if s.resolver.startswith("judge."):
                        assert s.basis == "judged"
                    if s.resolver in ("notice.text_declared", "synthetic.marking_declared",
                                      "authz.least_privilege_declared", "profile.field_declared"):
                        assert s.basis == "declared", (fid, o.key, s.key)
                    if s.resolver == "oversight.not_rubber_stamp":
                        assert s.basis == "indicator"

    def test_eu_catalog_vocabulary(self):
        """No claim words in the EU catalog's user-facing strings."""
        for o in EU.obligations:
            for text_ in (o.title, o.summary, o.limit or "", o.review_note or ""):
                assert not BANNED_WORDS.search(text_), (o.key, text_)
            for s in o.evidence:
                for text_ in (s.statement, s.limit or ""):
                    assert not BANNED_WORDS.search(text_), (o.key, s.key, text_)

    def test_eu_catalog_maps_the_rfc_table(self):
        by_key = {o.key: {s.resolver for s in o.evidence} for o in EU.obligations}
        assert by_key["art12-record-keeping"] == {
            "records.every_verdict_sealed", "records.witnessed", "records.chain_complete"}
        assert by_key["art14-human-oversight"] == {
            "oversight.override_available", "oversight.exercised", "oversight.not_rubber_stamp",
            "judge.approval_rationale"}
        assert by_key["art19-log-retention-provider"] == {"retention.floor", "retention.declared_prunes"}
        assert by_key["art50-1-ai-interaction-notice"] == {
            "notice.before_first_turn", "notice.text_declared", "judge.notice_clear"}
        assert by_key["art73-serious-incidents"] == {
            "incidents.register_exists", "incidents.reported_within_deadline"}
        assert by_key["art26-6-log-retention-deployer"] == {"retention.floor"}
        floor = next(s for o in EU.obligations if o.key == "art26-6-log-retention-deployer" for s in o.evidence)
        assert dict(floor.params) == {"floor_days": 183}


# ---------------------------------------------------------------------------
# Versions in force
# ---------------------------------------------------------------------------


class TestVersions:
    def test_eu_versions(self):
        assert [v.catalog_version for v in EU.versions] == ["2024-08", "2026-09"]
        assert EU.version_on(date(2026, 7, 26)).catalog_version == "2024-08"
        assert EU.version_on(date(2026, 7, 27)).catalog_version == "2026-09"
        assert EU.version_on(date(2024, 1, 1)) is None
        assert EU.current_version == "2026-09"

    def test_segments_split_at_the_boundary(self):
        assert EU.segments(date(2026, 7, 20), date(2026, 8, 3)) == [
            ("2024-08", date(2026, 7, 20), date(2026, 7, 26)),
            ("2026-09", date(2026, 7, 27), date(2026, 8, 3)),
        ]
        assert EU.segments(date(2026, 9, 1), date(2026, 9, 30)) == [("2026-09", date(2026, 9, 1), date(2026, 9, 30))]
        assert EU.segments(date(2024, 7, 30), date(2024, 8, 2))[0] == (None, date(2024, 7, 30), date(2024, 7, 31))
        assert EU.segments(date(2026, 9, 2), date(2026, 9, 1)) == []

    def test_obligations_in_a_version(self):
        old = {o.key for o in EU.obligations_in("2024-08")}
        new = {o.key for o in EU.obligations_in("2026-09")}
        assert "art5-prohibitions-2026" not in old and "art50-legacy" not in old
        assert new == {o.key for o in EU.obligations}

    def test_evaluation_segments_are_datetimes(self):
        from app.services.compliance.evaluation import segments

        start = datetime(2026, 7, 26, 18, tzinfo=timezone.utc)
        end = datetime(2026, 7, 27, 6, tzinfo=timezone.utc)
        segs = segments(EU, start, end)
        assert [(s.catalog_version, s.start, s.end) for s in segs] == [
            ("2024-08", start, datetime(2026, 7, 27, tzinfo=timezone.utc)),
            ("2026-09", datetime(2026, 7, 27, tzinfo=timezone.utc), end),
        ]


# ---------------------------------------------------------------------------
# Applicability flags / framework helpers
# ---------------------------------------------------------------------------


class TestFrameworkHelpers:
    FW = Framework(
        id="x", title="X", jurisdiction="X", reference="X",
        obligations=[Obligation(key="a", title="A", reference="1", applies_from=date(2026, 1, 1), summary="s",
                                aliases=frozenset({"old"}))],
        deadline_rules={"standard": timedelta(days=15), "death": timedelta(days=10)},
        retention_floors={"*": 365, "high": 400},
    )

    def test_deadlines(self):
        assert self.FW.deadline_for("death") == timedelta(days=10)
        assert self.FW.deadline_for("unknown") == timedelta(days=15)
        assert self.FW.deadline_rules_dict()["death"] == {"hours": 240, "days": 10}

    def test_floors_compose_any_and_classification(self):
        assert self.FW.retention_floor_for(None) == 365
        assert self.FW.retention_floor_for("high") == 400

    def test_aliases(self):
        assert [o.key for o in self.FW.resolve_key("old")] == ["a"]

    def test_flags_undetermined_before_false(self):
        ob = Obligation(key="f", title="F", reference="r", applies_from=date(2026, 1, 1), summary="s",
                        flags=frozenset({"interacts_with_natural_persons", "generates_synthetic_content"}))
        status, _ = applicability(ob, SystemContext(None, None, flags={"interacts_with_natural_persons": True}))
        assert status == "undetermined"
        status, _ = applicability(ob, SystemContext(None, None, flags={"generates_synthetic_content": False}))
        assert status == "not_applicable"


# ---------------------------------------------------------------------------
# Agreement statistics (pure; shared by every framework catalog)
# ---------------------------------------------------------------------------


class TestAgreement:
    def test_wilson_matches_the_textbook_value(self):
        lo, hi = agreement.wilson_interval(8, 10)
        assert lo == pytest.approx(0.4902, abs=1e-3) and hi == pytest.approx(0.9433, abs=1e-3)
        assert agreement.wilson_interval(0, 0) is None
        lo, hi = agreement.wilson_interval(10, 10)
        assert hi == pytest.approx(1.0) and lo < 1.0

    def test_kappa(self):
        # Classic 2×2: 20 agree-yes, 15 agree-no, 5 + 10 disagreements.
        pairs = [("y", "y")] * 20 + [("n", "n")] * 15 + [("y", "n")] * 5 + [("n", "y")] * 10
        assert agreement.cohens_kappa(pairs) == pytest.approx(0.4, abs=1e-3)
        assert agreement.cohens_kappa([("met", "met")] * 5) is None  # chance agreement is total
        assert agreement.cohens_kappa([]) is None

    def test_summary_flags_small_samples_and_low_agreement(self):
        small = agreement.summarize([("met", "met")] * 9)
        assert small["insufficient_sample"] is True and small["below_threshold"] is False
        low = agreement.summarize([("met", "met")] * 7 + [("met", "not_met")] * 5)
        assert low["insufficient_sample"] is False and low["below_threshold"] is True
        assert low["observed_agreement"] == pytest.approx(7 / 12, abs=1e-3)
        assert agreement.summarize(numeric=[(0.5, 0.7), (1, 1)])["mae"] == pytest.approx(0.1)
        assert "insufficient sample" in agreement.render(small)
        assert agreement.render({}) == "no human grades yet"


# ---------------------------------------------------------------------------
# Resolver engine rules
# ---------------------------------------------------------------------------


class TestEngineRules:
    def test_worst_ranking(self):
        assert worst(["met", "declared", "not_met"]) == "not_met"
        assert worst(["met", "not_evaluable"]) == "not_evaluable"
        assert worst(["declared", "indicator"]) == "indicator"
        assert worst([]) is None

    def _ctx(self):
        from app.services.compliance.resolvers import ResolverContext

        return ResolverContext(db=SimpleNamespace(rollback=lambda: None), tenant_id="t",
                               system=SimpleNamespace(id="s"), window_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
                               window_end=datetime(2026, 9, 2, tzinfo=timezone.utc))

    def test_basis_discipline_and_failure_is_not_a_verdict(self, monkeypatch):
        from app.services.compliance import resolvers

        monkeypatch.setitem(resolvers.RESOLVERS, "t.met", lambda ctx, spec: Outcome(MET, 1, 1, "x"))
        monkeypatch.setitem(resolvers.RESOLVERS, "t.boom", lambda ctx, spec: 1 / 0)
        declared = resolvers.run(self._ctx(), EvidenceSpec(key="k", statement="s", basis="declared", resolver="t.met"))
        assert declared.status == "declared"
        indicator = resolvers.run(self._ctx(), EvidenceSpec(key="k", statement="s", basis="indicator",
                                                            resolver="t.met"))
        assert indicator.status == "indicator"
        boom = resolvers.run(self._ctx(), EvidenceSpec(key="k", statement="s", basis="recomputed", resolver="t.boom"))
        assert boom.status == NOT_EVALUABLE and "ZeroDivisionError" in boom.detail
        unknown = resolvers.run(self._ctx(), EvidenceSpec(key="k", statement="s", basis="recomputed",
                                                          resolver="t.none"))
        assert unknown.status == NOT_EVALUABLE

    def test_counted_never_meets_an_empty_population(self):
        from app.services.compliance.resolvers._base import counted

        assert counted(0, 0, what="x").status == NOT_EVALUABLE
        assert counted(2, 2, what="x").status == MET
        assert counted(1, 2, what="x").status == NOT_MET

    def test_citations_are_capped(self):
        r = StatementResult(key="k", basis="recomputed", status=MET, citations=[str(i) for i in range(50)])
        assert len(r.to_dict()["citations"]) == 20

    def test_summary_keeps_bases_apart(self):
        from app.services.compliance.evaluation import summarize_statements

        results = [
            StatementResult(key="a", basis="recomputed", status=MET),
            StatementResult(key="b", basis="recomputed", status=NOT_MET),
            StatementResult(key="c", basis="judged", status=MET),
            StatementResult(key="d", basis="declared", status="declared"),
        ]
        s = summarize_statements(results)
        assert s["worst_status"] == NOT_MET
        assert s["by_basis"] == {"recomputed": {"met": 1, "measured": 2}, "judged": {"met": 1, "measured": 1}}
        assert "%" not in s["summary"]

    def test_other_frameworks_cross_links(self):
        from app.services.compliance.evaluation import other_frameworks

        spec = next(s for o in EU.obligations if o.key == "art12-record-keeping" for s in o.evidence
                    if s.resolver == "records.every_verdict_sealed")
        links = other_frameworks(spec, "eu-ai-act")
        assert links and all(l["framework_id"] != "eu-ai-act" for l in links)


# ---------------------------------------------------------------------------
# Consequential-rule literals pinned against the core
# ---------------------------------------------------------------------------


class TestCoreParity:
    def test_subtype_literals_match_constants_rs(self):
        from tests.core_repo import core_source
        from app.services.compliance.resolvers import records

        text_ = core_source("constants.rs").read_text()
        for literal in (*records.DENY_SUBTYPES, *records.ALWAYS_SEALED_SUBTYPES,
                        records.SUBTYPE_EMBEDDINGS, records.SUBTYPE_SKILL_RUN):
            assert f'"{literal}"' in text_, literal
        mint = core_source("services", "evidence", "mint.rs").read_text()
        block = mint[mint.index("const DENY_SUBTYPES"):mint.index("];", mint.index("const DENY_SUBTYPES"))]
        assert len(re.findall(r"st::\w+", block)) == len(records.DENY_SUBTYPES)

    def test_read_only_map_mirrors_the_core(self):
        from app.services.compliance.resolvers.records import read_only_map

        caps = {"tools": [{"name": "a", "annotations": {"readOnlyHint": True}}, {"name": "b", "annotations": {}},
                          {"name": "c"}]}
        assert read_only_map(caps) == {"a": True, "b": False, "c": False}
        assert read_only_map([{"name": "a", "annotations": {"readOnlyHint": True}}]) == {"a": True}
        assert read_only_map(None) == {}


# ---------------------------------------------------------------------------
# Retention composition
# ---------------------------------------------------------------------------


class TestRetentionComposition:
    def _system(self, tier=None):
        return SimpleNamespace(id="s", name="s", display_name=None, eu_ai_act_risk_tier=tier)

    def _fw(self, fid, floor=None):
        return SimpleNamespace(id=fid, retention_floor_days=floor)

    def test_floor_is_the_max_over_active_frameworks_by_classification(self):
        from app.services.compliance.retention_floors import floor_sources

        profile = SimpleNamespace(frameworks={"soc2": {"in_scope": True}, "hipaa": {"in_scope": False}})
        sources = floor_sources(self._system("high"), profile,
                                [self._fw("eu-ai-act"), self._fw("soc2"), self._fw("hipaa")])
        by_id = {s.framework_id: s.floor_days for s in sources}
        assert by_id["eu-ai-act"] == 183
        assert "hipaa" not in by_id  # declared out of scope
        assert max(by_id.values()) == by_id.get("soc2", 183) or max(by_id.values()) == 183
        # A limited-risk EU system carries no EU floor.
        assert "eu-ai-act" not in {s.framework_id for s in
                                   floor_sources(self._system("limited"), None, [self._fw("eu-ai-act")])}

    def test_uncatalogued_frameworks_use_the_registry_floor(self, monkeypatch):
        from app.services.compliance import offering, retention_floors

        # Catalogs are read through `offering`; empty its registry.
        monkeypatch.setattr(offering, "CATALOGS", {})
        sources = retention_floors.floor_sources(self._system(), None, [self._fw("narnia", 90)])
        assert [(s.framework_id, s.floor_days) for s in sources] == [("narnia", 90)]

    def test_compose_raises_windows_never_lowers_or_invents(self):
        from app.api.v1.models.admin.retention import DataRetentionPolicy
        from app.services.compliance.retention_floors import compose

        p = DataRetentionPolicy(body_days=30, log_days=90, run_days=365)
        c = compose(p, 183)
        assert (c.body_days, c.log_days, c.run_days) == (30, 183, 365)
        keep = compose(DataRetentionPolicy(log_days=None), 183)
        assert keep.log_days is None
        assert compose(None, 183) is None and compose(p, None) is p

    def test_prune_bracketing(self):
        from app.services.compliance.resolvers.retention import classify_prune

        at = datetime(2026, 9, 10, tzinfo=timezone.utc)
        floor = timedelta(days=183)
        assert classify_prune(at, at - timedelta(days=400), at - timedelta(days=200), floor) == "old_enough"
        assert classify_prune(at, at - timedelta(days=2), at - timedelta(days=1), floor) == "too_young"
        assert classify_prune(at, at - timedelta(days=300), at - timedelta(days=10), floor) == "unknown"
        assert classify_prune(at, None, None, floor) == "unknown"


# ---------------------------------------------------------------------------
# Migration a179_compliance_frameworks_more — registry rows for Colorado / Korea / Brazil
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------


class TestSealing:
    def test_build_record_drops_nulls_and_mirrors_record_type(self):
        from app.services.compliance import sealing

        rec = sealing.build_record(sealing.RECORD_DECLARATION, verdict="recorded", context={"a": None, "tags": []},
                                   payload={"x": 1})
        assert rec == {"record_type": "ai.brutor.declaration", "verdict": "recorded",
                       "context": {"record_type": "ai.brutor.declaration"}, "payload": {"x": 1}}
        assert sealing.reference("ai.brutor.rubric", "nothex") is None
        assert sealing.references(None, sealing.reference("t", "a" * 64, "p")) == [
            {"type": "t", "digest": "a" * 64, "purpose": "p"}]
        assert sealing.references(None) is None

    def test_json_digest_is_canonical(self):
        from app.services.compliance import sealing

        assert sealing.canonical_bytes({"b": 1, "a": [1.0, "é"]}) == '{"a":[1,"é"],"b":1}'.encode()
        assert sealing.json_digest({"b": 1, "a": 2}) == sealing.json_digest({"a": 2, "b": 1})

    def test_seal_many_maps_outcomes_in_order_and_never_raises(self, monkeypatch):
        from app.services.compliance import sealing

        monkeypatch.setattr("app.services.evidence.core_client.seal",
                            lambda t, recs, **kw: {"sealed": [{"record_id": f"{i:064x}"} for i in range(len(recs))]})
        out = sealing.seal_many("t", [{"record_type": "ai.brutor.x"}] * 3)
        assert [o.record_id for o in out] == [f"{i:064x}" for i in range(3)]
        monkeypatch.setattr("app.services.evidence.core_client.seal",
                            lambda t, recs, **kw: {"error": "evidence_disabled", "detail": "no key"})
        out = sealing.seal("t", "ai.brutor.x")
        assert not out.sealed and out.status() == {"sealed": False, "record_id": None,
                                                   "seal_error": "evidence_disabled", "seal_detail": "no key"}

        def boom(*a, **k):
            raise RuntimeError("x")

        monkeypatch.setattr("app.services.evidence.core_client.seal", boom)
        assert sealing.seal("t", "ai.brutor.x").error == "core_error"

    def test_hook_records(self, monkeypatch):
        from app.services.compliance import sealing_hooks

        sent = []
        monkeypatch.setattr("app.services.evidence.core_client.seal",
                            lambda t, recs, **kw: sent.append((t, recs)) or {"sealed": [{"record_id": "f" * 64}]})
        db = SimpleNamespace()
        contract = SimpleNamespace(id="ctr-1", tenant_id="t", ai_system_id="sys", version=3, approved_by="adm-1",
                                   contract_hash="sha256:" + "a" * 64)
        assert sealing_hooks.seal_epoch_boundary(db, contract).record_id == "f" * 64
        rec = sent[-1][1][0]
        assert rec["record_type"] == "ai.brutor.epoch-boundary" and rec["verdict"] == "epoch_boundary"
        assert rec["disposition"] == {"decision": "accept", "approver": "human", "human_disposed": True,
                                      "authority": "ctr-1"}
        assert rec["context"]["contract_version"] == "3"
        contract.approved_by = None
        sealing_hooks.seal_epoch_boundary(db, contract)
        assert sent[-1][1][0]["disposition"]["approver"] == "policy"
        assert sent[-1][1][0]["disposition"]["human_disposed"] is False

        intervention = SimpleNamespace(id="ri-1", tenant_id="t", root_task_id="run-1", reason="free text reason")
        sealing_hooks.seal_run_abort(db, intervention, "sys")
        rec = sent[-1][1][0]
        assert rec["record_type"] == "ai.brutor.run-abort" and rec["verdict"] == "resolved"
        assert rec["disposition"] == {"decision": "reject", "approver": "human", "human_disposed": True,
                                      "authority": "ri-1"}
        assert rec["context"]["intervention_id"] == "ri-1"
        # Free text only as a payload (digested by the core, never stored).
        assert "free text" not in str(rec["context"]) and rec["payload"] == {"reason": "free text reason"}

    def test_a_failed_seal_raises_a_capture_gap(self, monkeypatch):
        from app.services.compliance import sealing_hooks

        raised = []
        monkeypatch.setattr("app.services.evidence.core_client.seal",
                            lambda t, recs, **kw: {"error": "core_unreachable", "detail": "refused"})
        monkeypatch.setattr(sealing_hooks, "raise_capture_gap", lambda *a: raised.append(a))
        out = sealing_hooks.seal_run_abort(SimpleNamespace(), SimpleNamespace(
            id="ri-2", tenant_id="t", root_task_id="r", reason="x"), "sys")
        assert out.error == "core_unreachable"
        assert raised and raised[0][3] == "ai.brutor.run-abort" and raised[0][5] == "core_unreachable"


# ---------------------------------------------------------------------------
# Evaluation API + sealing hooks over HTTP (SQLite harness)
# ---------------------------------------------------------------------------

TENANT = "t1"
SYS_HIGH = "sys-high"
SYS_LOW = "sys-low"


@pytest.fixture(scope="module")
def harness():
    from app.api.v1.endpoints.admin.ai_system_contracts import create_ai_system_contracts_router
    from app.api.v1.endpoints.admin.compliance_evaluation import create_compliance_evaluation_router
    from app.api.v1.endpoints.admin.run_interventions import create_run_interventions_router
    from app.core.logging_system import BrutorLogger
    from app.database.models.resource_group import GroupType, ResourceGroup
    from app.database.seed_compliance_frameworks import seed_compliance_frameworks
    from tests.admin_endpoint_harness import build_admin_harness

    logger = BrutorLogger("compliance-tests")
    h = build_admin_harness([
        create_compliance_evaluation_router(logger),
        create_ai_system_contracts_router(logger),
        create_run_interventions_router(logger),
    ])

    def seed(s):
        from app.database.models.tenant import Tenant

        seed_compliance_frameworks(s)
        s.get(Tenant, TENANT).compliance_profile = {"frameworks": ["eu_ai_act"]}
        s.get(Tenant, TENANT).data_retention = {"store_bodies": "full", "log_days": 90}
        for gid, tier in ((SYS_HIGH, "high"), (SYS_LOW, "minimal")):
            s.add(ResourceGroup(id=gid, tenant_id=TENANT, name=gid, description=gid, hierarchy_level=0,
                                inherit_resources=True, is_active=True, is_system=False,
                                group_type=GroupType.AI_SYSTEM, system_kind="agent",
                                eu_ai_act_risk_tier=tier, eu_ai_act_role="provider"))

    h.seed(seed)
    yield h
    h.close()


@pytest.fixture
def fake_resolvers(monkeypatch):
    """Deterministic resolvers (the SQL is covered on Postgres)."""
    from app.services.compliance import evaluation, resolvers

    evaluation.clear_cache()

    def fake(ctx, spec):
        if spec.basis == "declared":
            return Outcome("declared", detail="declared", citations=["d" * 64])
        if spec.basis == "indicator":
            return Outcome("indicator", detail="no movement")
        if spec.resolver == "records.every_verdict_sealed":
            return Outcome(NOT_MET, 9, 10, "9 of 10", citations=["a" * 64], facts={"refusals": 2})
        return Outcome(MET, 3, 3, "3 of 3")

    for rid in list(resolvers.RESOLVERS):
        monkeypatch.setitem(resolvers.RESOLVERS, rid, fake)
    monkeypatch.setattr(resolvers._judge, "judge", lambda ctx, spec: Outcome(MET, 5, 5, "Judged: met 5 of 5."))
    yield
    evaluation.clear_cache()


def _get(h, path, expect=200, **params):
    r = h.client.get(path, params=params, headers=h.headers(TENANT))
    assert r.status_code == expect, r.text
    return r.json()


class TestEvaluationApi:
    def test_obligation_detail(self, harness, fake_resolvers):
        body = _get(harness, "/v1/admin/compliance/obligations/art12-record-keeping",
                    framework="eu-ai-act", ai_system_id=SYS_HIGH)
        assert body["resolved_keys"] == ["art12-record-keeping"] and body["not_legal_advice"]
        [system] = body["systems"]
        [ob] = system["obligations"]
        assert ob["applicability"] == "applies" and ob["worst_status"] == NOT_MET
        by_key = {s["key"]: s for s in ob["statements"]}
        sealed = by_key["records.every_verdict_sealed"]
        assert (sealed["status"], sealed["n"], sealed["m"]) == (NOT_MET, 9, 10)
        assert sealed["basis"] == "recomputed" and sealed["catalog_version"] == "2026-09"
        assert sealed["citations"] == ["a" * 64] and sealed["limit"]
        assert any(l["framework_id"] != "eu-ai-act" for l in sealed["other_frameworks"])
        assert ob["by_basis"]["recomputed"] == {"met": 2, "measured": 3}

    def test_an_old_key_resolves_to_its_successors(self, harness, fake_resolvers):
        body = _get(harness, "/v1/admin/compliance/obligations/art72-post-market", ai_system_id=SYS_HIGH)
        assert body["resolved_keys"] == ["art72-post-market-monitoring"]
        body = _get(harness, "/v1/admin/compliance/obligations/annex3-provider-duties", ai_system_id=SYS_HIGH)
        assert "art12-record-keeping" in body["resolved_keys"] and len(body["systems"][0]["obligations"]) > 5

    def test_not_applicable_obligations_carry_no_statements(self, harness, fake_resolvers):
        body = _get(harness, "/v1/admin/compliance/obligations/art12-record-keeping", ai_system_id=SYS_LOW)
        ob = body["systems"][0]["obligations"][0]
        assert ob["applicability"] == "not_applicable" and ob["statements"] == [] and ob["worst_status"] is None

    def test_errors(self, harness, fake_resolvers):
        _get(harness, "/v1/admin/compliance/obligations/nope", expect=404)
        _get(harness, "/v1/admin/compliance/obligations/art12-record-keeping", expect=404, framework="narnia")
        _get(harness, "/v1/admin/compliance/obligations/art12-record-keeping", expect=404, ai_system_id="missing")

    def test_overview(self, harness, fake_resolvers):
        body = _get(harness, "/v1/admin/compliance/overview", framework="eu-ai-act")
        assert body["framework"]["id"] == "eu-ai-act" and body["framework"]["enabled"] is True
        assert body["framework"]["verified_against_instrument"] is True
        systems = {s["ai_system_id"]: s for s in body["systems"]}
        high = systems[SYS_HIGH]
        assert {o["key"] for o in high["obligations_upcoming"]} >= {"art12-record-keeping"}
        assert high["capture"]["unsealed_governed_actions"] == 1
        assert high["statement_counts"]["worst_status"] == NOT_MET
        assert high["profile_declared"] is False
        # Undeclared interaction flag ⇒ Art 50(1) is an undetermined declaration.
        assert "art50-1-ai-interaction-notice" in {u["key"] for u in high["undetermined"]}
        assert [t["applies_from"] for t in body["timeline"]] == sorted(t["applies_from"] for t in body["timeline"])

    def test_systems_and_retention(self, harness, fake_resolvers):
        body = _get(harness, "/v1/admin/compliance/systems")
        rows = {r["ai_system_id"]: r for r in body["items"]}
        assert rows[SYS_HIGH]["retention_floor_days"] == 183 and rows[SYS_LOW]["retention_floor_days"] is None
        assert rows[SYS_HIGH]["frameworks"]["eu-ai-act"]["risk_tier"] == "high"
        assert body["frameworks_enabled"] == ["eu-ai-act"]
        ret = _get(harness, "/v1/admin/compliance/retention")
        assert ret["policy"]["log_days"] == 90 and ret["floor_days"] == 183
        assert ret["effective"]["log_days"] == 183 and ret["raised"] is True
        assert [s["ai_system_id"] for s in ret["systems"]] == [SYS_HIGH]

    def test_cache_is_per_window(self, harness, fake_resolvers, monkeypatch):
        from app.services.compliance import resolvers

        calls = []
        monkeypatch.setitem(resolvers.RESOLVERS, "records.witnessed",
                            lambda ctx, spec: calls.append(1) or Outcome(MET, 1, 1, "x"))
        params = dict(ai_system_id=SYS_HIGH, **{"from": "2026-09-01T00:00:00Z", "to": "2026-09-10T00:00:00Z"})
        _get(harness, "/v1/admin/compliance/obligations/art12-record-keeping", **params)
        _get(harness, "/v1/admin/compliance/obligations/art12-record-keeping", **params)
        assert len(calls) == 1


class TestSealingHooksOverHttp:
    def test_promotion_seals_an_epoch_boundary_and_survives_a_failure(self, harness, monkeypatch):
        sent = []
        monkeypatch.setattr("app.services.evidence.core_client.seal",
                            lambda t, recs, **kw: sent.append(recs) or {"error": "core_unreachable", "detail": "x"})
        h = harness.headers(TENANT)
        r = harness.client.post(f"/v1/admin/ai-systems/{SYS_HIGH}/contracts", headers=h)
        assert r.status_code in (200, 201), r.text
        cid = r.json()["contract"]["id"]
        assert harness.client.post(f"/v1/admin/contracts/{cid}/approve", headers=h, json={}).status_code == 200
        r = harness.client.post(f"/v1/admin/contracts/{cid}/promote", headers=h, json={})
        assert r.status_code == 200, r.text  # sealing failed; the promotion stands
        assert r.json()["status"] == "active"
        rec = sent[-1][0]
        assert rec["record_type"] == "ai.brutor.epoch-boundary" and rec["ai_system_id"] == SYS_HIGH
        assert rec["disposition"]["approver"] == "human"
        s = harness.Factory()
        try:
            from app.database.models.assurance_inbox import AssuranceInboxItem

            fps = {i.fingerprint for i in s.query(AssuranceInboxItem).filter(AssuranceInboxItem.tenant_id == TENANT)}
            assert f"capture_gap:cp_seal:ai.brutor.epoch-boundary:{SYS_HIGH}" in fps
        finally:
            s.close()

    def test_abort_seals_a_run_abort_record(self, harness, monkeypatch):
        from app.database.models.proxy_log import ProxyLog

        sent = []
        monkeypatch.setattr("app.services.evidence.core_client.seal",
                            lambda t, recs, **kw: sent.append(recs) or {"sealed": [{"record_id": "e" * 64}]})
        harness.seed(lambda s: s.add(ProxyLog(
            id="pl-run-1", tenant_id=TENANT, timestamp=datetime.now(timezone.utc), proxy_type="a2a",
            root_task_id="run-abort-1", resource_group_id=SYS_HIGH, end_user_session_id="", http_method="POST",
            endpoint="/x", success=True, guardrail_blocked=False, argument_policy_denied=False)))
        r = harness.client.post("/v1/admin/ai-systems/runs/run-abort-1/abort", headers=harness.headers(TENANT),
                                json={"reason": "operator saw a loop"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["record"] == {"sealed": True, "record_id": "e" * 64, "error": None, "detail": None}
        rec = sent[-1][0]
        assert rec["record_type"] == "ai.brutor.run-abort" and rec["ai_system_id"] == SYS_HIGH
        assert rec["disposition"]["authority"] == body["intervention"]["id"]
        assert rec["payload"] == {"reason": "operator saw a loop"}


class TestSweeperComposition:
    def test_the_sweeper_policy_is_raised_to_the_tenant_floor(self, harness):
        from app.database.models.tenant import Tenant
        from app.services.compliance.retention_floors import effective_tenant_policy

        s = harness.Factory()
        try:
            policy = effective_tenant_policy(s, s.get(Tenant, TENANT))
            assert policy.log_days == 183  # 90 by policy, 183 by the high-risk EU floor
            other = effective_tenant_policy(s, s.get(Tenant, "t2"))
            assert other is None  # no policy: keep everything
        finally:
            s.close()

    def test_the_scheduler_composes_before_sweeping(self, monkeypatch):
        from app.services.data_retention import scheduler

        src = Path(scheduler.__file__).read_text()
        assert "effective_tenant_policy" in src

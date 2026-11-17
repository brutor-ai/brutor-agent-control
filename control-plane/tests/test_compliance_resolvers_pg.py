# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The shared compliance resolvers against real Postgres

Every resolver is exercised on DB fixtures for met / not_met /
not_evaluable (missing inputs) and its edges: late notice, a prune
younger than the floor, backfilled rows, an approval never closed. Plus
version-in-force replay and the shared-resolver consistency rule:
the same fixture month under two frameworks citing the same
`(resolver, params)` yields identical statements.

Postgres only, deliberately: the resolvers use JSONB operators, arrays,
DISTINCT ON and recursive CTEs, which SQLite would either reject or —
worse — parse into different semantics. Gated like the other PG suites, and
installs its own scratch database on a throwaway server:

    BRUTOR_PG_TESTS=1 BRUTOR_PG_TEST_URL=postgresql://brutor:brutor123@localhost:<port>/postgres \
        .venv/bin/python -m pytest -q tests/test_compliance_resolvers_pg.py
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.resource_group import ResourceGroup
from app.services.compliance.catalogs import CATALOGS
from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.evaluation import SystemEvaluator
from app.services.compliance.resolvers import (
    DECLARED,
    INDICATOR,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    UNDETERMINED,
    ResolverContext,
    run,
)
from tests.admin_endpoint_harness import throwaway_installed_pg_database

pytestmark = pytest.mark.skipif(
    os.environ.get("BRUTOR_PG_TESTS") != "1",
    reason="needs Postgres; set BRUTOR_PG_TESTS=1 and BRUTOR_PG_TEST_URL",
)

TENANT = "compliance-resolvers"
LOG_ID = "c" * 64
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
WS = datetime(2026, 9, 1, tzinfo=timezone.utc)
WE = datetime(2026, 9, 15, tzinfo=timezone.utc)
BANNED = ("compliant", "certified", "guaranteed", "tamper-proof", " verified", " approved", " pass ")

TENANT_TABLES = (
    "action_records", "evidence_tree_heads", "evidence_logs", "proxy_logs", "ai_system_compliance_profiles",
    "ai_system_contracts", "tool_approval_requests", "compliance_notice_configs",
    "compliance_incident_events", "compliance_incidents", "ai_system_evidence",
    "compliance_documentation_exports", "assurance_check_reviews", "assurance_check_results",
    "assurance_checks", "run_interventions", "ai_system_drift_events", "ai_system_baselines",
    "ai_system_assurance_reports", "audit_chain_prunes", "mcp_servers", "assurance_inbox_items",
    "system_operation_log", "tenant_admins",
)


def _wipe(s):
    s.rollback()
    s.execute(text("DELETE FROM assurance_inbox_events WHERE item_id IN "
                   "(SELECT id FROM assurance_inbox_items WHERE tenant_id = :t)"), {"t": TENANT})
    for table in TENANT_TABLES:
        s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": TENANT})
    s.execute(text("DELETE FROM tenant_role_permissions WHERE role_id IN "
                   "(SELECT id FROM tenant_roles WHERE tenant_id = :t)"), {"t": TENANT})
    s.execute(text("DELETE FROM tenant_roles WHERE tenant_id = :t"), {"t": TENANT})
    s.execute(text("DELETE FROM resource_groups WHERE tenant_id = :t"), {"t": TENANT})
    s.commit()


@pytest.fixture(scope="module")
def pg_url():
    yield from throwaway_installed_pg_database("compliance_resolvers")


@pytest.fixture(scope="module")
def pg(pg_url):
    engine = create_engine(pg_url)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.execute(
        text(
            "INSERT INTO tenants (id, name, display_name, primary_contact_email, is_active, is_default, "
            "is_trial, suspended, features, settings) VALUES (:t, :t, :t, 'r@test.invalid', true, false, "
            "false, false, '{}'::json, '{}'::json) ON CONFLICT (id) DO NOTHING"
        ),
        {"t": TENANT},
    )
    s.commit()
    _wipe(s)
    yield s
    _wipe(s)
    s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    s.commit()
    s.close()
    engine.dispose()


@pytest.fixture
def db(pg):
    _wipe(pg)
    pg.execute(text("UPDATE tenants SET data_retention = NULL, compliance_profile = '{}'::json, mfa_policy = 'optional' "
                    "WHERE id = :t"), {"t": TENANT})
    ins(pg, "evidence_logs", log_id=LOG_ID, tenant_id=TENANT)
    pg.commit()
    return pg


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def ins(db, table: str, **cols: Any) -> None:
    names, values, params = [], [], {}
    for k, v in cols.items():
        names.append(k)
        if isinstance(v, (dict, list)):
            values.append(f"CAST(:{k} AS json)")
            params[k] = json.dumps(v)
        else:
            values.append(f":{k}")
            params[k] = v
    db.execute(text(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(values)})"), params)


def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def hex64() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def system(db, *, risk: Optional[str] = None, role: Optional[str] = None, parent: Optional[str] = None,
           phi: bool = False, group_type: str = "AI_SYSTEM") -> str:
    sid = uid("sys")
    ins(db, "resource_groups", id=sid, tenant_id=TENANT, name=sid, description=sid, hierarchy_level=0,
        inherit_from_parent=True, is_active=True, is_system=False, group_type=group_type,
        eu_ai_act_risk_tier=risk, eu_ai_act_role=role, parent_group_id=parent, phi_resource=phi)
    db.flush()
    return sid


def row(db, sid: str, *, at: datetime, proxy_type: str = "a2a", record_id: Optional[str] = None, **kw) -> str:
    pid = uid("pl")
    ins(db, "proxy_logs", id=pid, tenant_id=TENANT, timestamp=at, proxy_type=proxy_type, resource_group_id=sid,
        end_user_session_id="", http_method="POST", endpoint=kw.pop("endpoint", "/v1/x"),
        success=kw.pop("success", True), record_id=record_id, **kw)
    return pid


_seq = {"n": 0}


def record(db, sid: Optional[str], *, verdict: str = "executed", at: datetime, proxy_log_id: Optional[str] = None,
           context: Optional[Dict[str, Any]] = None, chain: Optional[Dict[str, Any]] = None,
           seq: Optional[int] = None, extra: Optional[Dict[str, Any]] = None, kind: str = "decide") -> str:
    rid = hex64()
    if seq is None:
        _seq["n"] += 1
        seq = _seq["n"]
    body: Dict[str, Any] = {"record_id": rid, "verdict": verdict, "context": context or {}}
    if chain:
        body["chain"] = chain
    body.update(extra or {})
    # As the core mints it (evidence/mint.rs `system_of`): the `epoch` column
    # is "contract:<hash>" whenever the body names a contract.
    chash = (body.get("system") or {}).get("contract_hash")
    ins(db, "action_records", record_id=rid, tenant_id=TENANT, log_id=LOG_ID, seq=seq, ai_system_id=sid,
        proxy_log_id=proxy_log_id, verdict=verdict, kind=kind, surface="a2a", timestamp=at, record=body,
        epoch=f"contract:{chash}" if chash else None, statement=b"\x00", kid="k" * 64)
    return rid


def sealed_row(db, sid, *, at, proxy_type="a2a", verdict="executed", backfilled=False, **kw) -> str:
    pid = uid("pl")
    rid = record(db, sid, verdict=verdict, at=at, proxy_log_id=pid,
                 context={"backfilled": True} if backfilled else {})
    ins(db, "proxy_logs", id=pid, tenant_id=TENANT, timestamp=at, proxy_type=proxy_type, resource_group_id=sid,
        end_user_session_id="", http_method="POST", endpoint=kw.pop("endpoint", "/v1/x"),
        success=kw.pop("success", True), record_id=rid, **kw)
    return rid


def profile(db, sid: str, *, core=None, frameworks=None, sealed: bool = True) -> None:
    ins(db, "ai_system_compliance_profiles", ai_system_id=sid, tenant_id=TENANT, core=core or {},
        frameworks=frameworks or {}, declaration_record_id=hex64() if sealed else None)


def contract(db, sid: str, *, version: int, promoted_at: Optional[datetime], status: str = "active",
             approved_by: Optional[str] = "admin-1", widened: bool = False, chash: Optional[str] = None) -> str:
    cid = uid("ctr")
    ins(db, "ai_system_contracts", id=cid, tenant_id=TENANT, ai_system_id=sid, version=version,
        contract_hash=chash or f"sha256:{hex64()}", resolved_closure={}, status=status,
        diff_summary={"has_widened_permissions": widened, "widened": []}, approved_by=approved_by,
        promoted_at=promoted_at)
    return cid


def approval(db, sid: str, *, status: str, at: datetime, resolved_by: Optional[str] = None,
             expires: Optional[datetime] = None) -> str:
    aid = uid("apr")
    ins(db, "tool_approval_requests", id=aid, tenant_id=TENANT, capability_type="tool", capability_name="t",
        requester_type="api_key", requester_group_id=sid, status=status, timeout_seconds=3600,
        expires_at=expires or at + timedelta(hours=1), resolved_by=resolved_by,
        resolved_at=(at + timedelta(minutes=5)) if resolved_by else None, created_at=at)
    return aid


def ctx_for(db, sid: str, *, ws: datetime = WS, we: datetime = WE, now: datetime = NOW,
            framework_id: str = "eu-ai-act") -> ResolverContext:
    db.flush()
    s = db.get(ResourceGroup, sid)
    db.refresh(s)
    p = db.get(AISystemComplianceProfile, sid)
    if p is not None:
        db.refresh(p)
    return ResolverContext(db=db, tenant_id=TENANT, system=s, window_start=ws, window_end=we,
                           framework_id=framework_id, now=now, profile=p)


def spec(resolver: str, basis: str = "recomputed", **params) -> EvidenceSpec:
    return EvidenceSpec(key=resolver, statement="s", basis=basis, resolver=resolver, params=params)


def ev(db, sid, resolver, basis="recomputed", **kw):
    ctx_kw = {k: kw.pop(k) for k in ("ws", "we", "now", "framework_id") if k in kw}
    result = run(ctx_for(db, sid, **ctx_kw), spec(resolver, basis, **kw))
    lowered = f" {result.detail.lower()} "
    for word in BANNED:
        assert word not in lowered, f"{resolver} detail uses banned vocabulary: {result.detail!r}"
    if result.status == MET:
        assert result.m, f"{resolver}: met on an empty population"
    return result


D = lambda day, h=12: datetime(2026, 9, day, h, tzinfo=timezone.utc)  # noqa: E731


# ---------------------------------------------------------------------------
# records.*
# ---------------------------------------------------------------------------


class TestEveryVerdictSealed:
    def test_met_with_refusals_counted_separately(self, db):
        sid = system(db, risk="limited")
        sealed_row(db, sid, at=D(2))
        sealed_row(db, sid, at=D(3), proxy_type="llm", verdict="blocked", guardrail_blocked=True)
        sealed_row(db, sid, at=D(4), proxy_type="mcp", mcp_method="tools/call", server_id="srv-x",
                   mcp_capability_name="write")
        # Not required to seal: a plain completion (inference_records off for
        # a non-high-risk system) and an MCP listing.
        row(db, sid, at=D(5), proxy_type="llm")
        row(db, sid, at=D(5), proxy_type="mcp", mcp_method="tools/list")
        r = ev(db, sid, "records.every_verdict_sealed")
        assert (r.status, r.n, r.m) == (MET, 3, 3)
        assert r.facts["refusals"] == 1 and r.facts["refusals_sealed"] == 1
        assert len(r.citations) == 3

    def test_an_unsealed_governed_row_is_not_met(self, db):
        sid = system(db)
        sealed_row(db, sid, at=D(2))
        row(db, sid, at=D(3), proxy_type="a2a")
        row(db, sid, at=D(3), proxy_type="skill", proxy_subtype="access_denied")  # refusal, no record
        r = ev(db, sid, "records.every_verdict_sealed")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 3)
        assert r.facts["refusals"] == 1 and r.facts["refusals_sealed"] == 0
        assert "capture gap" in r.detail

    def test_backfilled_rows_are_reported_separately(self, db):
        sid = system(db)
        sealed_row(db, sid, at=D(2))
        sealed_row(db, sid, at=D(3), backfilled=True)
        r = ev(db, sid, "records.every_verdict_sealed")
        assert (r.status, r.n, r.m) == (MET, 2, 2)
        assert r.facts["sealed_late_by_backfill"] == 1
        assert "Sealed late by backfill: 1" in r.detail

    def test_no_governed_rows_is_not_evaluable(self, db):
        sid = system(db)
        row(db, sid, at=D(2), proxy_type="llm")  # a completion that need not seal
        r = ev(db, sid, "records.every_verdict_sealed")
        assert (r.status, r.n, r.m) == (NOT_EVALUABLE, 0, 0)

    def test_high_risk_turns_inference_records_on(self, db):
        sid = system(db, risk="high", role="provider")
        row(db, sid, at=D(2), proxy_type="llm")
        r = ev(db, sid, "records.every_verdict_sealed")
        assert (r.status, r.m) == (NOT_MET, 1) and r.facts["inference_records"] is True
        profile(db, sid, core={"inference_records": False})
        assert ev(db, sid, "records.every_verdict_sealed").status == NOT_EVALUABLE

    def test_read_only_tools_seal_only_through_a_sensitive_group(self, db):
        sid = system(db)
        ins(db, "mcp_servers", id="srv-ro-" + sid, tenant_id=TENANT, name="ro", base_url="http://x",
            mcp_endpoint_path="/mcp",
            capabilities={"tools": [{"name": "read", "annotations": {"readOnlyHint": True}}]})
        row(db, sid, at=D(2), proxy_type="mcp", mcp_method="tools/call", server_id="srv-ro-" + sid,
            mcp_capability_name="read")
        assert ev(db, sid, "records.every_verdict_sealed").status == NOT_EVALUABLE
        db.execute(text("UPDATE resource_groups SET phi_resource = true WHERE id = :s"), {"s": sid})
        r = ev(db, sid, "records.every_verdict_sealed")
        assert (r.status, r.m) == (NOT_MET, 1)

    def test_sensitive_data_flag_seals_read_only_tools_too(self, db):
        # The framework-neutral flag, read as a column.
        sid = system(db)
        ins(db, "mcp_servers", id="srv-sd-" + sid, tenant_id=TENANT, name="ro", base_url="http://x",
            mcp_endpoint_path="/mcp",
            capabilities={"tools": [{"name": "read", "annotations": {"readOnlyHint": True}}]})
        row(db, sid, at=D(2), proxy_type="mcp", mcp_method="tools/call", server_id="srv-sd-" + sid,
            mcp_capability_name="read")
        assert ev(db, sid, "records.every_verdict_sealed").status == NOT_EVALUABLE
        db.execute(text("UPDATE resource_groups SET sensitive_data = true WHERE id = :s"), {"s": sid})
        assert ev(db, sid, "records.every_verdict_sealed").m == 1

    def test_planned_records_are_not_rows_and_unconfirmed_ones_are_surfaced(self, db):
        sid = system(db)
        sealed_row(db, sid, at=D(2))
        # Confirmed two-phase action: planned, then the outcome confirms it.
        p_ok = record(db, sid, verdict="planned", at=D(3))
        record(db, sid, at=D(3), chain={"parent_record_id": p_ok, "relation": "confirms"})
        # Dispatch attested, outcome never recorded (crash mid-dispatch).
        p_gap = record(db, sid, verdict="planned", at=D(4))
        # Still inside the grace — the outcome may yet land.
        record(db, sid, verdict="planned", at=WE - timedelta(minutes=1))
        # A row wrongly pointing at a planned record is not a sealed row.
        bad = record(db, sid, verdict="planned", at=D(5))
        row(db, sid, at=D(5), proxy_type="a2a", record_id=bad)
        r = ev(db, sid, "records.every_verdict_sealed", now=WE + timedelta(minutes=5))
        # Only proxy-log rows count: the sealed one and the mis-linked one.
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 2)
        assert r.facts["planned_records"] == 4
        assert r.facts["planned_unconfirmed"] == 2  # p_gap and the mis-linked one
        assert {x["id"] for x in r.refs if x["type"] == "planned_record"} == {p_gap, bad}
        assert "2 planned action(s) never confirmed" in r.detail
        assert p_gap not in r.citations and bad not in r.citations

    def test_unconfirmed_planned_is_surfaced_even_when_all_rows_are_sealed(self, db):
        sid = system(db)
        sealed_row(db, sid, at=D(2))
        record(db, sid, verdict="planned", at=D(4))
        r = ev(db, sid, "records.every_verdict_sealed")
        assert (r.status, r.n, r.m) == (MET, 1, 1)
        assert r.facts["planned_unconfirmed"] == 1
        assert "never confirmed" in r.detail

    def test_a_nested_ai_system_owns_its_own_subtree(self, db):
        parent = system(db)
        child_group = system(db, parent=parent, group_type="TEAM")
        nested = system(db, parent=parent)
        sealed_row(db, child_group, at=D(2))
        row(db, nested, at=D(2))  # belongs to the nested system, not the parent
        r = ev(db, parent, "records.every_verdict_sealed")
        assert (r.status, r.n, r.m) == (MET, 1, 1)


class TestWitnessed:
    def _records(self, db, sid, n=3, at=D(2)):
        for i in range(n):
            record(db, sid, at=at, seq=100 + i)

    def test_witnessed_heads_meet(self, db):
        sid = system(db)
        self._records(db, sid)
        ins(db, "evidence_tree_heads", id=uid("th"), log_id=LOG_ID, tenant_id=TENANT, tree_size=103, root="r" * 64,
            prev_size=0, statement=b"\x00", issued_at=D(3), grade="witnessed")
        r = ev(db, sid, "records.witnessed")
        assert (r.status, r.n, r.m) == (MET, 1, 1)
        assert r.facts["grades"]["witnessed"] == 1

    def test_standalone_or_self_witnessed_heads_are_not_met(self, db):
        sid = system(db)
        self._records(db, sid)
        ins(db, "evidence_tree_heads", id=uid("th"), log_id=LOG_ID, tenant_id=TENANT, tree_size=101, root="r" * 64,
            prev_size=0, statement=b"\x00", issued_at=D(3), grade="self_witnessed")
        ins(db, "evidence_tree_heads", id=uid("th"), log_id=LOG_ID, tenant_id=TENANT, tree_size=103, root="r" * 64,
            prev_size=101, statement=b"\x00", issued_at=D(3), grade="plural")
        r = ev(db, sid, "records.witnessed")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 2)
        assert "self-witnessed" in r.detail and "plural" in r.detail

    def test_records_no_head_covers_are_not_met(self, db):
        sid = system(db)
        self._records(db, sid)
        r = ev(db, sid, "records.witnessed")
        assert r.status == NOT_MET and r.facts["uncovered_records"] == 3 and len(r.citations) == 3

    def test_no_records_is_not_evaluable(self, db):
        assert ev(db, system(db), "records.witnessed").status == NOT_EVALUABLE


class TestChainComplete:
    def test_closed_approval_chain_and_sealed_epoch_meet(self, db):
        sid = system(db)
        parent = record(db, sid, verdict="awaiting_human", at=D(2))
        record(db, sid, verdict="executed", at=D(2, 13), chain={"parent_record_id": parent, "relation": "supersedes"})
        contract(db, sid, version=2, promoted_at=D(3))
        record(db, sid, verdict="epoch_boundary", at=D(3), kind="fyi",
               context={"contract_version": "2"}, extra={"disposition": {"approver": "human"}})
        r = ev(db, sid, "records.chain_complete")
        assert (r.status, r.n, r.m) == (MET, 2, 2)

    def test_awaiting_human_never_closed_past_ttl_is_a_gap(self, db):
        sid = system(db)
        aid = approval(db, sid, status="pending", at=D(2), expires=D(2, 13))
        gap = record(db, sid, verdict="awaiting_human", at=D(2), context={"approval_request_id": aid})
        r = ev(db, sid, "records.chain_complete")
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 1)
        assert gap in r.citations and r.facts["approval_chain_gaps"] == 1

    def test_an_open_chain_within_its_ttl_is_in_flight_not_a_gap(self, db):
        sid = system(db)
        aid = approval(db, sid, status="pending", at=D(14), expires=NOW + timedelta(hours=2))
        record(db, sid, verdict="awaiting_human", at=D(14), context={"approval_request_id": aid})
        r = ev(db, sid, "records.chain_complete")
        assert r.status == NOT_EVALUABLE and r.facts["approval_chains_in_flight"] == 1

    def test_promotion_without_epoch_boundary_and_unconfirmed_planned(self, db):
        sid = system(db)
        contract(db, sid, version=3, promoted_at=D(4))
        record(db, sid, verdict="planned", at=D(5))
        r = ev(db, sid, "records.chain_complete")
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 2)
        assert r.facts["two_phase_unconfirmed"] == 1 and r.facts["promotions_missing_epoch_boundary"]


class TestIntegrityAndPrunes:
    def _rows(self, db, sid, chain="chain-a"):
        for i in range(3):
            row(db, sid, at=D(2), chain_id=chain, chain_seq=10 + i)

    def test_verified_chain_meets(self, db, monkeypatch):
        sid = system(db)
        self._rows(db, sid)
        calls = []

        def fake(chain_id, **kw):
            calls.append((chain_id, kw))
            return {"verdict": "verified", "truncated": False, "undeclared_gaps": [], "pruned_rows": 0}

        monkeypatch.setattr("app.services.evidence.core_client.verify_chain", fake)
        r = ev(db, sid, "records.integrity_verified")
        assert (r.status, r.n, r.m) == (MET, 1, 1)
        assert calls[0][0] == "chain-a" and calls[0][1]["from_seq"] == 10 and calls[0][1]["limit"] == 3

    @pytest.mark.parametrize("verdict", ["broken", "gap", "forged"])
    def test_bad_verdicts_are_not_met(self, db, monkeypatch, verdict):
        sid = system(db)
        self._rows(db, sid)
        monkeypatch.setattr("app.services.evidence.core_client.verify_chain",
                            lambda chain_id, **kw: {"verdict": verdict, "undeclared_gaps": [[1, 3]]})
        assert ev(db, sid, "records.integrity_verified").status == NOT_MET
        # Every one of these reports an undeclared gap range.
        assert ev(db, sid, "retention.declared_prunes").status == NOT_MET

    def test_unreachable_verifier_is_not_evaluable_never_met(self, db, monkeypatch):
        sid = system(db)
        self._rows(db, sid)
        monkeypatch.setattr("app.services.evidence.core_client.verify_chain",
                            lambda chain_id, **kw: {"error": "core_unreachable", "detail": "refused"})
        assert ev(db, sid, "records.integrity_verified").status == NOT_EVALUABLE
        assert ev(db, sid, "retention.declared_prunes").status == NOT_EVALUABLE

    def test_unchained_rows_are_undetermined(self, db, monkeypatch):
        sid = system(db)
        row(db, sid, at=D(2))
        assert ev(db, sid, "records.integrity_verified").status == UNDETERMINED

    def test_declared_prunes(self, db, monkeypatch):
        sid = system(db)
        self._rows(db, sid)
        monkeypatch.setattr("app.services.evidence.core_client.verify_chain",
                            lambda chain_id, **kw: {"verdict": "pruned", "undeclared_gaps": [], "pruned_rows": 4})
        r = ev(db, sid, "retention.declared_prunes")
        assert (r.status, r.n, r.m) == (MET, 1, 1) and r.facts["pruned_rows_declared"] == 4
        monkeypatch.setattr("app.services.evidence.core_client.verify_chain",
                            lambda chain_id, **kw: {"verdict": "gap", "undeclared_gaps": [[11, 13]]})
        assert ev(db, sid, "retention.declared_prunes").status == NOT_MET


# ---------------------------------------------------------------------------
# retention.floor
# ---------------------------------------------------------------------------


class TestRetentionFloor:
    def _policy(self, db, log_days, enable_eu=False):
        db.execute(text("UPDATE tenants SET data_retention = CAST(:p AS json), compliance_profile = CAST(:c AS json) "
                        "WHERE id = :t"),
                   {"t": TENANT, "p": json.dumps({"store_bodies": "full", "log_days": log_days}),
                    "c": json.dumps({"frameworks": ["eu_ai_act"] if enable_eu else []})})

    def test_policy_below_the_floor_is_not_met(self, db):
        sid = system(db, risk="high", role="deployer")
        self._policy(db, 90)
        r = ev(db, sid, "retention.floor", floor_days=183)
        assert r.status == NOT_MET and r.facts["effective_log_days"] == 90

    def test_active_framework_floor_raises_the_effective_retention(self, db):
        sid = system(db, risk="high", role="deployer")
        self._policy(db, 90, enable_eu=True)
        r = ev(db, sid, "retention.floor", floor_days=183)
        assert r.status == MET and r.facts["effective_log_days"] == 183
        assert r.facts["framework_floors"][0]["framework_id"] == "eu-ai-act"

    def test_no_policy_keeps_everything(self, db):
        sid = system(db)
        r = ev(db, sid, "retention.floor", floor_days=183)
        assert r.status == MET and r.facts["effective_log_days"] is None

    def test_a_prune_younger_than_the_floor_is_not_met(self, db):
        sid = system(db)
        # Survivors at seq 9 (2 days before the prune) and 20.
        row(db, sid, at=D(1), chain_id="chain-p", chain_seq=9)
        row(db, sid, at=D(1, 13), chain_id="chain-p", chain_seq=20)
        ins(db, "audit_chain_prunes", id=uid("prn"), chain_id="chain-p", tenant_id=TENANT, seq_min=10, seq_max=19,
            row_count=10, runs=[], reason="manual", pruned_at=D(3))
        r = ev(db, sid, "retention.floor", floor_days=183)
        assert r.status == NOT_MET and r.facts["prunes"]["too_young"] == 1

    def test_an_old_prune_meets(self, db):
        sid = system(db)
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        row(db, sid, at=old, chain_id="chain-q", chain_seq=9)
        row(db, sid, at=old + timedelta(hours=1), chain_id="chain-q", chain_seq=20)
        ins(db, "audit_chain_prunes", id=uid("prn"), chain_id="chain-q", tenant_id=TENANT, seq_min=10, seq_max=19,
            row_count=10, runs=[], reason="retention", pruned_at=D(3))
        r = ev(db, sid, "retention.floor", floor_days=183)
        assert (r.status, r.n, r.m) == (MET, 3, 3)

    def test_an_unbracketable_prune_is_undetermined(self, db):
        sid = system(db)
        ins(db, "audit_chain_prunes", id=uid("prn"), chain_id="chain-none", tenant_id=TENANT, seq_min=1, seq_max=5,
            row_count=5, runs=[], reason="retention", pruned_at=D(3))
        assert ev(db, sid, "retention.floor", floor_days=183).status == UNDETERMINED

    def test_documentation_scope(self, db):
        sid = system(db)
        assert ev(db, sid, "retention.floor", floor_days=2190, scope="documentation").status == NOT_EVALUABLE
        record(db, sid, verdict="recorded", at=D(2), kind="fyi", context={"record_type": "ai.brutor.declaration"})
        assert ev(db, sid, "retention.floor", floor_days=2190, scope="documentation").status == MET


# ---------------------------------------------------------------------------
# oversight.* / authz.* / access.*
# ---------------------------------------------------------------------------


def admin(db, *, perms=("run:abort",), totp=False) -> str:
    role = uid("role")
    ins(db, "tenant_roles", id=role, tenant_id=TENANT, name=role, is_system=False)
    for p in perms:
        ins(db, "tenant_role_permissions", id=uid("rp"), role_id=role, permission=p)
    aid = uid("adm")
    ins(db, "tenant_admins", id=aid, tenant_id=TENANT, role_id=role, username=aid, email=f"{aid}@x.invalid",
        password_hash="x", is_active=True, is_locked=False, failed_login_attempts=0, totp_enabled=totp)
    return aid


class TestOversight:
    def test_override_available(self, db):
        sid = system(db)
        assert ev(db, sid, "oversight.override_available").status == NOT_MET
        admin(db)
        assert ev(db, sid, "oversight.override_available").status == UNDETERMINED
        profile(db, sid, core={"oversight_assignment": "Claims duty manager"})
        r = ev(db, sid, "oversight.override_available")
        assert (r.status, r.n, r.m) == (MET, 2, 2) and r.citations

    def test_exercised(self, db):
        sid = system(db)
        assert ev(db, sid, "oversight.exercised").status == NOT_EVALUABLE
        a = admin(db)
        approval(db, sid, status="approved", at=D(2), resolved_by=a)
        approval(db, sid, status="rejected", at=D(3), resolved_by=a)
        r = ev(db, sid, "oversight.exercised")
        assert (r.status, r.n, r.m) == (MET, 2, 2)
        approval(db, sid, status="expired", at=D(4))
        approval(db, sid, status="pending", at=D(14), expires=NOW + timedelta(hours=1))
        r = ev(db, sid, "oversight.exercised")
        assert (r.status, r.n, r.m) == (NOT_MET, 2, 3)
        assert r.facts["lapsed"] == 1 and r.facts["in_flight"] == 1

    def test_an_approval_the_agent_carried_out_is_a_human_decision(self, db):
        """`consumed` = approved by a person, token since redeemed. Counting
        only `approved`/`rejected` lost every approval that was acted on (the
        demo's row read "1 of 42")."""
        sid = system(db)
        a = admin(db)
        for day in (2, 3, 4):
            approval(db, sid, status="consumed", at=D(day), resolved_by=a)
        approval(db, sid, status="rejected", at=D(5), resolved_by=a)
        r = ev(db, sid, "oversight.exercised")
        assert (r.status, r.n, r.m) == (MET, 4, 4)
        assert r.facts["decided_by_human"] == 4 and r.facts["lapsed"] == 0
        who = ev(db, sid, "authz.human_identity_verified")
        assert who.m == 4

    def test_a_pending_hold_past_its_deadline_has_lapsed(self, db):
        """The sweeper rewrites it within a minute; until then it is not in flight."""
        sid = system(db)
        approval(db, sid, status="pending", at=D(2), expires=D(2) + timedelta(hours=1))
        r = ev(db, sid, "oversight.exercised")
        assert r.facts["lapsed"] == 1 and r.facts["in_flight"] == 0
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 1)

    def test_not_rubber_stamp_is_an_indicator_never_a_verdict(self, db):
        sid = system(db)
        r = ev(db, sid, "oversight.not_rubber_stamp", basis="indicator")
        assert r.status == NOT_EVALUABLE and r.n is None
        ins(db, "ai_system_baselines", id=uid("base"), tenant_id=TENANT, ai_system_id=sid,
            metric_key="approval_latency_p50", state="ready", sample_count=80)
        r = ev(db, sid, "oversight.not_rubber_stamp", basis="indicator")
        assert r.status == INDICATOR and "No movement" in r.detail
        ins(db, "ai_system_drift_events", id=uid("drift"), tenant_id=TENANT, ai_system_id=sid,
            drift_class="behavioural", metric_key="approval_latency_p50", detected_at=D(5), direction="down")
        r = ev(db, sid, "oversight.not_rubber_stamp", basis="indicator")
        assert r.status == INDICATOR and r.n is None and r.m is None
        assert len(r.facts["movement_events"]) == 1 and "not a finding against a threshold" in r.detail

    def test_human_identity(self, db):
        sid = system(db)
        assert ev(db, sid, "authz.human_identity_verified").status == NOT_EVALUABLE
        a = admin(db, totp=False)
        approval(db, sid, status="approved", at=D(2), resolved_by=a)
        assert ev(db, sid, "authz.human_identity_verified").status == MET
        approval(db, sid, status="approved", at=D(3), resolved_by="system:envelope-evaluator")
        r = ev(db, sid, "authz.human_identity_verified")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 2)
        db.execute(text("UPDATE tenants SET mfa_policy = 'required_admins' WHERE id = :t"), {"t": TENANT})
        r = ev(db, sid, "authz.human_identity_verified")
        assert (r.n, r.m) == (0, 2) and r.facts["missing_required_2fa"] == 1

    def test_out_of_grant_refused(self, db):
        sid = system(db)
        assert ev(db, sid, "authz.out_of_grant_refused").status == NOT_EVALUABLE
        h = f"sha256:{hex64()}"
        contract(db, sid, version=1, promoted_at=D(1), chash=h)
        pid = uid("pl")
        rid = record(db, sid, verdict="denied", at=D(2), proxy_log_id=pid, extra={"system": {"contract_hash": h}})
        ins(db, "proxy_logs", id=pid, tenant_id=TENANT, timestamp=D(2), proxy_type="a2a", resource_group_id=sid,
            end_user_session_id="", http_method="POST", endpoint="/x", success=False, record_id=rid,
            agent_authz_decision="deny")
        r = ev(db, sid, "authz.out_of_grant_refused")
        assert r.status == MET and r.facts["out_of_grant_sealed_denied"] == 1
        record(db, sid, at=D(3), extra={"system": {"contract_hash": f"sha256:{hex64()}"}})
        assert ev(db, sid, "authz.out_of_grant_refused").status == NOT_MET
        row(db, sid, at=D(4), proxy_subtype="agent_authz_block")  # denial never sealed
        assert ev(db, sid, "authz.out_of_grant_refused").facts["out_of_grant_attempts"] == 2

    def test_least_privilege_declared(self, db):
        sid = system(db)
        assert ev(db, sid, "authz.least_privilege_declared", basis="declared").status == UNDETERMINED
        contract(db, sid, version=1, promoted_at=D(1))
        assert ev(db, sid, "authz.least_privilege_declared", basis="declared").status == UNDETERMINED
        profile(db, sid)
        r = ev(db, sid, "authz.least_privilege_declared", basis="declared")
        assert r.status == DECLARED and len(r.citations) == 1

    def test_sensitive_reads_recorded(self, db):
        sid = system(db)
        assert ev(db, sid, "access.sensitive_reads_recorded", tag="hipaa.phi").status == NOT_EVALUABLE
        sealed_row(db, sid, at=D(2), compliance_tags=["hipaa.phi"])
        row(db, sid, at=D(3), compliance_tags=["hipaa.phi", "soc2.cc7.2"])
        row(db, sid, at=D(3), compliance_tags=["soc2.cc7.2"])
        r = ev(db, sid, "access.sensitive_reads_recorded", tag="hipaa.phi")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 2)


# ---------------------------------------------------------------------------
# monitoring.* / change.*
# ---------------------------------------------------------------------------


class TestMonitoringAndChange:
    def test_daily_health(self, db):
        sid = system(db)
        ws, we, now = D(1, 0), D(4, 0), D(4, 0)
        r = ev(db, sid, "monitoring.daily_health", ws=ws, we=we, now=now)
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 3)  # [1st, 4th) — the end is exclusive
        for day in (1, 2, 3):
            ins(db, "ai_system_assurance_reports", id=uid("rep"), tenant_id=TENANT, ai_system_id=sid,
                period_hours=24, report_json={}, created_at=D(day))
        ins(db, "ai_system_drift_events", id=uid("drift"), tenant_id=TENANT, ai_system_id=sid,
            drift_class="cost", metric_key="cost", detected_at=D(3, 23))
        r = ev(db, sid, "monitoring.daily_health", ws=ws, we=we, now=now)
        assert (r.status, r.n, r.m) == (MET, 3, 3)

    def test_suspend_available_and_abort_sealing(self, db):
        sid = system(db)
        assert ev(db, sid, "monitoring.suspend_available").status == NOT_MET  # nobody holds run:abort
        admin(db)
        row(db, sid, at=D(2), root_task_id="run-1")
        ins(db, "run_interventions", id="ri-" + sid, tenant_id=TENANT, root_task_id="run-1", reason="stop",
            requested_at=D(2, 13))
        r = ev(db, sid, "monitoring.suspend_available")
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 1)
        record(db, sid, verdict="resolved", at=D(2, 13),
               context={"record_type": "ai.brutor.run-abort", "intervention_id": "ri-" + sid})
        assert ev(db, sid, "monitoring.suspend_available").status == MET

    def test_promotions_sealed(self, db):
        sid = system(db)
        assert ev(db, sid, "change.promotions_sealed").status == NOT_EVALUABLE
        contract(db, sid, version=1, promoted_at=D(2), status="superseded")
        contract(db, sid, version=2, promoted_at=D(5))
        record(db, sid, verdict="epoch_boundary", at=D(5), kind="fyi", context={"contract_version": "2"},
               extra={"disposition": {"approver": "policy"}})
        r = ev(db, sid, "change.promotions_sealed")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 2)
        assert r.facts["missing"][0]["version"] == 1

    def test_no_unapproved_widening(self, db):
        sid = system(db)
        assert ev(db, sid, "change.no_unapproved_widening").status == NOT_EVALUABLE
        contract(db, sid, version=1, promoted_at=D(2))
        assert ev(db, sid, "change.no_unapproved_widening").status == MET
        contract(db, sid, version=2, promoted_at=D(3), status="superseded", widened=True, approved_by=None)
        r = ev(db, sid, "change.no_unapproved_widening")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 2)


# ---------------------------------------------------------------------------
# notice.* / synthetic.*
# ---------------------------------------------------------------------------

HANDLE_A, HANDLE_B, HANDLE_C = "01K5A0000000000000000000AA", "01K5A0000000000000000000BB", "01K5A0000000000000000000CC"


def notice(db, handle: str, at: datetime, seq: int) -> str:
    return record(db, None, verdict="recorded", at=at, seq=seq, kind="fyi",
                  context={"record_type": "ai.brutor.notice", "conversation_handle": handle})


class TestNotice:
    def test_notice_before_first_turn(self, db):
        sid = system(db)
        assert ev(db, sid, "notice.before_first_turn").status == NOT_EVALUABLE
        notice(db, HANDLE_A, D(2, 10), seq=500)
        first = record(db, sid, at=D(2, 11), seq=501)
        row(db, sid, at=D(2, 11), proxy_type="llm", record_id=first,
            custom_metadata={"conversation_handle": HANDLE_A.lower()})
        row(db, sid, at=D(2, 12), proxy_type="llm", custom_metadata={"conversation_handle": HANDLE_A})
        r = ev(db, sid, "notice.before_first_turn")
        assert (r.status, r.n, r.m) == (MET, 1, 1)

    def test_a_late_notice_is_not_met(self, db):
        sid = system(db)
        row(db, sid, at=D(3, 10), proxy_type="llm", custom_metadata={"conversation_handle": HANDLE_B})
        notice(db, HANDLE_B, D(3, 10) + timedelta(seconds=1), seq=600)
        r = ev(db, sid, "notice.before_first_turn")
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 1) and r.facts["late_notice"] == 1

    def test_log_order_counts_even_when_the_clock_agrees(self, db):
        sid = system(db)
        first = record(db, sid, at=D(4, 11), seq=700)
        notice(db, HANDLE_C, D(4, 10), seq=701)  # earlier timestamp, later in the log
        row(db, sid, at=D(4, 11), proxy_type="llm", record_id=first,
            custom_metadata={"conversation_handle": HANDLE_C})
        r = ev(db, sid, "notice.before_first_turn")
        assert r.status == NOT_MET and r.facts["late_notice"] == 1

    def test_missing_notice_and_a_conversation_that_began_before_the_window(self, db):
        sid = system(db)
        row(db, sid, at=D(5), proxy_type="llm", custom_metadata={"conversation_handle": "01K5A00000000000000000000D"})
        row(db, sid, at=datetime(2026, 8, 20, tzinfo=timezone.utc), proxy_type="llm",
            custom_metadata={"conversation_handle": "01K5A00000000000000000000E"})
        row(db, sid, at=D(5), proxy_type="llm", custom_metadata={"conversation_handle": "01K5A00000000000000000000E"})
        r = ev(db, sid, "notice.before_first_turn")
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 1) and r.facts["no_notice"] == 1

    def test_exemption_is_declared(self, db):
        sid = system(db)
        ins(db, "compliance_notice_configs", id=uid("ntc"), tenant_id=TENANT, ai_system_id=sid, enabled=False,
            surface="both", texts={}, text_digests={}, record_id=hex64(),
            exemption={"asserted": True, "basis": "obvious", "rationale": "x"})
        assert ev(db, sid, "notice.before_first_turn").status == DECLARED
        assert ev(db, sid, "notice.text_declared", basis="declared").status == DECLARED

    def test_text_declared(self, db):
        sid = system(db)
        assert ev(db, sid, "notice.text_declared", basis="declared").status == UNDETERMINED
        ins(db, "compliance_notice_configs", id=uid("ntc"), tenant_id=TENANT, ai_system_id=None, enabled=True,
            surface="banner", texts={"en": "You are talking to an AI"}, text_digests={"en": hex64()})
        assert ev(db, sid, "notice.text_declared", basis="declared").status == UNDETERMINED  # unsealed
        db.execute(text("UPDATE compliance_notice_configs SET record_id = :r WHERE tenant_id = :t"),
                   {"r": hex64(), "t": TENANT})
        r = ev(db, sid, "notice.text_declared", basis="declared")
        assert r.status == DECLARED and r.facts["scope"] == "tenant" and r.facts["locales"] == ["en"]
        db.execute(text("UPDATE compliance_notice_configs SET enabled = false WHERE tenant_id = :t"), {"t": TENANT})
        assert ev(db, sid, "notice.text_declared", basis="declared").status == NOT_MET

    def test_synthetic_marking(self, db):
        sid = system(db)
        assert ev(db, sid, "synthetic.marking_declared", basis="declared").status == UNDETERMINED
        profile(db, sid, core={"synthetic_marking": "X-Brutor-AI-Generated header on every response"})
        r = ev(db, sid, "synthetic.marking_declared", basis="declared")
        assert r.status == DECLARED and r.facts["content_marking_provided"] is False


# ---------------------------------------------------------------------------
# incidents.* / evidence.* / docs.* / profile.*
# ---------------------------------------------------------------------------


def incident(db, sid, *, aware_at, reported_at=None, classification="standard", status="open", deadlines=None):
    ins(db, "compliance_incidents", id=uid("inc"), tenant_id=TENANT, ai_system_id=sid, title="t", description="",
        classification=classification, severity="high", became_aware_at=aware_at, frameworks=["eu-ai-act"],
        deadlines=deadlines or {}, status=status, reported_at=reported_at, linked_run_ids=[],
        linked_inbox_item_ids=[])


class TestIncidentsAndDocuments:
    def test_reported_within_deadline(self, db):
        sid = system(db)
        assert ev(db, sid, "incidents.reported_within_deadline", framework="eu-ai-act").status == NOT_EVALUABLE
        incident(db, sid, aware_at=D(1), reported_at=D(10), status="reported")                 # 9 d ≤ 15
        r = ev(db, sid, "incidents.reported_within_deadline", framework="eu-ai-act")
        assert (r.status, r.n, r.m) == (MET, 1, 1)
        incident(db, sid, aware_at=D(2), reported_at=D(14), classification="death", status="reported")  # 12 > 10
        incident(db, sid, aware_at=D(3), classification="widespread")                          # overdue, unreported
        incident(db, sid, aware_at=D(14))                                                      # still in time
        r = ev(db, sid, "incidents.reported_within_deadline", framework="eu-ai-act")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 3)
        assert r.facts["within_deadline_not_yet_reported"] == 1

    def test_stored_deadline_wins_over_the_rule(self, db):
        sid = system(db)
        incident(db, sid, aware_at=D(1), reported_at=D(10), status="reported",
                 deadlines={"eu-ai-act": {"due_at": D(5).isoformat()}})
        assert ev(db, sid, "incidents.reported_within_deadline", framework="eu-ai-act").status == NOT_MET

    def test_stored_rule_map_is_authoritative_over_a_changed_catalog(self, db):
        # Raised when the catalog gave this classification 30 days (stored as
        # `{rule: ISO}` by compute_deadlines); today's catalog says 15. The
        # clock frozen on the incident decides: reported on day 20 is on time.
        sid = system(db)
        incident(db, sid, aware_at=D(1), reported_at=D(20), status="reported",
                 deadlines={"eu-ai-act": {"standard": (D(1) + timedelta(days=30)).isoformat()}})
        r = ev(db, sid, "incidents.reported_within_deadline", framework="eu-ai-act")
        assert (r.status, r.n, r.m) == (MET, 1, 1)

    def test_stored_multi_stage_uses_the_earliest_stage(self, db):
        sid = system(db)
        incident(db, sid, aware_at=D(1), reported_at=D(5), status="reported",
                 deadlines={"eu-ai-act": {"final": (D(1) + timedelta(days=30)).isoformat(),
                                          "initial": (D(1) + timedelta(days=2)).isoformat()}})
        r = ev(db, sid, "incidents.reported_within_deadline", framework="eu-ai-act")
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 1)

    def test_dora_stages_on_their_own_anchors(self, db):
        # Classified 10 h after awareness and notified 2 h later (via
        # `reported_at`) is on time — measured from awareness it was late —
        # and so are its later stages. A second incident whose
        # initial notification was on time but whose intermediate report is
        # past 72 h from that submission counts against.
        sid = system(db)
        hour = timedelta(hours=1)
        common = dict(tenant_id=TENANT, ai_system_id=sid, title="t", description="", classification="major_incident",
                      severity="high", frameworks=["dora"], deadlines={}, linked_run_ids=[],
                      linked_inbox_item_ids=[])
        later = {"intermediate": (D(1) + 40 * hour).isoformat(), "final": (D(1) + 50 * hour).isoformat()}
        ins(db, "compliance_incidents", id=uid("inc"), became_aware_at=D(1), classified_at=D(1) + 10 * hour,
            reported_at=D(1) + 12 * hour, status="reported", stage_submissions={"dora": later}, **common)
        r = ev(db, sid, "incidents.reported_within_deadline", framework="dora")
        assert (r.status, r.n, r.m) == (MET, 1, 1)
        ins(db, "compliance_incidents", id=uid("inc"), became_aware_at=D(2), classified_at=D(2) + hour,
            status="reported", stage_submissions={"dora": {"initial": (D(2) + 2 * hour).isoformat()}}, **common)
        r = ev(db, sid, "incidents.reported_within_deadline", framework="dora")
        assert (r.status, r.n, r.m) == (NOT_MET, 1, 2)

    def test_register_exists(self, db):
        sid = system(db)
        assert ev(db, sid, "incidents.register_exists").status == UNDETERMINED
        incident(db, sid, aware_at=D(14))
        assert ev(db, sid, "incidents.register_exists").status == MET
        incident(db, sid, aware_at=D(1), classification="widespread")
        assert ev(db, sid, "incidents.register_exists").status == NOT_MET

    def test_pointer_on_file(self, db):
        sid = system(db)
        assert ev(db, sid, "evidence.pointer_on_file", kind="risk_assessment").status == NOT_MET
        ins(db, "ai_system_evidence", id=uid("evd"), tenant_id=TENANT, ai_system_id=sid, kind="risk_assessment",
            title="RA 2025", valid_until=datetime(2026, 1, 1, tzinfo=timezone.utc))
        r = ev(db, sid, "evidence.pointer_on_file", kind="risk_assessment")
        assert (r.status, r.n, r.m) == (NOT_MET, 0, 1)
        ins(db, "ai_system_evidence", id=uid("evd"), tenant_id=TENANT, ai_system_id=sid, kind="risk_assessment",
            title="RA 2026", valid_until=None, independent=True)
        r = ev(db, sid, "evidence.pointer_on_file", kind="risk_assessment")
        assert (r.status, r.n, r.m) == (MET, 1, 2) and r.facts["independent"] == 1

    def test_technical_documentation(self, db):
        sid = system(db)
        assert ev(db, sid, "docs.technical_documentation", framework="eu-ai-act").status == NOT_EVALUABLE
        contract(db, sid, version=4, promoted_at=D(2))
        ins(db, "compliance_documentation_exports", id=uid("doc"), tenant_id=TENANT, ai_system_id=sid,
            framework_id="eu-ai-act", template="eu-annex-iv", contract_version=3, content={}, digest=hex64(),
            record_id=hex64())
        r = ev(db, sid, "docs.technical_documentation", framework="eu-ai-act")
        assert r.status == NOT_MET and "v3" in r.detail
        ins(db, "compliance_documentation_exports", id=uid("doc"), tenant_id=TENANT, ai_system_id=sid,
            framework_id="eu-ai-act", template="eu-annex-iv", contract_version=4, content={}, digest=hex64())
        assert "not sealed" in ev(db, sid, "docs.technical_documentation", framework="eu-ai-act").detail
        db.execute(text("UPDATE compliance_documentation_exports SET record_id = :r WHERE contract_version = 4 "
                        "AND tenant_id = :t"), {"r": hex64(), "t": TENANT})
        assert ev(db, sid, "docs.technical_documentation", framework="eu-ai-act").status == MET

    def test_registration_and_profile_fields(self, db):
        sid = system(db)
        assert ev(db, sid, "docs.registration_recorded", basis="declared", framework="eu-ai-act").status == UNDETERMINED
        assert ev(db, sid, "profile.field_declared", basis="declared",
                  path="frameworks.eu-ai-act.qms_reference").status == UNDETERMINED
        profile(db, sid, core={"external_registrations": [{"framework_id": "eu-ai-act", "registry": "EU DB",
                                                            "id": "EUDB-1"}]},
                frameworks={"eu-ai-act": {"qms_reference": "QMS-7"}}, sealed=False)
        # Declared but unsealed: undetermined, never `declared`.
        assert ev(db, sid, "profile.field_declared", basis="declared",
                  path="frameworks.eu-ai-act.qms_reference").status == UNDETERMINED
        db.execute(text("UPDATE ai_system_compliance_profiles SET declaration_record_id = :r WHERE ai_system_id = :s"),
                   {"r": hex64(), "s": sid})
        assert ev(db, sid, "profile.field_declared", basis="declared",
                  path="frameworks.eu-ai-act.qms_reference").status == DECLARED
        assert ev(db, sid, "docs.registration_recorded", basis="declared", framework="eu-ai-act").status == DECLARED


# ---------------------------------------------------------------------------
# judge.<template>
# ---------------------------------------------------------------------------


class TestJudged:
    def _check(self, db, template="notice_clear") -> str:
        cid = uid("chk")
        ins(db, "assurance_checks", id=cid, tenant_id=TENANT, name=cid, spec_hash="h", tier="semantic",
            evaluator={"template": template}, output="boolean", trigger="batch")
        return cid

    def _assessment(self, db, sid, cid, result, fp="fp-1", at=D(3)):
        return record(db, sid, verdict="assessment", at=at, kind="fyi",
                      context={"check_id": cid, "evaluator_fingerprint": fp},
                      extra={"controls": [{"id": "ai.brutor.judge.notice_clear", "result": result}]})

    def test_no_bound_check_is_not_evaluable(self, db):
        assert ev(db, system(db), "judge.notice_clear", basis="judged").status == NOT_EVALUABLE

    def test_unsealed_results_are_not_evidence(self, db):
        sid = system(db)
        cid = self._check(db)
        ins(db, "assurance_check_results", id=uid("ckr"), tenant_id=TENANT, check_id=cid, check_version=1,
            value={"value": True}, evaluator_fingerprint="fp", evaluated_at=D(3), backtest=False)
        r = ev(db, sid, "judge.notice_clear", basis="judged")
        assert r.status == NOT_EVALUABLE and r.facts["unsealed_results"] == 1

    def test_judged_counts_with_agreement_beside_never_merged(self, db):
        sid = system(db)
        cid = self._check(db)
        for res in ["pass"] * 4 + ["fail", "n/a"]:
            self._assessment(db, sid, cid, res)
        for i in range(12):
            human = "met" if i < 11 else "not_met"
            ins(db, "assurance_check_reviews", id=uid("rev"), tenant_id=TENANT, check_id=cid,
                evaluator_fingerprint="fp-1", week=date(2026, 9, 7), sample_key=f"s{i}", judged_result="met",
                human_result=human, status="graded", blind=True, reviewed_at=D(10))
        r = ev(db, sid, "judge.notice_clear", basis="judged")
        assert (r.status, r.n, r.m) == (NOT_MET, 4, 5)
        agreement = r.facts["agreement"][cid]
        assert agreement["n"] == 12 and agreement["observed_agreement"] == pytest.approx(11 / 12, abs=1e-3)
        assert agreement["insufficient_sample"] is False
        assert "human agreement 0.92" in r.detail and "blind" in r.detail


# ---------------------------------------------------------------------------
# Version in force and shared-resolver consistency
# ---------------------------------------------------------------------------


class TestVersionInForce:
    def test_a_window_across_the_omnibus_is_split_and_labelled(self, db):
        sid = system(db, risk="high", role="provider")
        sealed_row(db, sid, at=datetime(2026, 7, 20, tzinfo=timezone.utc))
        sealed_row(db, sid, at=datetime(2026, 8, 3, tzinfo=timezone.utc))
        row(db, sid, at=datetime(2026, 8, 4, tzinfo=timezone.utc))
        db.flush()
        s = db.get(ResourceGroup, sid)
        window = (datetime(2026, 7, 15, tzinfo=timezone.utc), datetime(2026, 8, 10, tzinfo=timezone.utc))
        evaluator = SystemEvaluator(db, TENANT, CATALOGS["eu-ai-act"], s, None, window,
                                    now=datetime(2026, 8, 10, tzinfo=timezone.utc))
        art12 = evaluator.obligation(CATALOGS["eu-ai-act"].obligation("art12-record-keeping"))
        sealed = [r for r in art12["statements"] if r.resolver == "records.every_verdict_sealed"]
        assert [r.catalog_version for r in sealed] == ["2024-08", "2026-09"]
        assert [(r.status, r.n, r.m) for r in sealed] == [(MET, 1, 1), (NOT_MET, 1, 2)]
        assert sealed[0].window_end == datetime(2026, 7, 27, tzinfo=timezone.utc)
        # The Omnibus-only prohibitions are absent from the pre-Omnibus segment.
        extra = evaluator.obligation(CATALOGS["eu-ai-act"].obligation("art5-prohibitions-2026"))
        assert {r.catalog_version for r in extra["statements"]} <= {"2026-09"}


class TestSharedResolverConsistency:
    """The same fixture month under two frameworks that cite the same
    `(resolver, params)` yields identical statement results. The
    other framework catalogs extend this to three."""

    FRAMEWORKS = ("eu-ai-act", "iso-42001", "soc2")

    def test_same_evidence_same_rows(self, db, monkeypatch):
        monkeypatch.setattr("app.services.evidence.core_client.verify_chain",
                            lambda chain_id, **kw: {"verdict": "verified", "undeclared_gaps": []})
        sid = system(db, risk="high", role="provider_and_deployer")
        a = admin(db)
        sealed_row(db, sid, at=D(2), chain_id="chain-s", chain_seq=1)
        row(db, sid, at=D(3), chain_id="chain-s", chain_seq=2)
        approval(db, sid, status="approved", at=D(4), resolved_by=a)
        contract(db, sid, version=1, promoted_at=D(5))
        db.flush()
        s = db.get(ResourceGroup, sid)
        results = {}
        for fid in [f for f in self.FRAMEWORKS if f in CATALOGS]:
            fw = CATALOGS[fid]
            evaluator = SystemEvaluator(db, TENANT, fw, s, None, (WS, WE), now=NOW)
            by_identity = {}
            for ob in fw.obligations:
                for spec_ in ob.evidence:
                    seg = evaluator.segments[-1]
                    r = evaluator.statement(spec_, seg)
                    by_identity[spec_.identity] = (r.status, r.n, r.m, json.dumps(r.facts, sort_keys=True, default=str))
            results[fid] = by_identity
        frameworks = list(results)
        assert len(frameworks) >= 2
        shared_total = 0
        for i, f1 in enumerate(frameworks):
            for f2 in frameworks[i + 1:]:
                shared = set(results[f1]) & set(results[f2])
                shared_total += len(shared)
                for identity in shared:
                    assert results[f1][identity] == results[f2][identity], (f1, f2, identity)
        assert shared_total >= 3, "the catalogs should cite some of the same resolvers"

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence Ledger API — HTTP behavioural spec

On the shared admin harness: real auth, full-schema SQLite. The rows the
Rust core would write — a tenant log, its node set, sealed records, a
tree head with a receipt — are seeded with the same append algorithm the
core uses, so `/proof` is exercised against a store shaped exactly like
production's, and the proof it serves is checked with the RFC 9162
verifier.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.evidence import create_evidence_router
from app.core.logging_system import BrutorLogger
from app.database.models.action_record import ActionRecord
from app.database.models.evidence_log import (
    EvidenceLog,
    EvidenceLogEntry,
    EvidenceLogNode,
    EvidenceReceipt,
    EvidenceTreeHead,
)
from app.database.models.disclosure_record import DisclosureRecord
from app.database.models.witness import Witness
from app.services.evidence import core_client, merkle
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
OTHER = "t2"
LOG_ID = hashlib.sha256(b"deployment|t1").hexdigest()
KID = "ab" * 32
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

# (verdict, kind, surface, system, run, human_disposed)
RECORDS = [
    ("executed", "decide", "mcp", "sys-a", "run-1", False),
    ("blocked", "decide", "mcp", "sys-a", "run-1", False),
    ("denied", "decide", "a2a", "sys-b", "run-2", False),
    ("awaiting_human", "decide", "mcp", "sys-a", "run-3", False),
    ("executed", "decide", "mcp", "sys-a", "run-3", True),
    ("epoch_boundary", "fyi", "contract", "sys-b", None, False),
    ("executed", "decide", "skill", None, "run-4", False),
]


def _record_body(i, verdict, kind, surface, system, run, human):
    body = {
        "schema": "ai.brutor/action-record/v1",
        "action_id": f"{surface}/plog-{i}",
        "kind": kind,
        "operator": TENANT,
        "system": {"ai_system_id": system} if system else "ungoverned",
        "timestamp": (T0 + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "disposition": {
            "decision": "accept" if verdict == "executed" else "reject",
            "approver": "human" if human else "policy",
            "human_disposed": human,
        },
        "context": {"root_task_id": run, "surface": surface},
    }
    if verdict == "executed":
        body["effect"] = {"type": "tools/call", "status": "confirmed", "response_digest": "00" * 32}
    if human:
        body["chain"] = {"parent_record_id": "rec-3", "relation": "supersedes"}
    return body


def seed(session):
    statements = [f"statement-{i}".encode() for i in range(len(RECORDS))]
    nodes = {}
    for i, stmt in enumerate(statements):
        merkle.append_leaf(nodes, i, merkle.leaf_hash(stmt))
    n = len(statements)
    get_node = merkle.store_getter(nodes)
    root = merkle.root_hash(get_node, n)

    session.add(EvidenceLog(log_id=LOG_ID, tenant_id=TENANT, next_seq=n, tree_size=n, root=root, kid=KID))
    for (level, index), h in nodes.items():
        session.add(EvidenceLogNode(log_id=LOG_ID, level=level, index=index, hash=h))
    for i, (spec, stmt) in enumerate(zip(RECORDS, statements)):
        verdict, kind, surface, system, run, human = spec
        session.add(ActionRecord(
            record_id=f"rec-{i}", tenant_id=TENANT, log_id=LOG_ID, seq=i,
            ai_system_id=system, root_task_id=run, proxy_log_id=f"plog-{i}",
            verdict=verdict, kind=kind, surface=surface, epoch="contract:deadbeef",
            timestamp=T0 + timedelta(minutes=i), tags=["soc2.cc7.2"] if surface == "mcp" else None,
            record=_record_body(i, *spec), statement=stmt, kid=KID,
        ))
        session.add(EvidenceLogEntry(
            log_id=LOG_ID, seq=i, kind="record", leaf_hash=merkle.leaf_hash(stmt), ref_id=f"rec-{i}",
        ))
    session.flush()

    session.add(EvidenceTreeHead(
        id="head-1", log_id=LOG_ID, tenant_id=TENANT, tree_size=4,
        root=merkle.root_hash(get_node, 4), prev_size=0, statement=b"head-statement",
        issued_at=T0 + timedelta(minutes=4), grade="witnessed",
    ))
    session.add(EvidenceTreeHead(
        id="head-2", log_id=LOG_ID, tenant_id=TENANT, tree_size=n,
        root=root, prev_size=4, prev_root=merkle.root_hash(get_node, 4),
        statement=b"head-statement-2", issued_at=T0 + timedelta(minutes=8), grade="standalone",
    ))
    session.flush()
    session.add(EvidenceReceipt(
        id="rcpt-1", tree_head_id="head-1", witness_id="wit-platform", kind="scitt",
        receipt=b"receipt-bytes", leaf_index=41, tree_size=42, witness_iat=1_800_000_000,
        verdict="witnessed", verified_at=T0 + timedelta(minutes=5),
    ))
    session.add(Witness(
        id="wit-platform", scope="platform", tenant_id=None, kind="scitt",
        url="https://witness.example/entries", operator_label="Example Witness Co",
        pinned_key="pk-1", enabled=True, same_operator=False,
    ))
    session.add(Witness(
        id="wit-t2", scope="tenant", tenant_id=OTHER, kind="rfc3161",
        url="https://tsa.other.example", operator_label="Other Tenant TSA",
    ))
    session.add(DisclosureRecord(
        id="disc-1", tenant_id=TENANT, log_id=LOG_ID, seq=None, audience="external auditor",
        selector={"ai_system_id": "sys-a"}, completeness="contiguous",
        disclosed_record_ids=["rec-0", "rec-1"], suppressed_fields=["attestation.digest_salt"],
        bundle_digest="cd" * 32,
    ))


@pytest.fixture(scope="module")
def harness():
    logger = BrutorLogger("evidence-tests")
    h = build_admin_harness([create_evidence_router(logger)])
    h.seed(seed)
    yield h
    h.close()


def get(harness, path, tenant=TENANT, expect=200, **params):
    r = harness.client.get(path, params=params, headers=harness.headers(tenant))
    assert r.status_code == expect, r.text
    return r.json()


class TestRecords:
    def test_list_is_newest_first_with_summary_fields(self, harness):
        body = get(harness, "/v1/admin/evidence/records")
        assert body["total"] == len(RECORDS)
        assert body["limit"] == 50 and body["offset"] == 0
        first = body["items"][0]
        assert first["record_id"] == "rec-6"
        assert first["action_id"] == "skill/plog-6"
        assert first["effect_type"] == "tools/call"
        assert "record" not in first and "statement_b64" not in first

    def test_filters(self, harness):
        assert get(harness, "/v1/admin/evidence/records", verdict="executed")["total"] == 3
        assert get(harness, "/v1/admin/evidence/records", kind="fyi")["total"] == 1
        assert get(harness, "/v1/admin/evidence/records", surface="a2a")["total"] == 1
        assert get(harness, "/v1/admin/evidence/records", ai_system_id="sys-a")["total"] == 4
        assert get(harness, "/v1/admin/evidence/records", root_task_id="run-3")["total"] == 2
        window = get(
            harness, "/v1/admin/evidence/records",
            since=(T0 + timedelta(minutes=2)).isoformat(),
            until=(T0 + timedelta(minutes=4)).isoformat(),
        )
        assert {i["record_id"] for i in window["items"]} == {"rec-2", "rec-3", "rec-4"}

    def test_human_disposed_and_chain_surface_on_the_summary(self, harness):
        body = get(harness, "/v1/admin/evidence/records", root_task_id="run-3")
        by_id = {i["record_id"]: i for i in body["items"]}
        assert by_id["rec-4"]["human_disposed"] is True
        assert by_id["rec-4"]["chain"] == {"parent_record_id": "rec-3", "relation": "supersedes"}
        assert by_id["rec-3"]["human_disposed"] is False
        assert by_id["rec-3"]["chain"] is None

    def test_pagination(self, harness):
        page = get(harness, "/v1/admin/evidence/records", limit=3, offset=3)
        assert page["total"] == len(RECORDS)
        assert [i["record_id"] for i in page["items"]] == ["rec-3", "rec-2", "rec-1"]
        r = harness.client.get(
            "/v1/admin/evidence/records", params={"limit": 500}, headers=harness.headers()
        )
        assert r.status_code == 422

    def test_unknown_verdict_is_422(self, harness):
        get(harness, "/v1/admin/evidence/records", verdict="maybe", expect=422)

    def test_detail_carries_body_statement_and_log_position(self, harness):
        body = get(harness, "/v1/admin/evidence/records/rec-1")
        assert body["record"]["verdict"] == "blocked"
        assert base64.b64decode(body["statement_b64"]) == b"statement-1"
        assert body["kid"] == KID
        assert body["log"] == {"log_id": LOG_ID, "seq": 1, "tree_size_now": len(RECORDS)}

    def test_tenant_isolation(self, harness):
        assert get(harness, "/v1/admin/evidence/records", tenant=OTHER)["total"] == 0
        get(harness, "/v1/admin/evidence/records/rec-1", tenant=OTHER, expect=404)
        get(harness, "/v1/admin/evidence/records/rec-1/proof", tenant=OTHER, expect=404)
        assert get(harness, "/v1/admin/evidence/summary", tenant=OTHER)["records_total"] == 0
        assert get(harness, "/v1/admin/evidence/summary", tenant=OTHER)["log"] is None
        assert get(harness, "/v1/admin/evidence/logs", tenant=OTHER)["total"] == 0
        assert get(harness, "/v1/admin/evidence/disclosures", tenant=OTHER)["total"] == 0
        get(harness, f"/v1/admin/evidence/logs/{LOG_ID}/tree-heads", tenant=OTHER, expect=404)
        get(harness, "/v1/admin/evidence/tree-heads/head-1", tenant=OTHER, expect=404)


class TestProof:
    @pytest.mark.parametrize("seq", range(len(RECORDS)))
    def test_every_record_proves_against_the_current_tree(self, harness, seq):
        proof = get(harness, f"/v1/admin/evidence/records/rec-{seq}/proof")
        assert proof["leaf_index"] == seq
        assert proof["tree_size"] == len(RECORDS)
        assert proof["leaf_hash"] == merkle.leaf_hash(f"statement-{seq}".encode())
        assert merkle.verify_inclusion(
            proof["leaf_hash"], proof["leaf_index"], proof["tree_size"], proof["path"], proof["root"]
        )
        # The root the proof folds to is the one the log advertises.
        assert proof["root"] == get(harness, "/v1/admin/evidence/logs")["items"][0]["root"]

    def test_proof_against_an_earlier_head(self, harness):
        proof = get(harness, "/v1/admin/evidence/records/rec-2/proof", tree_size=4)
        head = get(harness, "/v1/admin/evidence/tree-heads/head-1")
        assert proof["tree_size"] == 4 and proof["root"] == head["root"]
        assert merkle.verify_inclusion(proof["leaf_hash"], 2, 4, proof["path"], proof["root"])

    def test_a_record_outside_the_requested_size_is_422(self, harness):
        get(harness, "/v1/admin/evidence/records/rec-5/proof", tree_size=4, expect=422)
        get(harness, "/v1/admin/evidence/records/rec-0/proof", tree_size=99, expect=422)


class TestSummaryAndLogs:
    def test_summary_counts(self, harness):
        s = get(harness, "/v1/admin/evidence/summary")
        assert s["records_total"] == len(RECORDS)
        assert s["by_verdict"] == {"executed": 3, "blocked": 1, "denied": 1, "awaiting_human": 1, "epoch_boundary": 1}
        assert s["by_kind"] == {"decide": 6, "fyi": 1}
        assert s["by_surface"] == {"mcp": 4, "a2a": 1, "contract": 1, "skill": 1}
        assert s["human_disposed_count"] == 1
        assert s["systems_covered"] == 2
        assert s["log"]["log_id"] == LOG_ID
        assert s["log"]["tree_size"] == len(RECORDS)
        assert s["log"]["last_tree_head"]["id"] == "head-2"
        assert s["log"]["last_tree_head"]["grade"] == "standalone"
        assert s["log"]["unwitnessed_heads"] == 1

    def test_summary_scoped_to_a_system(self, harness):
        s = get(harness, "/v1/admin/evidence/summary", ai_system_id="sys-b")
        assert s["records_total"] == 2 and s["systems_covered"] == 1

    def test_logs(self, harness):
        body = get(harness, "/v1/admin/evidence/logs")
        assert body["total"] == 1
        log = body["items"][0]
        assert log["log_id"] == LOG_ID and log["tenant_id"] == TENANT and log["kid"] == KID
        assert log["heads_count"] == 2 and log["receipts_count"] == 1
        assert log["latest_grade"] == "standalone"
        assert log["last_tree_head"]["tree_size"] == len(RECORDS)


    def test_tree_heads_with_receipts(self, harness):
        body = get(harness, f"/v1/admin/evidence/logs/{LOG_ID}/tree-heads")
        assert body["total"] == 2
        assert [h["id"] for h in body["items"]] == ["head-2", "head-1"]
        head1 = body["items"][1]
        assert base64.b64decode(head1["statement_b64"]) == b"head-statement"
        assert head1["transparent_statement_b64"] is None
        assert len(head1["receipts"]) == 1
        rcpt = head1["receipts"][0]
        assert rcpt["verdict"] == "witnessed" and rcpt["witness_id"] == "wit-platform"
        assert rcpt["leaf_index"] == 41 and rcpt["tree_size"] == 42 and rcpt["witness_iat"] == 1_800_000_000
        assert "receipt_b64" not in rcpt
        page = get(harness, f"/v1/admin/evidence/logs/{LOG_ID}/tree-heads", limit=1, offset=1)
        assert [h["id"] for h in page["items"]] == ["head-1"]

    def test_one_tree_head(self, harness):
        head = get(harness, "/v1/admin/evidence/tree-heads/head-2")
        assert head["prev_size"] == 4 and head["receipts"] == []


class TestWitnesses:
    def test_list_shows_platform_plus_own(self, harness):
        body = get(harness, "/v1/admin/evidence/witnesses")
        assert {w["id"] for w in body["items"]} == {"wit-platform"}
        other = get(harness, "/v1/admin/evidence/witnesses", tenant=OTHER)
        assert {w["id"] for w in other["items"]} == {"wit-platform", "wit-t2"}

    def test_tenant_admin_crud_on_its_own_scope(self, harness):
        r = harness.client.post(
            "/v1/admin/evidence/witnesses",
            json={"kind": "scitt", "url": "https://audit.t1.example/entries", "operator_label": "T1 Auditor"},
            headers=harness.headers(),
        )
        assert r.status_code == 201, r.text
        created = r.json()
        assert created["scope"] == "tenant" and created["tenant_id"] == TENANT
        assert created["enabled"] is True
        wid = created["id"]

        r = harness.client.patch(
            f"/v1/admin/evidence/witnesses/{wid}",
            json={"enabled": False, "pinned_key": "pk-9", "operator_label": "T1 Auditor (paused)"},
            headers=harness.headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False and r.json()["pinned_key"] == "pk-9"

        # The other tenant cannot see or touch it.
        assert wid not in {w["id"] for w in get(harness, "/v1/admin/evidence/witnesses", tenant=OTHER)["items"]}
        r = harness.client.delete(f"/v1/admin/evidence/witnesses/{wid}", headers=harness.headers(OTHER))
        assert r.status_code == 404

        r = harness.client.delete(f"/v1/admin/evidence/witnesses/{wid}", headers=harness.headers())
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert wid not in {w["id"] for w in get(harness, "/v1/admin/evidence/witnesses")["items"]}

    def test_tenant_admin_cannot_create_or_edit_platform_scope(self, harness):
        r = harness.client.post(
            "/v1/admin/evidence/witnesses",
            json={"scope": "platform", "kind": "scitt", "url": "https://x.example", "operator_label": "x"},
            headers=harness.headers(),
        )
        assert r.status_code == 403
        r = harness.client.patch(
            "/v1/admin/evidence/witnesses/wit-platform", json={"enabled": False}, headers=harness.headers()
        )
        assert r.status_code == 403
        r = harness.client.delete("/v1/admin/evidence/witnesses/wit-platform", headers=harness.headers())
        assert r.status_code == 403


    def test_validation(self, harness):
        bad = [
            {"kind": "carrier-pigeon", "url": "https://x.example", "operator_label": "x"},
            {"kind": "scitt", "url": "ftp://x.example", "operator_label": "x"},
            {"scope": "galaxy", "kind": "scitt", "url": "https://x.example", "operator_label": "x"},
        ]
        for body in bad:
            r = harness.client.post("/v1/admin/evidence/witnesses", json=body, headers=harness.headers())
            assert r.status_code == 422, body


class TestDisclosures:
    def test_list(self, harness):
        body = get(harness, "/v1/admin/evidence/disclosures")
        assert body["total"] == 1
        d = body["items"][0]
        assert d["audience"] == "external auditor" and d["completeness"] == "contiguous"
        assert d["disclosed_count"] == 2 and d["suppressed_fields"] == ["attestation.digest_salt"]
        assert d["statement_b64"] is None


class TestVerify:
    def test_unreachable_verifier_is_a_finding_not_a_pass(self, harness, monkeypatch):
        # A closed port, a short timeout: the client must come back with the
        # `verifier_unavailable` finding rather than raise or hang.
        monkeypatch.setenv("BRUTOR_GATEWAY_INTERNAL_URL", "http://127.0.0.1:9")
        r = harness.client.post(
            "/v1/admin/evidence/verify", json={"record_id": "rec-0"}, headers=harness.headers()
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["record_id"] is None
        assert [f["code"] for f in body["findings"]] == ["verifier_unavailable"]
        assert body["findings"][0]["severity"] == "error"

    def test_passes_the_core_response_through(self, harness, monkeypatch):
        seen = {}

        def fake(record, statement_b64, **_):
            seen["record"], seen["statement_b64"] = record, statement_b64
            return {"ok": True, "findings": [], "record_id": "rec-0", "effect_mode": "confirmed",
                    "statement": {"verified": True, "iss": "brutor", "sub": "rec-0", "kid": KID}}

        monkeypatch.setattr(core_client, "verify_record", fake)
        r = harness.client.post(
            "/v1/admin/evidence/verify", json={"record_id": "rec-0"}, headers=harness.headers()
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        assert seen["record"]["action_id"] == "mcp/plog-0"
        assert base64.b64decode(seen["statement_b64"]) == b"statement-0"

        inline = {"record": {"schema": "ai.brutor/action-record/v1"}, "statement_b64": "AAE="}
        r = harness.client.post("/v1/admin/evidence/verify", json=inline, headers=harness.headers())
        assert r.status_code == 200
        assert seen["record"] == inline["record"] and seen["statement_b64"] == "AAE="

    def test_neither_id_nor_record_is_422(self, harness):
        r = harness.client.post("/v1/admin/evidence/verify", json={}, headers=harness.headers())
        assert r.status_code == 422

    def test_other_tenant_cannot_verify_by_id(self, harness):
        r = harness.client.post(
            "/v1/admin/evidence/verify", json={"record_id": "rec-0"}, headers=harness.headers(OTHER)
        )
        assert r.status_code == 404

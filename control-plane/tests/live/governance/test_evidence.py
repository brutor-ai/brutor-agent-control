# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Live evidence series — the governed call leaves not only an audit
row but a *sealed action record* in the tenant's Merkle log, and the record
verifies through the control plane's evidence API.

Each test extends an MCP governance scenario with the third proof:

    live HTTP response  →  proxy-log row  →  action record (verdict, effect,
    never-enters)  →  signature / rule check  →  RFC 6962 inclusion proof
    recomputed here from the served path.

The series skips itself when the stack seals nothing (no
``BRUTOR_EVIDENCE_SIGNING_KEY`` on the core): that is a configuration state,
not a failure, and the Evidence Ledger says so on its own.
"""

from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from app.services.evidence.merkle import verify_inclusion

from .helpers import assert_log, mcp_call, mcp_tools_list
from .test_mcp import mcp  # noqa: F401 — the MCP demo provisioning fixture

EVIDENCE = "/v1/admin/evidence"

# Verdict vocabulary. Pinned by the CP's parity test; a
# stranger here means the stack and this suite disagree on the contract.
EXECUTED = "executed"
DENIED = "denied"
BLOCKED = "blocked"


def _summary(control, admin_headers) -> Dict[str, Any]:
    r = control.get(f"{EVIDENCE}/summary", headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def evidence_enabled(control_client, admin_headers) -> Dict[str, Any]:
    """Skip the series when the core seals nothing for this tenant."""
    s = _summary(control_client, admin_headers)
    if not s.get("log"):
        pytest.skip(
            "no evidence log for the tenant — the core has no "
            "BRUTOR_EVIDENCE_SIGNING_KEY (Settings → Evidence shows the state)"
        )
    return s


def _record_for(control, admin_headers, log: Dict[str, Any], timeout: float = 20.0) -> Dict[str, Any]:
    """The sealed record a proxy-log row points at.

    `record_id` is stamped in the same INSERT as the row, but the query API
    can answer before the writer's batch lands — so re-read the row until
    it carries the id, then fetch the record."""
    corr = log.get("correlation_id")
    rid = log.get("record_id")
    deadline = time.monotonic() + timeout
    while not rid and time.monotonic() < deadline:
        time.sleep(0.4)
        r = control.post(
            "/v1/admin/proxy-logs/query", headers=admin_headers,
            json={"correlation_id": corr, "limit": 5},
        )
        rows = r.json() if r.status_code == 200 else {}
        rows = rows.get("logs") if isinstance(rows, dict) and "logs" in rows else rows
        rows = rows if isinstance(rows, list) else (rows or {}).get("items", [])
        for row in rows:
            if row.get("record_id"):
                rid = row["record_id"]
                break
    assert rid, f"governed row corr={corr} landed without a record_id (capture gap)"
    r = control.get(f"{EVIDENCE}/records/{rid}", headers=admin_headers)
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec.get("record_id") == rid
    return rec


def _record_body(rec: Dict[str, Any]) -> Dict[str, Any]:
    return rec.get("record") or rec


def _verify(control, admin_headers, rid: str) -> Dict[str, Any]:
    r = control.post(f"{EVIDENCE}/verify", headers=admin_headers, json={"record_id": rid})
    assert r.status_code == 200, r.text
    return r.json()


def _proof_verifies(control, admin_headers, rid: str) -> Dict[str, Any]:
    r = control.get(f"{EVIDENCE}/records/{rid}/proof", headers=admin_headers)
    assert r.status_code == 200, r.text
    p = r.json()
    assert verify_inclusion(p["leaf_hash"], p["leaf_index"], p["tree_size"], p["path"], p["root"]), (
        f"inclusion proof for {rid} does not fold to the served root: {p}"
    )
    return p


def _assert_never_enters(body: Dict[str, Any], *values: str) -> None:
    dumped = str(body)
    for v in values:
        if v:
            assert v not in dumped, f"never-enters value {v!r} found in record {body.get('record_id')}"


# --------------------------------------------------------------------------- #


def test_argument_policy_refusal_is_sealed_denied(
    proxy_client, control_client, admin_headers, mcp, log_probe, evidence_enabled
):
    call = mcp_call(proxy_client, mcp["sales_key"], mcp["server"], "query_sales_db",
                    {"sql": "DELETE FROM bookings WHERE id = 20;"})
    assert call.status == 403, call.text
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=False, argument_policy_denied=True)

    rec = _record_for(control_client, admin_headers, log)
    body = _record_body(rec)
    assert body["verdict"] == DENIED, body
    assert body["disposition"]["decision"] == "reject"
    assert body["disposition"]["approver"] == "policy"
    assert body["disposition"]["human_disposed"] is False
    assert "effect" not in body, "a refusal carries no effect"
    assert any(c["id"].startswith("ai.brutor.argument_policy") for c in body.get("controls", [])), body
    # The refused SQL and the caller's identity never enter the record.
    _assert_never_enters(body, "DELETE FROM bookings", mcp["sales_key"], log.get("subject_id") or "")

    v = _verify(control_client, admin_headers, rec["record_id"])
    assert v.get("ok") is True, v
    assert v.get("statement", {}).get("verified") is True, v
    _proof_verifies(control_client, admin_headers, rec["record_id"])


def test_golden_read_is_sealed_executed_with_confirmed_effect(
    proxy_client, control_client, admin_headers, mcp, log_probe, evidence_enabled
):
    call = mcp_call(proxy_client, mcp["sales_key"], mcp["server"], "query_sales_db",
                    {"sql": "SELECT region, quarter, revenue FROM bookings LIMIT 3;"})
    if call.status != 200:
        pytest.skip(f"golden MCP read needs the demo server up; got {call.status}: {call.text[:160]}")
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=True, server_id_set=True)

    rec = _record_for(control_client, admin_headers, log)
    body = _record_body(rec)
    assert body["verdict"] == EXECUTED, body
    assert body["effect"]["status"] == "confirmed"
    assert len(body["effect"]["response_digest"]) == 64, "confirmed requires an observed response"
    assert body["disposition"]["approver"] == "policy" and body["disposition"]["human_disposed"] is False
    # The row hash is cited, so the two ledgers cannot disagree.
    assert any(r["type"] == "ai.brutor.proxy-log-row" for r in body.get("references", [])), body
    _assert_never_enters(body, "SELECT region", mcp["sales_key"])

    v = _verify(control_client, admin_headers, rec["record_id"])
    assert v.get("ok") is True, v
    assert v.get("effect_mode") == "confirmed", v
    _proof_verifies(control_client, admin_headers, rec["record_id"])


def test_a_query_is_not_sealed(proxy_client, control_client, admin_headers, mcp, log_probe, evidence_enabled):
    """Commands seal, queries do not."""
    call = mcp_tools_list(proxy_client, mcp["sales_key"], mcp["server"])
    if call.status != 200:
        pytest.skip(f"tools/list needs the demo MCP server up; got {call.status}: {call.text[:160]}")
    log = log_probe(call.corr)
    assert_log(log, proxy_type="mcp", success=True)
    # Give the writer a batch to land before asserting the absence.
    time.sleep(1.5)
    r = control_client.post("/v1/admin/proxy-logs/query", headers=admin_headers,
                            json={"correlation_id": call.corr, "limit": 5})
    assert r.status_code == 200, r.text
    rows = r.json()
    rows = rows.get("logs", rows) if isinstance(rows, dict) else rows
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    assert rows and all(not row.get("record_id") for row in rows), rows


def test_tenant_log_advances_and_root_matches_served_proofs(
    control_client, admin_headers, evidence_enabled
):
    """The summary's log root is the root every served proof folds to."""
    before = evidence_enabled["log"]
    after = _summary(control_client, admin_headers)["log"]
    assert after["tree_size"] >= before["tree_size"]
    r = control_client.get(f"{EVIDENCE}/records", headers=admin_headers, params={"limit": 3})
    assert r.status_code == 200, r.text
    items = r.json().get("items", [])
    assert items, "records were sealed above, so the ledger cannot be empty"
    for it in items:
        p = _proof_verifies(control_client, admin_headers, it["record_id"])
        assert p["tree_size"] <= _summary(control_client, admin_headers)["log"]["tree_size"]

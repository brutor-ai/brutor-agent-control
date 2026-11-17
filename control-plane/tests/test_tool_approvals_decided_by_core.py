# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The gateway core decides tool approvals; the control plane never writes one

`POST /v1/admin/approvals/{id}/approve|reject` used to update the
request itself and write only the admin audit log. The decision row the core writes on the run's chain is what mints the
`resolved` evidence record with a human disposition, what the run ledger
reads and what the compliance resolvers count — so a decision made here had
an `awaiting_human` record no human verdict ever answered, and a lapsed
request "expired" here was never sealed.

The contract pinned here: the control plane authorises the administrator,
asks the core (`core_client.decide_approval`), maps the core's outcome to its
HTTP answer, writes the admin audit log for a decision that was made — and
does not touch the row. No answer from the core is no decision (503).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin import tool_approvals as endpoint
from app.constants import approval_decision as decisions
from app.constants import approvals as approval_status
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.tool_approval_request import ToolApprovalRequest
from app.services.evidence import core_client
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
RESOLVED_AT = "2026-11-10T12:00:00+00:00"


@pytest.fixture()
def harness():
    h = build_admin_harness(
        [endpoint.create_tool_approvals_admin_router(BrutorLogger("approvals-core"))],
        logger_name="approvals-core",
    )

    def seed(s):
        now = datetime.now(timezone.utc)
        for rid, tenant in (("a-1", TENANT), ("a-other", "t2")):
            s.add(ToolApprovalRequest(
                id=rid, tenant_id=tenant, server_config_id=None, server_name="apps",
                capability_type="tool", capability_name="send_payment",
                requester_type="agent", requester_group_id="g1", requester_display="agent-1",
                status=approval_status.PENDING, token_used="N", timeout_seconds=60,
                expires_at=now + timedelta(hours=1), created_at=now - timedelta(minutes=10),
            ))
    h.seed(seed)
    yield h
    h.close()


@pytest.fixture()
def core(monkeypatch):
    """Record what the endpoint asks of the core; answer as configured."""
    calls = []
    answer = {}

    def fake(tenant_id, approval_request_id, decision, **kw):
        calls.append({"tenant_id": tenant_id, "id": approval_request_id,
                      "decision": decision, **kw})
        return answer["value"]

    monkeypatch.setattr(core_client, "decide_approval", fake)
    return calls, answer


def _decided(outcome, **extra):
    return {"outcome": outcome, "status": outcome, "resolved_by": "admin:1",
            "resolved_at": RESOLVED_AT, **extra}


def _stored(harness, rid="a-1"):
    with harness.Factory() as s:
        r = s.get(ToolApprovalRequest, rid)
        return r.status, r.approval_token, r.resolved_by


def _audit_actions(harness):
    with harness.Factory() as s:
        return [a.action for a in s.query(TenantAdminAuditLog).all()
                if a.action.startswith("tool_approval.")]


UNTOUCHED = (approval_status.PENDING, None, None)


@pytest.mark.parametrize("decision,outcome", [("approve", "approved"), ("reject", "rejected")])
def test_a_decision_is_made_by_the_core_and_audited_here(harness, core, decision, outcome):
    calls, answer = core
    extra = {"approval_token": "tok-1"} if decision == "approve" else {}
    answer["value"] = _decided(outcome, **extra)

    r = harness.client.post(f"/v1/admin/approvals/a-1/{decision}",
                            json={"note": "checked"}, headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["status"], body["resolved_by"], body["resolved_at"]) == (
        "a-1", outcome, "admin:1", RESOLVED_AT)
    assert body.get("approval_token") == extra.get("approval_token")

    assert len(calls) == 1
    call = calls[0]
    assert (call["tenant_id"], call["id"], call["decision"], call["note"]) == (
        TENANT, "a-1", decision, "checked")
    assert call["approver_id"] and call["approver_username"]
    # The row is the core's to write (the fake core wrote nothing).
    assert _stored(harness) == UNTOUCHED
    assert _audit_actions(harness) == [f"tool_approval.{decision}"]


@pytest.mark.parametrize("outcome,extra,expected", [
    (decisions.EXPIRED, {}, 410),
    (decisions.ALREADY_RESOLVED, {"status": "rejected"}, 409),
    (decisions.NO_LONGER_PENDING, {}, 409),
    (decisions.NOT_FOUND, {}, 404),
])
def test_what_the_core_refuses_is_refused_and_not_audited_as_a_decision(
        harness, core, outcome, extra, expected):
    calls, answer = core
    answer["value"] = {"outcome": outcome, **extra}
    r = harness.client.post("/v1/admin/approvals/a-1/approve",
                            json={}, headers=harness.headers(TENANT))
    assert r.status_code == expected, r.text
    assert len(calls) == 1
    assert _stored(harness) == UNTOUCHED
    assert _audit_actions(harness) == []


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_no_answer_from_the_core_is_no_decision(harness, core, decision):
    calls, answer = core
    answer["value"] = {"error": core_client.ERROR_CORE_UNREACHABLE, "detail": "refused"}
    r = harness.client.post(f"/v1/admin/approvals/a-1/{decision}",
                            json={}, headers=harness.headers(TENANT))
    assert r.status_code == 503, r.text
    assert len(calls) == 1
    assert _stored(harness) == UNTOUCHED
    assert _audit_actions(harness) == []


def test_another_tenants_request_is_not_found_and_the_core_is_not_asked(harness, core):
    calls, _ = core
    r = harness.client.post("/v1/admin/approvals/a-other/approve",
                            json={}, headers=harness.headers(TENANT))
    assert r.status_code == 404
    assert calls == []
    assert _stored(harness, "a-other") == UNTOUCHED


# --- the client ---------------------------------------------------------------


def _client_with(monkeypatch, response):
    seen = []

    def fake_post(path, body, timeout):
        seen.append((path, dict(body)))
        return response

    monkeypatch.setattr(core_client, "_post", fake_post)
    return seen


def test_the_client_sends_the_decision_and_who_made_it(monkeypatch):
    seen = _client_with(monkeypatch, _decided("approved", approval_token="t"))
    out = core_client.decide_approval("t1", "apr-1", "approve", approver_id="17",
                                      approver_username="olivia", note="ok")
    assert out["outcome"] == "approved"
    assert seen == [("/v1/internal/approvals/decide", {
        "tenant_id": "t1", "approval_request_id": "apr-1", "decision": "approve",
        "note": "ok", "approver": {"id": "17", "username": "olivia"},
    })]


@pytest.mark.parametrize("response", [
    {"ok": True},                                              # no outcome
    {"outcome": "escalated"},                                  # an outcome nobody defined
    _decided("rejected"),                                      # asked to approve, got the other
    {"outcome": "approved", "status": "approved"},             # decided, members missing
    {"outcome": "approved", "status": "rejected",
     "resolved_by": "admin:1", "resolved_at": RESOLVED_AT},    # outcome and status disagree
])
def test_an_answer_that_is_not_what_was_asked_for_is_an_error(monkeypatch, response):
    _client_with(monkeypatch, response)
    out = core_client.decide_approval("t1", "apr-1", "approve", approver_id="17",
                                      approver_username="olivia")
    assert core_client.is_error(out), out


def test_the_client_refuses_a_decision_that_is_not_one_without_calling(monkeypatch):
    seen = _client_with(monkeypatch, _decided("approved"))
    out = core_client.decide_approval("t1", "apr-1", "maybe", approver_id="17",
                                      approver_username="olivia")
    assert core_client.is_error(out) and seen == []


def test_a_transport_failure_is_an_error(monkeypatch):
    _client_with(monkeypatch, {"error": core_client.ERROR_CORE_UNREACHABLE, "detail": "x"})
    out = core_client.decide_approval("t1", "apr-1", "reject", approver_id="17",
                                      approver_username="olivia")
    assert out["error"] == core_client.ERROR_CORE_UNREACHABLE


# --- the class of bug ----------------------------------------------------------


def test_the_endpoint_module_never_assigns_an_approval_status():
    """The class of that bug: nothing in the admin endpoint writes `status`,
    `approval_token` or `resolved_*` on a request. A write here is a decision
    (or an expiry) the core did not make and no evidence records."""
    tree = ast.parse(inspect.getsource(endpoint))
    written = sorted({
        t.attr
        for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
        for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(t, ast.Attribute)
    })
    forbidden = {"status", "approval_token", "resolved_by", "resolved_at", "resolution_note",
                 "token_used"}
    assert not (forbidden & set(written)), f"the endpoint writes {forbidden & set(written)}"
    assert "db.commit()" not in inspect.getsource(endpoint)

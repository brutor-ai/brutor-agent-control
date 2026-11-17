# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Tool approval statuses mean the same thing in both planes

The core writes `consumed` when an agent redeems its approval token, and a
pending request past its deadline stays `pending` until the core's sweeper
reaches it. The admin list filtered by exact status, so every redeemed approval
vanished from "approved" and lapsed requests were counted as actionable
(the Portal had the same bug). This pins the shared meaning:
approved ⊇ consumed; expired ⊇ lapsed pending; pending excludes lapsed; unknown
filters are rejected, never silently empty.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.tool_approvals import create_tool_approvals_admin_router
from app.constants import approvals as approval_status
from app.core.logging_system import BrutorLogger
from app.database.models.tool_approval_request import ToolApprovalRequest
from app.services.evidence import core_client
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"


def _now() -> datetime:
    """The wall clock, read when used — never at import (tests/test_no_import_time_clock.py)."""
    return datetime.now(timezone.utc)


# id: (status, expires_at offset from the moment the harness is seeded)
ROWS = {
    "a-pending": (approval_status.PENDING, timedelta(hours=2)),
    "a-lapsed": (approval_status.PENDING, -timedelta(minutes=5)),
    "a-approved": (approval_status.APPROVED, timedelta(hours=2)),
    "a-consumed": (approval_status.CONSUMED, timedelta(hours=2)),
    "a-rejected": (approval_status.REJECTED, timedelta(hours=2)),
    "a-expired": (approval_status.EXPIRED, -timedelta(hours=1)),
}


@pytest.fixture(scope="module")
def harness():
    h = build_admin_harness([create_tool_approvals_admin_router(BrutorLogger("approvals-vocab"))],
                            logger_name="approvals-vocab")

    def seed(s):
        now = _now()
        for i, (rid, (status, expires_in)) in enumerate(ROWS.items()):
            expires_at = now + expires_in
            s.add(ToolApprovalRequest(
                id=rid, tenant_id=TENANT, server_config_id=None, server_name="apps",
                capability_type="tool", capability_name="applications_set_recommendation",
                requester_type="agent", requester_group_id="g1", requester_display="agent-1",
                status=status, token_used="N", timeout_seconds=86400, expires_at=expires_at,
                created_at=now - timedelta(minutes=10 + i),
            ))
    h.seed(seed)
    yield h
    h.close()


def ids(harness, status_filter):
    r = harness.client.get(f"/v1/admin/approvals?status_filter={status_filter}",
                           headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


def test_approved_includes_redeemed_tokens(harness):
    got = ids(harness, "approved")
    assert set(got) == {"a-approved", "a-consumed"}
    assert got["a-consumed"]["decision"] == "approved"
    assert got["a-consumed"]["token_used"] is True
    assert got["a-approved"]["token_used"] is False


def test_consumed_is_filterable_on_its_own(harness):
    assert set(ids(harness, "consumed")) == {"a-consumed"}


def test_pending_excludes_a_request_past_its_deadline(harness):
    assert set(ids(harness, "pending")) == {"a-pending"}


def test_expired_includes_a_lapsed_pending_request(harness):
    got = ids(harness, "expired")
    assert set(got) == {"a-expired", "a-lapsed"}
    assert got["a-lapsed"]["decision"] == "expired"
    assert got["a-lapsed"]["status"] == "pending", "the stored value is reported truthfully"


def test_all_returns_every_row(harness):
    assert set(ids(harness, "all")) == set(ROWS)


def test_unknown_filter_is_rejected_not_silently_empty(harness):
    r = harness.client.get("/v1/admin/approvals?status_filter=bogus", headers=harness.headers(TENANT))
    assert r.status_code == 422, r.text


def test_pending_count_is_actionable_only(harness):
    r = harness.client.get("/v1/admin/approvals/pending-count", headers=harness.headers(TENANT))
    assert r.status_code == 200, r.text
    assert r.json()["pending_count"] == 1


def test_every_status_has_a_decision():
    for s in approval_status.ALL_STATUSES:
        assert approval_status.decision(s, _now() + timedelta(hours=1)) != approval_status.UNKNOWN_DECISION
    assert approval_status.decision("from-the-future", None) == approval_status.UNKNOWN_DECISION


# --- approve / reject routes answer 200 and record the decision -------------
#
# Regression: both handlers called the audit logger without the
# DB session after committing, so every decision was saved and then answered
# HTTP 500. The class-level guard is tests/test_admin_audit_log_calls.py; these
# pin the routes end to end.

@pytest.fixture()
def decide_harness():
    h = build_admin_harness([create_tool_approvals_admin_router(BrutorLogger("approvals-decide"))],
                            logger_name="approvals-decide")

    def seed(s):
        now = _now()
        for rid in ("d-approve", "d-reject"):
            s.add(ToolApprovalRequest(
                id=rid, tenant_id=TENANT, server_config_id=None, server_name="apps",
                capability_type="tool", capability_name="applications_set_recommendation",
                requester_type="agent", requester_group_id="g1", requester_display="agent-1",
                status=approval_status.PENDING, token_used="N", timeout_seconds=86400,
                expires_at=now + timedelta(hours=2), created_at=now - timedelta(minutes=1),
            ))
    h.seed(seed)
    yield h
    h.close()


@pytest.mark.parametrize("verb,expected", [("approve", "approved"), ("reject", "rejected")])
def test_a_decision_answers_200_and_is_recorded(decide_harness, verb, expected, monkeypatch):
    rid = f"d-{verb}"

    # The gateway core makes the decision (the contract is
    # tests/test_tool_approvals_decided_by_core.py). This stands in for it:
    # it writes the row as the core does, so the route is still pinned end to
    # end — 200, the audit logger called with its session, the decision
    # visible in the list.
    def core_decides(tenant_id, approval_request_id, decision, **_kw):
        with decide_harness.Factory() as s:
            row = s.get(ToolApprovalRequest, approval_request_id)
            row.status = expected
            s.commit()
        return {"outcome": expected, "status": expected, "resolved_by": "admin:1",
                "resolved_at": _now().isoformat()}

    monkeypatch.setattr(core_client, "decide_approval", core_decides)
    r = decide_harness.client.post(f"/v1/admin/approvals/{rid}/{verb}",
                                   json={"note": "decided in test"},
                                   headers=decide_harness.headers(TENANT))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == expected
    got = decide_harness.client.get(f"/v1/admin/approvals?status_filter={expected}",
                                    headers=decide_harness.headers(TENANT))
    assert rid in {i["id"] for i in got.json()["items"]}

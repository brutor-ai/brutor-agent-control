# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Holds raised by one call can be shown together.

A call that is held twice (two tools of one agent run, a tool and the
delegation that led to it) leaves two approval requests with the same
`originating_trace_root_id`. The list and the detail say which call a hold
belongs to, and the list can be asked for the holds of one call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin import tool_approvals as endpoint
from app.constants import approvals as approval_status
from app.core.logging_system import BrutorLogger
from app.database.models.tool_approval_request import ToolApprovalRequest
from tests.admin_endpoint_harness import build_admin_harness

BASE = "/v1/admin/approvals"

# id -> (tenant, trace root, kind, status)
HOLDS = {
    "hold-a1": ("t1", "root-a", "agent_run", approval_status.PENDING),
    "hold-a2": ("t1", "root-a", "agent_run", approval_status.PENDING),
    "hold-a3": ("t1", "root-a", "agent_run", approval_status.REJECTED),
    "hold-b1": ("t1", "root-b", "llm_request", approval_status.PENDING),
    "hold-none": ("t1", None, None, approval_status.PENDING),
    "hold-other-tenant": ("t2", "root-a", "agent_run", approval_status.PENDING),
}


@pytest.fixture()
def h():
    harness = build_admin_harness(
        [endpoint.create_tool_approvals_admin_router(BrutorLogger("approvals-trace"))],
        logger_name="approvals-trace",
    )

    def seed(s):
        now = datetime.now(timezone.utc)
        for hold_id, (tenant, root, kind, status) in HOLDS.items():
            s.add(
                ToolApprovalRequest(
                    id=hold_id,
                    tenant_id=tenant,
                    server_name="apps",
                    capability_type="tool",
                    capability_name="send_payment",
                    requester_type="agent",
                    requester_group_id="g1",
                    status=status,
                    token_used="N",
                    timeout_seconds=300,
                    expires_at=now + timedelta(minutes=5),
                    created_at=now,
                    originating_trace_root_id=root,
                    originating_trace_root_kind=kind,
                    originating_parent_task_id="task-1" if root else None,
                )
            )

    harness.seed(seed)
    yield harness
    harness.close()


def _list(h, tenant="t1", **params):
    r = h.client.get(BASE, params=params, headers=h.headers(tenant))
    assert r.status_code == 200, r.text
    return r.json()


def test_the_list_says_which_call_each_hold_belongs_to(h):
    items = {i["id"]: i for i in _list(h)["items"]}
    assert set(items) == {"hold-a1", "hold-a2", "hold-b1", "hold-none"}
    for hold_id, item in items.items():
        _, root, kind, _ = HOLDS[hold_id]
        assert item["originating_trace_root_id"] == root
        assert item["originating_trace_root_kind"] == kind


def test_the_detail_says_which_call_the_hold_belongs_to(h):
    r = h.client.get(f"{BASE}/hold-a1", headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    approval = r.json()["approval"]
    assert approval["originating_trace_root_id"] == "root-a"
    assert approval["originating_trace_root_kind"] == "agent_run"
    approval = h.client.get(f"{BASE}/hold-none", headers=h.headers("t1")).json()["approval"]
    assert approval["originating_trace_root_id"] is None and approval["originating_trace_root_kind"] is None


def test_the_list_can_be_asked_for_the_holds_of_one_call(h):
    body = _list(h, trace_root_id="root-a")
    assert {i["id"] for i in body["items"]} == {"hold-a1", "hold-a2"}
    assert body["total"] == 2
    # With the other filters, not instead of them.
    body = _list(h, trace_root_id="root-a", status_filter="all")
    assert {i["id"] for i in body["items"]} == {"hold-a1", "hold-a2", "hold-a3"}
    assert {i["id"] for i in _list(h, trace_root_id="root-b")["items"]} == {"hold-b1"}
    assert _list(h, trace_root_id="root-nobody")["items"] == []


def test_another_tenants_holds_of_the_same_call_are_not_listed(h):
    assert {i["id"] for i in _list(h, "t2", trace_root_id="root-a")["items"]} == {"hold-other-tenant"}
    assert "hold-other-tenant" not in {i["id"] for i in _list(h, trace_root_id="root-a", status_filter="all")["items"]}


@pytest.mark.parametrize("value", ["", "x" * 65])
def test_a_trace_root_no_hold_can_have_is_refused(h, value):
    r = h.client.get(BASE, params={"trace_root_id": value}, headers=h.headers("t1"))
    assert r.status_code == 422, r.text

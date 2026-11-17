# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Admin Console's approvals screen shows every hold the gateway core
raises.

`tool_approval_requests.server_config_id` is nullable (migration a102_approval_generic_surface):
the core fills it only for an MCP hold whose server it can resolve, and
leaves it NULL for an A2A call, a skill execution, and an MCP hold whose
server has no enabled configuration (`brutor-gateway-core`
`src/services/agent_approval.rs`: "Resolve server_config_id only for MCP").
`ToolApprovalDetail.server_config_id` was a required string, so
`GET /v1/admin/approvals/{id}` answered 500 for exactly the holds an
agent raises on those surfaces. In the Community Edition this screen is
where a human decides them.

The rows below are written the way the core writes them: every column it
leaves NULL is NULL here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin import tool_approvals as endpoint
from app.api.v1.models.admin.tool_approval import ToolApprovalDetail, ToolApprovalSummary
from app.constants import approvals as approval_status
from app.core.logging_system import BrutorLogger
from app.database.models.tool_approval_request import ToolApprovalRequest
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"
BASE = "/v1/admin/approvals"

# surface -> (scope_ref stored in server_name, capability_type, capability_name)
HOLDS = {
    "a2a": ("card-claims-triage", "a2a_call", "assess_claim"),
    "skill": ("skill-refund", "skill_execution", "issue_refund"),
    "mcp-unresolved": ("apps", "tool", "send_payment"),
}


@pytest.fixture()
def h():
    harness = build_admin_harness(
        [endpoint.create_tool_approvals_admin_router(BrutorLogger("approvals-held"))],
        logger_name="approvals-held",
    )

    def seed(s):
        now = datetime.now(timezone.utc)
        for surface, (scope, capability_type, capability_name) in HOLDS.items():
            s.add(
                ToolApprovalRequest(
                    id=f"hold-{surface}",
                    tenant_id=TENANT,
                    server_config_id=None,
                    server_name=scope,
                    capability_type=capability_type,
                    capability_name=capability_name,
                    requester_type="agent",
                    requester_user_id=None,
                    requester_api_key_id=None,
                    requester_group_id="g1",
                    requester_display=None,
                    call_arguments=None,
                    request_metadata={"surface": surface.split("-")[0], "agent_id": "agent-1"},
                    status=approval_status.PENDING,
                    token_used="N",
                    timeout_seconds=300,
                    expires_at=now + timedelta(minutes=5),
                    created_at=now,
                )
            )

    harness.seed(seed)
    yield harness
    harness.close()


def test_the_table_lets_the_core_leave_these_columns_null():
    """What the model must accept is read from the table, not assumed."""
    columns = ToolApprovalRequest.__table__.columns
    nullable = {name for name in ToolApprovalDetail.model_fields if name in columns and columns[name].nullable}
    assert "server_config_id" in nullable
    for name in sorted(nullable):
        assert not ToolApprovalDetail.model_fields[name].is_required(), (
            f"tool_approval_requests.{name} is nullable, but ToolApprovalDetail.{name} is required"
        )
    for name, field in ToolApprovalSummary.model_fields.items():
        if name in columns and columns[name].nullable:
            assert not field.is_required(), name


def test_the_list_shows_every_hold(h):
    r = h.client.get(BASE, headers=h.headers(TENANT))
    assert r.status_code == 200, r.text
    listed = {a["id"]: a for a in r.json()["items"]}
    assert sorted(listed) == sorted(f"hold-{surface}" for surface in HOLDS)
    assert listed["hold-a2a"]["capability_type"] == "a2a_call"


@pytest.mark.parametrize("surface", sorted(HOLDS))
def test_the_detail_of_a_hold_without_a_server_configuration_is_shown(h, surface):
    r = h.client.get(f"{BASE}/hold-{surface}", headers=h.headers(TENANT))
    assert r.status_code == 200, r.text
    approval = r.json()["approval"]
    scope, capability_type, capability_name = HOLDS[surface]
    assert approval["server_config_id"] is None
    assert (approval["server_name"], approval["capability_type"], approval["capability_name"]) == (
        scope, capability_type, capability_name,
    )
    assert approval["requester_type"] == "agent"
    assert approval["requester_user_id"] is None and approval["call_arguments"] is None
    assert approval["status"] == approval_status.PENDING

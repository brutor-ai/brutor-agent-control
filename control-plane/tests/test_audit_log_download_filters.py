# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The audit-log download holds the rows the list shows.

The list (`POST /v1/admin/users/audit-logs/search`) filters by
administrator, action, resource type, IP address, HTTP method, outcome,
time and free text. The download took three of those and ignored the
rest, so a file exported from a filtered view held rows the view did not
show. It takes the same filters, with the same meaning.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database.models.admin import TenantAdminAuditLog
from tests.admin_endpoint_harness import build_admin_harness

EXPORT = "/v1/admin/users/audit-logs/export"
SEARCH = "/v1/admin/users/audit-logs/search"

ROWS = [
    # action, resource_type, method, ip, success, hours ago, path
    ("probe_create", "mcp_server", "POST", "10.0.0.1", True, 1, "/v1/admin/mcp-servers"),
    ("probe_create", "llm_model", "POST", "10.0.0.2", False, 1, "/v1/admin/llms"),
    ("probe_update", "mcp_server", "PATCH", "10.0.0.1", True, 30, "/v1/admin/mcp-servers/x"),
    ("probe_delete", "mcp_server", "DELETE", "10.0.0.3", True, 2, "/v1/admin/mcp-servers/needle"),
]


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([])
    now = datetime.now(timezone.utc)

    def seed(s):
        for tenant in ("t1", "t2"):
            for action, resource_type, method, ip, success, hours, path in ROWS:
                s.add(TenantAdminAuditLog(
                    id=uuid.uuid4().hex[:32], tenant_id=tenant, action=action, resource_type=resource_type,
                    request_method=method, ip_address=ip, success=success, request_path=path,
                    timestamp=now - timedelta(hours=hours),
                ))

    harness.seed(seed)
    yield harness
    harness.close()


def _exported(h, **params):
    r = h.client.get(EXPORT, params={"format": "jsonl", **params}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    lines = [json.loads(line) for line in r.text.strip().split("\n")]
    return lines[0]["_meta"], [row for row in lines[1:] if str(row.get("action", "")).startswith("probe_")]


def _listed(h, **body):
    r = h.client.post(SEARCH, json={"limit": 1000, **body}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    return [row for row in r.json()["logs"] if row["action"].startswith("probe_")]


FILTERS = [
    {"actions": ["probe_create"]},
    {"actions": ["probe_create", "probe_delete"]},
    {"resource_types": ["llm_model"]},
    {"request_methods": ["PATCH", "DELETE"]},
    {"ip_addresses": ["10.0.0.1"]},
    {"success": False},
    {"hours_ago": 24},
    {"q": "needle"},
    {"actions": ["probe_create"], "success": True, "ip_addresses": ["10.0.0.1"]},
]


@pytest.mark.parametrize("filters", FILTERS, ids=[",".join(f) for f in FILTERS])
def test_the_download_holds_what_the_list_shows(h, filters):
    listed = _listed(h, **filters)
    meta, exported = _exported(h, **filters)
    assert sorted(row["id"] for row in exported) == sorted(row["id"] for row in listed)
    assert 0 < len(exported) < len(ROWS)
    assert all(row["tenant_id"] == "t1" for row in exported)
    for name in filters:
        assert meta["filters"][name] is not None


def test_no_filter_downloads_every_row_of_the_tenant(h):
    _, exported = _exported(h)
    assert len(exported) == len(ROWS)


def test_an_action_is_matched_exactly_as_the_list_matches_it(h):
    _, exported = _exported(h, actions="probe_upd")
    assert exported == []
    _, exported = _exported(h, actions="probe_update")
    assert [row["action"] for row in exported] == ["probe_update"]


def test_the_filters_that_matched_differently_are_gone(h):
    """`action_filter` matched a part of the action where the list matches
    the whole of it; the list's own filters cover what the three did."""
    parameters = {p["name"] for p in h.app.openapi()["paths"][EXPORT]["get"]["parameters"]}
    assert not parameters & {"user_filter", "action_filter", "success_only"}
    assert {"admin_ids", "actions", "success", "start_time", "end_time", "hours_ago", "q"} <= parameters

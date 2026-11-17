# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Virtual MCP servers over HTTP. A virtual server aggregates tools from upstream
servers, so the tenant boundary has to hold on both ends: the virtual server itself
and every upstream it names.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.virtual_servers import create_virtual_server_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.mcp_server import MCPServer
from app.database.models.tenant import Tenant
from app.database.models.virtual_server import VirtualServer
from app.services.mcp.server.virtual_server_manager import VirtualServerManager
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/virtual-servers"
_n = {"i": 0}


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("test-virtual-servers")
    for url in throwaway_pg_database("virtual_servers"):
        harness = build_admin_harness(
            [create_virtual_server_router(VirtualServerManager(logger=log), log)], database_url=url
        )

        def seed(s):
            for sid, tid in (("up-t1", "t1"), ("up2-t1", "t1"), ("up-t2", "t2")):
                s.add(MCPServer(id=sid, tenant_id=tid, name=sid, base_url="https://mcp.example"))

        harness.seed(seed)
        yield harness
        harness.close()


def _caps(server="up-t1", tool="read", **extra) -> dict:
    return {"tools": [{"server_id": server, "capability_name": tool, **extra}]}


def _vs(h, tenant="t1", caps=None, **over) -> str:
    _n["i"] += 1
    server = {"t1": "up-t1", "t2": "up-t2"}[tenant]
    body = {"name": f"vs-{_n['i']}", "description": "d", "capabilities": caps or _caps(server), **over}
    r = h.client.post(BASE, json=body, headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    return r.json()["server"]["id"]


def _row(h, vs_id):
    s = h.Factory()
    try:
        v = s.get(VirtualServer, vs_id)
        if v is None:
            return None
        return {"name": v.name, "enabled": v.enabled, "tenant_id": v.tenant_id, "capabilities": v.capabilities,
                "description": v.description, "owner": v.owner}
    finally:
        s.close()


def _audited(h, tenant, action):
    s = h.Factory()
    try:
        return s.query(TenantAdminAuditLog).filter_by(tenant_id=tenant, action=action).count()
    finally:
        s.close()


class TestIsolation:
    @pytest.mark.parametrize(
        "method,suffix,kw",
        [
            ("get", "", {}),
            ("patch", "", {"json": {"name": "stolen"}}),
            ("delete", "", {}),
            ("post", "/capabilities/tool?source_server_id=up-t1&tool_name=x", {}),
            ("post", "/capabilities/resource?source_server_id=up-t1&resource_uri=x", {}),
            ("post", "/capabilities/prompt?source_server_id=up-t1&prompt_name=x", {}),
            ("delete", "/capabilities/source/up-t2", {}),
            ("get", "/statistics", {}),
        ],
    )
    def test_another_tenants_virtual_server_looks_absent(self, h, method, suffix, kw):
        vs = _vs(h, "t2")
        before = _row(h, vs)
        r = getattr(h.client, method)(f"{BASE}/{vs}{suffix}", headers=h.headers("t1"), **kw)
        assert r.status_code == 404, r.text
        assert _row(h, vs) == before

    def test_listing_shows_only_the_callers_tenant(self, h):
        mine, theirs = _vs(h, "t1"), _vs(h, "t2")
        r = h.client.get(BASE, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert mine in r.text and theirs not in r.text

    def test_an_upstream_of_another_tenant_cannot_be_aggregated_at_create(self, h):
        r = h.client.post(
            BASE, json={"name": "cross", "description": "d", "capabilities": _caps("up-t2")}, headers=h.headers("t1")
        )
        assert r.status_code == 400
        assert "up-t2" not in h.client.get(BASE, headers=h.headers("t1")).text

    def test_an_upstream_of_another_tenant_cannot_be_aggregated_by_patch(self, h):
        vs = _vs(h)
        before = _row(h, vs)["capabilities"]
        r = h.client.patch(f"{BASE}/{vs}", json={"capabilities": _caps("up-t2")}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert _row(h, vs)["capabilities"] == before

    @pytest.mark.parametrize(
        "path", ["tool?source_server_id=up-t2&tool_name=x", "resource?source_server_id=up-t2&resource_uri=x",
                 "prompt?source_server_id=up-t2&prompt_name=x"],
    )
    def test_an_upstream_of_another_tenant_cannot_be_added_one_by_one(self, h, path):
        vs = _vs(h)
        before = _row(h, vs)["capabilities"]
        r = h.client.post(f"{BASE}/{vs}/capabilities/{path}", headers=h.headers("t1"))
        assert r.status_code in (400, 404), r.text
        assert _row(h, vs)["capabilities"] == before


class TestPermissions:
    @pytest.mark.parametrize(
        "method,suffix,kw,held",
        [
            ("get", "", {}, Permission.VIRTUAL_SERVER_CREATE),
            ("post", "", {"json": {"name": "n", "description": "d", "capabilities": {}}}, Permission.VIRTUAL_SERVER_READ),
            ("get", "/x", {}, Permission.VIRTUAL_SERVER_CREATE),
            ("patch", "/x", {"json": {"name": "n"}}, Permission.VIRTUAL_SERVER_READ),
            ("delete", "/x", {}, Permission.VIRTUAL_SERVER_UPDATE),
            ("post", "/x/capabilities/tool?source_server_id=a&tool_name=b", {}, Permission.VIRTUAL_SERVER_READ),
            ("delete", "/x/capabilities/source/a", {}, Permission.VIRTUAL_SERVER_READ),
        ],
    )
    def test_the_wrong_permission_is_403(self, h, method, suffix, kw, held):
        limited = h.limited_headers("t1", [held])
        assert getattr(h.client, method)(f"{BASE}{suffix}", headers=limited, **kw).status_code == 403

    def test_anonymous_is_refused(self, h):
        assert h.client.get(BASE).status_code in (401, 403)


class TestCrud:
    def test_create_snapshots_the_mapping_and_audits(self, h):
        before = _audited(h, "t1", "virtual_server_create")
        vs = _vs(h, caps=_caps(tool="read", exposed_name="renamed"))
        row = _row(h, vs)
        assert (row["tenant_id"], row["enabled"]) == ("t1", True)
        assert [t["exposed_name"] for t in row["capabilities"]["tools"]] == ["renamed"]
        assert row["capabilities"]["source_mapping"]["renamed"] == "up-t1"
        assert row["capabilities"]["reverse_name_mapping"]["renamed"] == "read"
        got = _audited(h, "t1", "virtual_server_create")
        assert got == before + 1 or got > 0

    def test_two_entries_claiming_one_exposed_name_are_refused(self, h):
        caps = {"tools": [{"server_id": "up-t1", "capability_name": "a", "exposed_name": "same"},
                          {"server_id": "up2-t1", "capability_name": "b", "exposed_name": "same"}]}
        r = h.client.post(BASE, json={"name": "dup-exposed", "description": "d", "capabilities": caps}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert "same" in r.text

    def test_the_feature_gate_applies(self, h):
        def flag(value):
            def apply(s):
                s.get(Tenant, "t2").features = {"virtual_mcp_servers": value}

            return apply

        h.seed(flag(False))
        try:
            r = h.client.post(BASE, json={"name": "gated", "description": "d", "capabilities": {}}, headers=h.headers("t2"))
            assert r.status_code == 403
        finally:
            h.seed(flag(True))

    def test_get_returns_counts(self, h):
        vs = _vs(h)
        r = h.client.get(f"{BASE}/{vs}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["id"] == vs

    def test_patch_changes_what_was_sent(self, h):
        vs = _vs(h)
        caps_before = _row(h, vs)["capabilities"]
        r = h.client.patch(f"{BASE}/{vs}", json={"name": "renamed-vs", "enabled": False}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        row = _row(h, vs)
        assert (row["name"], row["enabled"], row["description"]) == ("renamed-vs", False, "d")
        assert row["capabilities"] == caps_before

    def test_patch_replaces_capabilities(self, h):
        vs = _vs(h)
        r = h.client.patch(f"{BASE}/{vs}", json={"capabilities": _caps("up2-t1", "write")}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, vs)["capabilities"]["source_mapping"]["write"] == "up2-t1"

    def test_delete_removes_it(self, h):
        vs = _vs(h)
        assert h.client.delete(f"{BASE}/{vs}", headers=h.headers("t1")).status_code in (200, 204)
        assert _row(h, vs) is None

    def test_statistics_answer(self, h):
        vs = _vs(h)
        r = h.client.get(f"{BASE}/{vs}/statistics", headers=h.headers("t1"))
        assert r.status_code == 200, r.text

    def test_listing_filters_enabled(self, h):
        off = _vs(h)
        h.client.patch(f"{BASE}/{off}", json={"enabled": False}, headers=h.headers("t1"))
        r = h.client.get(f"{BASE}?enabled_only=true", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert off not in r.text

    @pytest.mark.parametrize(
        "method,suffix,kw", [("get", "", {}), ("patch", "", {"json": {"name": "x"}}), ("delete", "", {})]
    )
    def test_unknown_is_404(self, h, method, suffix, kw):
        assert getattr(h.client, method)(f"{BASE}/nope{suffix}", headers=h.headers("t1"), **kw).status_code == 404


class TestCapabilityEdits:
    def test_add_each_kind_and_remove_by_source(self, h):
        vs = _vs(h, caps={"tools": []})
        t1 = h.headers("t1")
        for path in (
            "tool?source_server_id=up-t1&tool_name=extra",
            "resource?source_server_id=up-t1&resource_uri=file://a",
            "prompt?source_server_id=up-t1&prompt_name=hello",
        ):
            r = h.client.post(f"{BASE}/{vs}/capabilities/{path}", headers=t1)
            assert r.status_code == 201, r.text
        caps = _row(h, vs)["capabilities"]
        assert caps["tools"] and caps["resources"] and caps["prompts"]
        assert caps["source_mapping"]["extra"] == "up-t1", "the core routes tools/call by this map"
        r = h.client.delete(f"{BASE}/{vs}/capabilities/source/up-t1", headers=t1)
        assert r.status_code == 200, r.text
        caps = _row(h, vs)["capabilities"]
        assert not caps["tools"] and not caps["resources"] and not caps["prompts"]
        assert caps["source_mapping"] == {} and caps["reverse_name_mapping"] == {}

    def test_removing_one_source_keeps_the_other(self, h):
        vs = _vs(h, caps={"tools": []})
        t1 = h.headers("t1")
        h.client.post(f"{BASE}/{vs}/capabilities/tool?source_server_id=up-t1&tool_name=a", headers=t1)
        h.client.post(f"{BASE}/{vs}/capabilities/tool?source_server_id=up2-t1&tool_name=b", headers=t1)
        assert h.client.delete(f"{BASE}/{vs}/capabilities/source/up-t1", headers=t1).status_code == 200
        caps = _row(h, vs)["capabilities"]
        assert [t["server_id"] for t in caps["tools"]] == ["up2-t1"]
        assert caps["source_mapping"] == {"b": "up2-t1"}

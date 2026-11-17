# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP registries and server configs over HTTP: tenant isolation, permission denial,
secret handling and the outbound URL guard.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.server_configs import create_admin_server_configs_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.mcp_registry import MCPRegistry
from app.database.models.mcp_server import MCPServer
from app.database.models.server_config import ServerConfig
from app.database.models.tenant import Tenant
from app.services.portal.server_config_manager import ServerConfigManager
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

REG = "/v1/admin/mcp-registries"
CFG = "/v1/admin/server-configs"
TOKEN = "registry-secret-token"
IMDS = "http://169.254.169.254/latest/meta-data"
_n = {"i": 0}


def _next() -> str:
    _n["i"] += 1
    return f"{_n['i']:04d}"


def harness_with(extra_routers=()):
    """The file's harness: the server-configs router, and the routers an
    edition adds to it (the Enterprise half mounts the registry router)."""
    log = BrutorLogger("test-registries")
    for url in throwaway_pg_database("reg_cfg"):
        harness = build_admin_harness(
            [*extra_routers, create_admin_server_configs_router(ServerConfigManager(log), log)],
            database_url=url,
        )

        def seed(s):
            for tid in ("t1", "t2"):
                s.add(MCPServer(id=f"srv-{tid}", tenant_id=tid, name=f"srv-{tid}", base_url="https://mcp.example"))
                s.get(Tenant, tid).features = {}

        harness.seed(seed)
        yield harness
        harness.close()


@pytest.fixture(scope="module")
def h():
    yield from harness_with()


def _reg_body(**over) -> dict:
    n = _next()
    return {"id": f"r{n}", "name": f"reg {n}", "url": f"https://reg{n}.example/", "auth_type": "none", **over}


def _reg(h, tenant="t1", **over) -> dict:
    r = h.client.post(REG, json=_reg_body(**over), headers=h.headers(tenant))
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("registry", body)


def _reg_row(h, rid):
    s = h.Factory()
    try:
        row = s.get(MCPRegistry, rid)
        if row is not None:
            s.expunge(row)
        return row
    finally:
        s.close()


def _cfg(h, tenant="t1", **over) -> dict:
    r = h.client.post(
        CFG, json={"mcp_server_id": f"srv-{tenant}", "name": f"cfg-{_next()}", **over}, headers=h.headers(tenant)
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("config", body)


def _cfg_row(h, cid):
    s = h.Factory()
    try:
        row = s.get(ServerConfig, cid)
        if row is not None:
            s.expunge(row)
        return row
    finally:
        s.close()


class TestServerConfigs:
    @pytest.mark.parametrize(
        "method,suffix,kw", [("get", "", {}), ("patch", "", {"json": {"name": "stolen"}}), ("delete", "", {})]
    )
    def test_another_tenants_config_looks_absent(self, h, method, suffix, kw):
        cfg = _cfg(h, "t2")
        r = getattr(h.client, method)(f"{CFG}/{cfg['id']}{suffix}", headers=h.headers("t1"), **kw)
        assert r.status_code == 404, r.text
        assert _cfg_row(h, cfg["id"]).name == cfg["name"]

    def test_another_tenants_server_cannot_back_a_config(self, h):
        r = h.client.post(CFG, json={"mcp_server_id": "srv-t2", "name": "cross"}, headers=h.headers("t1"))
        assert r.status_code in (400, 404), r.text

    def test_exactly_one_backend_is_required(self, h):
        r = h.client.post(CFG, json={"name": "none"}, headers=h.headers("t1"))
        assert r.status_code in (400, 422)
        r = h.client.post(CFG, json={"mcp_server_id": "srv-t1", "virtual_server_id": "vs", "name": "both"}, headers=h.headers("t1"))
        assert r.status_code in (400, 422)

    def test_listing_and_lookups_stay_in_the_tenant(self, h):
        mine, theirs = _cfg(h, "t1"), _cfg(h, "t2")
        for path in ("", "/by-server/srv-t1"):
            r = h.client.get(f"{CFG}{path}", headers=h.headers("t1"))
            assert r.status_code == 200, r.text
            assert mine["id"] in r.text and theirs["id"] not in r.text
        assert theirs["id"] not in h.client.get(f"{CFG}/by-server/srv-t2", headers=h.headers("t1")).text

    def test_update_delete_and_stats(self, h):
        cfg = _cfg(h)
        r = h.client.patch(f"{CFG}/{cfg['id']}", json={"name": "renamed", "enabled": False, "priority": 5}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        row = _cfg_row(h, cfg["id"])
        assert (row.name, row.enabled, row.priority) == ("renamed", False, 5)
        assert h.client.get(f"{CFG}/stats", headers=h.headers("t1")).status_code == 200
        assert h.client.delete(f"{CFG}/{cfg['id']}", headers=h.headers("t1")).status_code in (200, 204)
        assert _cfg_row(h, cfg["id"]) is None

    def test_a_group_of_another_tenant_is_not_bound(self, h):
        r = h.client.post(
            CFG, json={"mcp_server_id": "srv-t1", "name": f"g-{_next()}", "group_ids": ["group-default-t2"]},
            headers=h.headers("t1"),
        )
        assert r.status_code in (400, 404), r.text

    def test_by_group_lookup_answers_for_my_group_only(self, h):
        assert h.client.get(f"{CFG}/by-group/group-default-t1", headers=h.headers("t1")).status_code == 200
        cfg = _cfg(h, "t2", group_ids=["group-default-t2"])
        r = h.client.get(f"{CFG}/by-group/group-default-t2", headers=h.headers("t1"))
        assert r.status_code == 404 or (r.json()["configs"] == [] and cfg["id"] not in r.text)

    def test_a_read_only_admin_cannot_write(self, h):
        cfg = _cfg(h)
        ro = h.limited_headers("t1", [Permission.SERVER_CONFIG_READ])
        assert h.client.get(f"{CFG}/{cfg['id']}", headers=ro).status_code == 200
        assert h.client.patch(f"{CFG}/{cfg['id']}", json={"name": "x"}, headers=ro).status_code == 403
        assert h.client.delete(f"{CFG}/{cfg['id']}", headers=ro).status_code == 403
        assert _cfg_row(h, cfg["id"]).name == cfg["name"]

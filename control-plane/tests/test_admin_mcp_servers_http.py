# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
HTTP-level tests for the MCP servers admin API (/v1/admin/mcp-servers).

Network-
touching endpoints (test-connection, discover-capabilities, live health
probes) are exercised only in their no-network paths here — capability
discovery is disabled on create via the documented query flag.

What is worth defending:

  * **CRUD round-trips with tenant scoping.** A server id from another
    tenant is a 404 on read, update and delete alike.
  * **Creation without discovery** stays local and fast — the flag exists
    precisely so operators can register a server that is not up yet.
  * **Deletion is real** — the row is gone, not soft-hidden.
"""

import pytest

from tests.admin_endpoint_harness import build_admin_harness

from app.api.v1.endpoints.admin.mcp_servers import create_mcp_server_router
from app.core.logging_system import BrutorLogger
from app.services.mcp.server.mcp_server_manager import MCPServerManager


@pytest.fixture()
def h():
    log = BrutorLogger("test-mcp-servers")
    harness = build_admin_harness(
        [create_mcp_server_router(MCPServerManager(log), log)],
        logger_name="test-mcp-servers",
    )
    yield harness
    harness.close()


def _mk(name="sales-tools", **over):
    body = {
        "name": name,
        "description": "The sales team's toolbox",
        "base_url": "http://tools.internal:3000",
        "mcp_endpoint_path": "/mcp",
        "auth_type": "none",
    }
    body.update(over)
    return body


def _create(h, tenant="t1", name="sales-tools", **over):
    r = h.client.post(
        "/v1/admin/mcp-servers?discover_capabilities=false",
        json=_mk(name, **over),
        headers=h.headers(tenant),
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("server", body)


class TestCrud:
    def test_create_without_discovery_registers_the_server(self, h):
        body = _create(h)
        assert body["name"] == "sales-tools"
        assert body["base_url"] == "http://tools.internal:3000"

    def test_get_by_id_round_trips(self, h):
        sid = _create(h)["id"]
        r = h.client.get(f"/v1/admin/mcp-servers/{sid}", headers=h.headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("server", body)["id"] == sid

    def test_listing_contains_the_server(self, h):
        sid = _create(h)["id"]
        r = h.client.get("/v1/admin/mcp-servers", headers=h.headers())
        assert r.status_code == 200, r.text
        payload = r.json()
        servers = payload.get("servers", payload if isinstance(payload, list) else [])
        if isinstance(servers, dict):
            servers = list(servers.values())
        assert any(s["id"] == sid for s in servers)

    def test_patch_updates_fields(self, h):
        sid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/mcp-servers/{sid}",
            json={"description": "renamed toolbox", "enabled": False},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        server = body.get("server", body)
        assert server["description"] == "renamed toolbox"
        assert server["enabled"] is False

    def test_delete_removes_the_row(self, h):
        sid = _create(h)["id"]
        r = h.client.delete(f"/v1/admin/mcp-servers/{sid}", headers=h.headers())
        assert r.status_code in (200, 204), r.text
        assert (
            h.client.get(
                f"/v1/admin/mcp-servers/{sid}", headers=h.headers()
            ).status_code
            == 404
        )

    def test_unknown_id_is_404(self, h):
        for method, path in [
            ("get", "/v1/admin/mcp-servers/nope"),
            ("delete", "/v1/admin/mcp-servers/nope"),
        ]:
            r = getattr(h.client, method)(path, headers=h.headers())
            assert r.status_code == 404, f"{method} {path}: {r.status_code}"

    def test_create_requires_a_base_url(self, h):
        r = h.client.post(
            "/v1/admin/mcp-servers?discover_capabilities=false",
            json={"name": "no-url"},
            headers=h.headers(),
        )
        assert r.status_code == 422

    def test_reads_require_auth(self, h):
        assert h.client.get("/v1/admin/mcp-servers").status_code in (401, 403)


class TestHealth:
    def test_stored_health_is_readable_without_probing(self, h):
        sid = _create(h)["id"]
        r = h.client.get(
            f"/v1/admin/mcp-servers/{sid}/health",
            params={"probe": "false"},
            headers=h.headers(),
        )
        # Health of a never-probed server must be a readable state (however
        # the API spells "unknown"), not an error.
        assert r.status_code == 200, r.text


class TestIsolation:
    def test_other_tenant_cannot_read_update_or_delete(self, h):
        sid = _create(h, tenant="t1")["id"]
        assert (
            h.client.get(
                f"/v1/admin/mcp-servers/{sid}", headers=h.headers("t2")
            ).status_code
            == 404
        )
        assert (
            h.client.patch(
                f"/v1/admin/mcp-servers/{sid}",
                json={"description": "hostile"},
                headers=h.headers("t2"),
            ).status_code
            == 404
        )
        assert (
            h.client.delete(
                f"/v1/admin/mcp-servers/{sid}", headers=h.headers("t2")
            ).status_code
            == 404
        )

    def test_listings_are_disjoint(self, h):
        _create(h, tenant="t1", name="t1-tools")
        _create(h, tenant="t2", name="t2-tools")
        r = h.client.get("/v1/admin/mcp-servers", headers=h.headers("t1"))
        payload = r.json()
        servers = payload.get("servers", payload if isinstance(payload, list) else [])
        if isinstance(servers, dict):
            servers = list(servers.values())
        names = {s["name"] for s in servers}
        assert "t1-tools" in names
        assert "t2-tools" not in names


# ---------------------------------------------------------------------------
# Data residency region
# ---------------------------------------------------------------------------


class TestRegion:
    """`mcp_servers.region` has existed since a069_data_residency and the Rust residency
    check reads it, but neither request model carried the field, so the
    Admin UI's region input was silently dropped on create and update and
    every server came out unscoped. These lock the round-trip in."""

    def test_create_with_region_stores_and_returns_it(self, h):
        server = _create(h, region="eu-west-1")
        assert server["region"] == "eu-west-1"
        r = h.client.get(f"/v1/admin/mcp-servers/{server['id']}", headers=h.headers())
        assert r.status_code == 200, r.text
        assert r.json().get("server", r.json())["region"] == "eu-west-1"

    def test_region_defaults_to_unspecified(self, h):
        assert _create(h)["region"] is None

    def test_patch_updates_and_clears_region(self, h):
        sid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/mcp-servers/{sid}",
            json={"region": "ap-southeast-2"},
            headers=h.headers(),
        )
        assert r.status_code == 200, r.text
        assert r.json().get("server", r.json())["region"] == "ap-southeast-2"

        # An empty string is how the form says "unspecified"; it clears.
        r = h.client.patch(
            f"/v1/admin/mcp-servers/{sid}", json={"region": ""}, headers=h.headers()
        )
        assert r.status_code == 200, r.text
        assert r.json().get("server", r.json())["region"] is None

    def test_region_is_normalised_like_the_tenant_profile(self, h):
        # The runtime literal-compares this against the tenant's
        # allowed_regions, which the residency model lower-cases.
        assert _create(h, region=" EU-West-1 ")["region"] == "eu-west-1"

    @pytest.mark.parametrize("bad", ["eu west 1", "eu/west", "-eu-west-1", "x" * 33])
    def test_invalid_region_is_a_422_on_create_and_update(self, h, bad):
        r = h.client.post(
            "/v1/admin/mcp-servers?discover_capabilities=false",
            json=_mk(region=bad),
            headers=h.headers(),
        )
        assert r.status_code == 422, r.text

        sid = _create(h)["id"]
        r = h.client.patch(
            f"/v1/admin/mcp-servers/{sid}", json={"region": bad}, headers=h.headers()
        )
        assert r.status_code == 422, r.text


def test_a_server_a_virtual_server_references_is_not_deleted_and_the_answer_lists_them(h):
    """409, with the virtual servers under `detail.used_by` for a program to read."""
    from app.database.models.virtual_server import VirtualServer

    sid = _create(h)["id"]

    def fn(s):
        s.add(VirtualServer(
            id="vs-apps", tenant_id="t1", name="Apps", owner="admin_t1", enabled=True,
            capabilities={"tools": [{"server_id": sid, "name": "search"}], "resources": [], "prompts": []},
        ))

    h.seed(fn)
    r = h.client.delete(f"/v1/admin/mcp-servers/{sid}", headers=h.headers())
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["used_by"] == {"virtual_servers": [{"id": "vs-apps", "name": "Apps"}]}
    assert "Apps" in detail["message"]
    assert h.client.get(f"/v1/admin/mcp-servers/{sid}", headers=h.headers()).status_code == 200
    declared = h.client.app.openapi()["paths"]["/v1/admin/mcp-servers/{server_id}"]["delete"]["responses"]["409"]
    assert declared["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/InUseRefusal"}

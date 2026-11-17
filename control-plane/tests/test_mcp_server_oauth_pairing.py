# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
An OAuth-protected MCP server says whether an identity provider is paired
with it.

The gateway exchanges an authorization code for a server and an identity
provider only when the two are paired: the server's own `idp_id` names the
provider, or a server configuration of the server attaches an active OAuth
client of that provider. Otherwise it answers 403 "Identity Provider is not
configured for this MCP server".

A server is saved before its configurations are, so a server with no pair is
a state every OAuth server passes through: it is accepted and reported
(`oauth_pairing` of the server's answers), not refused. What is refused is
what can never pair: an `idp_id` that names no identity provider of the
tenant.
"""

from __future__ import annotations

import pytest

from app.api.v1.endpoints.admin.mcp_servers import create_mcp_server_router
from app.core.logging_system import BrutorLogger
from app.database.models.idp_server import IdPServer
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.server_config import ServerConfig
from app.services.mcp.server.mcp_server_manager import MCPServerManager
from app.services.mcp.server.oauth_pairing import UNPAIRED_WARNING, oauth_pairing
from tests.admin_endpoint_harness import build_admin_harness

BASE = "/v1/admin/mcp-servers"


@pytest.fixture()
def h():
    log = BrutorLogger("test-oauth-pairing")
    harness = build_admin_harness(
        [create_mcp_server_router(MCPServerManager(log), log)], logger_name="test-oauth-pairing"
    )

    def idps(s):
        for tenant in ("t1", "t2"):
            for name in ("a", "b"):
                s.add(IdPServer(
                    id=f"idp-{tenant}-{name}", tenant_id=tenant, name=f"IdP {name} of {tenant}",
                    type="oidc_discoverable", issuer=f"https://{name}.{tenant}.example", enabled=True,
                ))

    harness.seed(idps)
    yield harness
    harness.close()


def _create(h, tenant="t1", **over):
    body = {
        "name": "crm", "base_url": "https://crm.example", "mcp_endpoint_path": "/mcp", "auth_type": "oauth", **over,
    }
    return h.client.post(f"{BASE}?discover_capabilities=false", json=body, headers=h.headers(tenant))


def _get(h, server_id, tenant="t1"):
    r = h.client.get(f"{BASE}/{server_id}", headers=h.headers(tenant))
    assert r.status_code == 200, r.text
    body = r.json()
    return body.get("server", body)


def _listed(h, server_id, tenant="t1"):
    r = h.client.get(BASE, headers=h.headers(tenant))
    assert r.status_code == 200, r.text
    return r.json()["servers"][server_id]


def _attach(h, server_id, idp_id, *, active=True, tenant="t1", client="c1"):
    def add(s):
        s.add(OAuthAppClient(
            id=f"oauth-{client}", tenant_id=tenant, name=f"client {client}", idp_id=idp_id, client_id=client,
            client_type="public", supports_pkce=True, app_type="webapp",
            redirect_uri="https://portal.example/callback", is_active=active,
        ))
        s.add(ServerConfig(
            id=f"sc-{client}", tenant_id=tenant, mcp_server_id=server_id, oauth_client_id=f"oauth-{client}",
            name=f"config {client}", is_default=False, enabled=True,
        ))

    h.seed(add)


def test_an_oauth_server_saved_with_no_pair_says_so(h):
    r = _create(h)
    assert r.status_code in (200, 201), r.text
    server = r.json()["server"]
    unpaired = {"paired": False, "idp_ids": [], "warning": UNPAIRED_WARNING}
    assert server["oauth_pairing"] == unpaired
    assert _get(h, server["id"])["oauth_pairing"] == unpaired
    assert _listed(h, server["id"])["oauth_pairing"] == unpaired
    # The warning says what the gateway does and what ends it.
    assert "403" in UNPAIRED_WARNING and "identity provider" in UNPAIRED_WARNING
    assert "server configuration" in UNPAIRED_WARNING


def test_a_server_that_is_not_oauth_protected_has_no_pairing_to_report(h):
    r = _create(h, auth_type="none")
    assert r.status_code in (200, 201), r.text
    assert r.json()["server"]["oauth_pairing"] is None
    r = _create(h, name="bearer", base_url="https://b.example", auth_type="bearer")
    assert r.json()["server"]["oauth_pairing"] is None


def test_the_servers_own_identity_provider_pairs_it(h):
    r = _create(h, idp_id="idp-t1-a")
    assert r.status_code in (200, 201), r.text
    assert r.json()["server"]["oauth_pairing"] == {"paired": True, "idp_ids": ["idp-t1-a"], "warning": None}


def test_an_active_client_of_a_server_configuration_pairs_it(h):
    server_id = _create(h).json()["server"]["id"]
    _attach(h, server_id, "idp-t1-b")
    assert _get(h, server_id)["oauth_pairing"] == {"paired": True, "idp_ids": ["idp-t1-b"], "warning": None}
    # Both ways at once: every provider the gateway would accept.
    r = h.client.patch(f"{BASE}/{server_id}", json={"idp_id": "idp-t1-a"}, headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["server"]["oauth_pairing"] == {
        "paired": True, "idp_ids": ["idp-t1-a", "idp-t1-b"], "warning": None,
    }


def test_what_the_gateway_does_not_count_does_not_pair(h):
    server_id = _create(h).json()["server"]["id"]
    # A client that is not active.
    _attach(h, server_id, "idp-t1-a", active=False, client="inactive")
    # A client and a configuration of another tenant, under this server's id.
    _attach(h, server_id, "idp-t2-a", tenant="t2", client="foreign")
    assert _get(h, server_id)["oauth_pairing"]["paired"] is False
    # An `idp_id` on the row that names no identity provider of the tenant
    # (a row written before the check below, or by hand).
    h.seed(lambda s: s.query(MCPServer).filter(MCPServer.id == server_id).update({"idp_id": "idp-t2-a"}))
    assert _get(h, server_id)["oauth_pairing"] == {"paired": False, "idp_ids": [], "warning": UNPAIRED_WARNING}
    with h.Factory() as s:
        assert oauth_pairing(s, s.get(MCPServer, server_id))["paired"] is False


@pytest.mark.parametrize("idp_id", ["idp-nope", "idp-t2-a"])
def test_an_identity_provider_that_is_not_the_tenants_is_refused_at_save(h, idp_id):
    r = _create(h, idp_id=idp_id)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == f"Identity provider '{idp_id}' not found"
    with h.Factory() as s:
        assert s.query(MCPServer).count() == 0

    server_id = _create(h, idp_id="idp-t1-a").json()["server"]["id"]
    r = h.client.patch(f"{BASE}/{server_id}", json={"idp_id": idp_id}, headers=h.headers())
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == f"Identity provider '{idp_id}' not found"
    assert _get(h, server_id)["idp_id"] == "idp-t1-a"
    # Taking the provider off is not naming one.
    r = h.client.patch(f"{BASE}/{server_id}", json={"idp_id": None}, headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["server"]["oauth_pairing"]["paired"] is False


def test_changing_the_auth_type_changes_what_is_reported(h):
    server_id = _create(h, auth_type="none").json()["server"]["id"]
    r = h.client.patch(f"{BASE}/{server_id}", json={"auth_type": "oauth"}, headers=h.headers())
    assert r.status_code == 200, r.text
    assert r.json()["server"]["oauth_pairing"]["warning"] == UNPAIRED_WARNING

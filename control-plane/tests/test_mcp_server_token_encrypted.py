# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The static token of an MCP server is stored encrypted and never returned.

`mcp_servers.auth_token` is the bearer the gateway (and this control
plane's capability discovery) sends to an MCP server. Every writer
encrypts it with the control plane's encryption service, as a model's
`api_key` is; it is decrypted only to make the call; an update that does
not name it leaves it, an empty one removes it; no answer carries it
(`has_auth_token` says whether one is set); a control plane with no
encryption service stores none (503); and a stored value that does not
decrypt is never sent to the server.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api.v1.endpoints.admin.mcp_servers import create_mcp_server_router
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.mcp_server import MCPServer
from app.services.config_manager import ConfigManager
from app.services.mcp.server import server_token
from app.services.mcp.server.mcp_server_manager import MCPServerManager
from app.services.oauth.encryption_service import EncryptionService
from tests.admin_endpoint_harness import build_admin_harness

API = "/v1/admin/mcp-servers"
TOKEN = "mcp-server-token-that-must-not-leak"
OTHER = "second-mcp-server-token-value"
LOG = BrutorLogger("test-mcp-server-token")


class _Stop(Exception):
    """Raised where the first request to the server would be made."""


class Recording(MCPServerManager):
    """The real manager with the network taken out: the bearer of the
    first request it would have made is kept."""

    def __init__(self, logger, encryption_service=None):
        super().__init__(logger, encryption_service=encryption_service)
        self.bearers = []

    async def _probe_stateless(self, server, access_token, timeout):
        self.bearers.append(access_token)
        raise _Stop()


def _harness(encryption):
    manager = Recording(LOG, encryption_service=encryption)
    harness = build_admin_harness(
        [create_mcp_server_router(manager, LOG, encryption_service=encryption)],
        logger_name="test-mcp-server-token",
    )
    harness.manager = manager
    return harness


@pytest.fixture(autouse=True)
def _the_dead_loopback_port_is_allowed(monkeypatch):
    """The servers here live at a loopback port nothing listens on
    (`tests/hermetic.py`); private addresses are refused unless an operator
    allows them, so this file's installation allows that one."""
    monkeypatch.setenv("BRUTOR_OUTBOUND_ALLOW_HOSTS", "127.0.0.1")


@pytest.fixture()
def encryption():
    return EncryptionService(config_manager=ConfigManager(logger=LOG, validate=False), logger=LOG)


@pytest.fixture()
def h(encryption):
    harness = _harness(encryption)
    yield harness
    harness.close()


@pytest.fixture()
def bare():
    """A control plane with no encryption service."""
    harness = _harness(None)
    yield harness
    harness.close()


def _body(**over) -> dict:
    return {
        "name": "sales-tools", "base_url": "http://127.0.0.1:9", "mcp_endpoint_path": "/mcp",
        "auth_type": "bearer", "auth_token": TOKEN, **over,
    }


def _create(h, discover=False, **over):
    return h.client.post(
        f"{API}?discover_capabilities={'true' if discover else 'false'}", json=_body(**over), headers=h.headers("t1")
    )


def _server(r) -> dict:
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("server", body)


def _stored(h, server_id):
    with h.Factory() as s:
        return s.get(MCPServer, server_id).auth_token


def test_a_token_is_stored_as_ciphertext_and_never_returned(h, encryption):
    r = _create(h)
    server = _server(r)
    stored = _stored(h, server["id"])
    assert stored and stored != TOKEN and TOKEN not in stored
    assert encryption.decrypt(stored) == TOKEN
    for answer in (
        r,
        h.client.get(f"{API}/{server['id']}", headers=h.headers("t1")),
        h.client.get(API, headers=h.headers("t1")),
    ):
        assert answer.status_code in (200, 201), answer.text
        assert TOKEN not in answer.text and stored not in answer.text
        assert '"auth_token"' not in answer.text
    assert server["has_auth_token"] is True
    with h.Factory() as s:
        row = s.get(MCPServer, server["id"])
        assert TOKEN not in json.dumps(row.to_dict(include_sensitive=True), default=str)
        assert stored not in json.dumps(row.to_dict(include_sensitive=True), default=str)
        for audit in s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.resource_id == server["id"]):
            assert TOKEN not in str(audit.details) and stored not in str(audit.details)


def test_a_server_without_a_token_says_so(h):
    server = _server(_create(h, auth_type="none", auth_token=None))
    assert server["has_auth_token"] is False
    assert _stored(h, server["id"]) is None


def test_an_update_that_does_not_name_the_token_keeps_it_and_an_empty_one_removes_it(h, encryption):
    server = _server(_create(h))
    before = _stored(h, server["id"])
    r = h.client.patch(f"{API}/{server['id']}", json={"description": "renamed"}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    assert _stored(h, server["id"]) == before and r.json()["server"]["has_auth_token"] is True
    # A new token replaces it, encrypted; the audit row names the field and no value.
    r = h.client.patch(f"{API}/{server['id']}", json={"auth_token": OTHER}, headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    rotated = _stored(h, server["id"])
    assert rotated != before and encryption.decrypt(rotated) == OTHER
    assert OTHER not in r.text and rotated not in r.text
    with h.Factory() as s:
        details = [
            str(a.details) for a in s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.resource_id == server["id"])
        ]
    assert any("auth_token" in d for d in details)
    for d in details:
        assert OTHER not in d and TOKEN not in d and rotated not in d and before not in d
    for empty in ("", None):
        h.client.patch(f"{API}/{server['id']}", json={"auth_token": OTHER}, headers=h.headers("t1"))
        r = h.client.patch(f"{API}/{server['id']}", json={"auth_token": empty}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _stored(h, server["id"]) is None and r.json()["server"]["has_auth_token"] is False


def test_without_an_encryption_service_no_token_is_stored(bare):
    r = _create(bare)
    assert r.status_code == 503, r.text
    assert "ENCRYPTION_KEY" in r.json()["detail"] and TOKEN not in r.text
    with bare.Factory() as s:
        assert s.query(MCPServer).count() == 0
    # A server without a token is created, and a token cannot be added to it.
    server = _server(_create(bare, auth_token=None))
    r = bare.client.patch(f"{API}/{server['id']}", json={"auth_token": TOKEN}, headers=bare.headers("t1"))
    assert r.status_code == 503 and "ENCRYPTION_KEY" in r.json()["detail"]
    assert _stored(bare, server["id"]) is None


def test_capability_discovery_sends_the_token_in_clear(h):
    server = _server(_create(h, discover=True))
    assert h.manager.bearers == [TOKEN]
    r = h.client.post(f"{API}/{server['id']}/discover-capabilities", headers=h.headers("t1"))
    assert r.status_code in (200, 502), r.text
    assert h.manager.bearers == [TOKEN, TOKEN]
    assert TOKEN not in r.text


def test_a_server_with_no_token_is_discovered_without_one(h):
    _server(_create(h, discover=True, auth_type="none", auth_token=None))
    assert h.manager.bearers == [None]


@pytest.mark.parametrize("stored", ["typed-in-clear-before", "gAAAAABnotreallyciphertext"])
def test_a_stored_value_that_does_not_decrypt_is_never_sent(h, stored):
    server = _server(_create(h))
    with h.Factory() as s:
        s.get(MCPServer, server["id"]).auth_token = stored
        s.commit()
    r = h.client.post(f"{API}/{server['id']}/discover-capabilities", headers=h.headers("t1"))
    assert h.manager.bearers == [], "the server was called"
    assert r.status_code == 503, r.text
    assert "ENCRYPTION_KEY" in r.json()["detail"] or "decrypt" in r.json()["detail"]
    assert stored not in r.text
    # The manager itself, as the import and deployment paths call it: no call, no discovery.
    with h.Factory() as s:
        row = s.get(MCPServer, server["id"])
        assert asyncio.run(h.manager.discover_capabilities(server=row, db=s)) is False
    assert h.manager.bearers == []


def test_a_manager_without_an_encryption_service_does_not_send_a_stored_value(h):
    server = _server(_create(h))
    bare_manager = Recording(LOG, encryption_service=None)
    with h.Factory() as s:
        row = s.get(MCPServer, server["id"])
        assert asyncio.run(bare_manager.discover_capabilities(server=row, db=s)) is False
    assert bare_manager.bearers == []


def test_seal_and_open_are_one_pair(encryption):
    sealed = server_token.seal(encryption, TOKEN)
    assert sealed != TOKEN and server_token.open_token(encryption, sealed) == TOKEN
    assert server_token.seal(encryption, None) is None and server_token.seal(encryption, "") is None
    assert server_token.open_token(encryption, None) is None and server_token.open_token(None, "") is None
    with pytest.raises(server_token.TokenUnreadable):
        server_token.open_token(encryption, "not-ciphertext")
    with pytest.raises(server_token.TokenUnreadable):
        server_token.open_token(None, sealed)


# --- the other writers of the column ---------------------------------------------


def _catalog_harness(encryption):
    from app.api.v1.endpoints.admin.mcp_catalog import create_mcp_catalog_router
    from app.database.models.mcp_catalog_entry import MCPCatalogEntry

    harness = build_admin_harness(
        [create_mcp_catalog_router(LOG, encryption_service=encryption)], logger_name="test-mcp-server-token"
    )
    harness.seed(lambda s: s.add(MCPCatalogEntry(
        id="cat-key", tenant_id="t1", slug="keyed_mcp", name="Keyed MCP", runtime="remote",
        auth_pattern="api_key", enabled=True, source="system", remote_config={"fixedURL": "http://127.0.0.1:9"},
    )))
    return harness


def _import(h):
    # Imported switched off: nothing is probed.
    return h.client.post(
        "/v1/admin/mcp-catalog/cat-key/import",
        json={"enabled": False, "credentials": {"api_key": TOKEN}},
        headers=h.headers("t1"),
    )


def test_a_catalog_import_stores_the_credential_as_ciphertext(encryption):
    h = _catalog_harness(encryption)
    try:
        r = _import(h)
        assert r.status_code in (200, 201), r.text
        assert TOKEN not in r.text
        with h.Factory() as s:
            (row,) = s.query(MCPServer).all()
            assert row.auth_token != TOKEN and encryption.decrypt(row.auth_token) == TOKEN
    finally:
        h.close()


def test_a_catalog_import_with_a_credential_is_refused_without_an_encryption_service():
    h = _catalog_harness(None)
    try:
        r = _import(h)
        assert r.status_code == 503, r.text
        assert "ENCRYPTION_KEY" in str(r.json()["detail"]) and TOKEN not in r.text
        with h.Factory() as s:
            assert s.query(MCPServer).count() == 0
    finally:
        h.close()


def _seed_shared(h, monkeypatch, encryption_service, spec):
    from app.database import seed_shared_mcp_servers as seed

    monkeypatch.setattr(seed, "_load_spec", lambda: [spec])
    monkeypatch.setattr(seed, "_installation_encryption_service", lambda logger: None)
    with h.Factory() as s:
        seed.seed_shared_mcp_servers(s, "t1", None, LOG, encryption_service=encryption_service)
        s.commit()


def test_the_shared_server_seed_stores_the_token_as_ciphertext(h, encryption, monkeypatch):
    spec = {"slug": "map", "base_url": "http://127.0.0.1:9/mcp", "auth_type": "bearer", "auth_token": TOKEN}
    _seed_shared(h, monkeypatch, encryption, spec)
    stored = _stored(h, "shared-mcp-map-t1")
    assert stored != TOKEN and encryption.decrypt(stored) == TOKEN


def test_the_shared_server_seed_registers_no_server_whose_token_it_cannot_encrypt(h, monkeypatch):
    spec = {"slug": "map", "base_url": "http://127.0.0.1:9/mcp", "auth_type": "bearer", "auth_token": TOKEN}
    _seed_shared(h, monkeypatch, None, spec)
    with h.Factory() as s:
        assert s.get(MCPServer, "shared-mcp-map-t1") is None
    # A server without a token needs no encryption service.
    _seed_shared(h, monkeypatch, None, {"slug": "open", "base_url": "http://127.0.0.1:9/mcp"})
    assert _stored(h, "shared-mcp-open-t1") is None


def test_the_shared_server_seed_uses_the_installations_encryption_service(encryption):
    from app.database.seed_shared_mcp_servers import _installation_encryption_service

    service = _installation_encryption_service(LOG)
    assert encryption.decrypt(service.encrypt(TOKEN)) == TOKEN


# --- a token is a bearer server's ----------------------------------------------
#
# The static token is sent as a bearer. A server that authenticates another
# way (OAuth, nothing) has none: a token stored while it was a bearer server
# does not survive the change, whichever writer makes it.


@pytest.mark.parametrize("auth_type", ["none", "oauth"])
def test_a_change_away_from_bearer_removes_the_stored_token(h, auth_type):
    server = _server(_create(h))
    assert server["has_auth_token"] is True

    r = h.client.patch(f"/v1/admin/mcp-servers/{server['id']}", json={"auth_type": auth_type}, headers=h.headers())

    assert r.status_code == 200, r.text
    assert _stored(h, server["id"]) is None
    got = h.client.get(f"/v1/admin/mcp-servers/{server['id']}", headers=h.headers()).json()
    assert _server_of(got)["has_auth_token"] is False
    with h.Factory() as s:
        rows = s.query(TenantAdminAuditLog).filter_by(action="mcp_server_update").all()
        assert any("auth_token" in json.dumps(row.details) for row in rows)


def test_a_token_sent_for_a_server_that_is_not_a_bearer_server_is_refused(h):
    server = _server(_create(h))
    before = _stored(h, server["id"])
    r = h.client.patch(
        f"/v1/admin/mcp-servers/{server['id']}", json={"auth_type": "none", "auth_token": "tok-xyz-1"}, headers=h.headers()
    )
    assert r.status_code == 422, r.text
    assert "bearer" in r.json()["detail"] and "tok-xyz-1" not in r.text
    assert _stored(h, server["id"]) == before
    # An empty token with the change is the change alone.
    r = h.client.patch(
        f"/v1/admin/mcp-servers/{server['id']}", json={"auth_type": "none", "auth_token": ""}, headers=h.headers()
    )
    assert r.status_code == 200, r.text
    assert _stored(h, server["id"]) is None


@pytest.mark.parametrize("auth_type", ["none", "oauth"])
def test_a_server_that_is_not_a_bearer_server_is_created_without_a_token(h, auth_type):
    refused = _create(h, auth_type=auth_type)
    assert refused.status_code == 422, refused.text
    assert TOKEN not in refused.text
    server = _server(_create(h, auth_type=auth_type, auth_token=None))
    assert server["has_auth_token"] is False
    assert _stored(h, server["id"]) is None


def test_no_writer_stores_a_token_on_a_server_that_is_not_a_bearer_server(h, encryption):
    """The rule is the row's own: a writer that sets the columns directly
    (a deployment, a catalog import, a seed) is held to it as the API is."""
    def insert(s):
        s.add(MCPServer(id="mcp-direct", tenant_id="t1", name="direct", base_url="https://mcp.example",
                        auth_type="oauth", auth_token=encryption.encrypt("tok")))
        s.add(MCPServer(id="mcp-bearer", tenant_id="t1", name="bearer", base_url="https://mcp.example",
                        auth_type="bearer", auth_token=encryption.encrypt("tok")))

    h.seed(insert)
    assert _stored(h, "mcp-direct") is None
    assert _stored(h, "mcp-bearer") is not None

    def change(s):
        s.get(MCPServer, "mcp-bearer").auth_type = "none"

    h.seed(change)
    assert _stored(h, "mcp-bearer") is None


def _server_of(body: dict) -> dict:
    return body.get("server", body)


# --- signing a catalog entry reads the tenant's key with the router's service ---


def test_a_catalog_entry_is_signed_with_the_routers_encryption_service(encryption):
    from app.database.models.mcp_catalog_entry import MCPCatalogEntry

    h = _catalog_harness(encryption)
    try:
        r = h.client.post("/v1/admin/mcp-catalog/cat-key/sign", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        with h.Factory() as s:
            assert s.get(MCPCatalogEntry, "cat-key").signature
    finally:
        h.close()


def test_signing_without_an_encryption_service_is_refused():
    h = _catalog_harness(None)
    try:
        r = h.client.post("/v1/admin/mcp-catalog/cat-key/sign", headers=h.headers("t1"))
        assert r.status_code == 503, r.text
        assert "ENCRYPTION_KEY" in r.text
    finally:
        h.close()

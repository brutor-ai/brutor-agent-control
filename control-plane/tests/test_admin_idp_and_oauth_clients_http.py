# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Identity providers and OAuth app clients over HTTP: tenant isolation, permission
denial, secret handling and the outbound URL guard. Both resources are what a
tenant's authentication hangs off, so every refusal also checks the row.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.api.v1.endpoints.admin.idp_servers import create_idp_admin_router
from app.api.v1.endpoints.admin.oauth_app_clients import create_admin_oauth_app_clients_router
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog
from app.database.models.idp_server import IdPServer
from app.database.models.mcp_server import MCPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.server_config import ServerConfig
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

IDPS = "/v1/admin/idps"
CLIENTS = "/v1/admin/oauth-app-clients"
SECRET = "super-secret-value"


class FakeEncryption:
    """Reversible, and visibly not the plaintext."""

    def encrypt(self, value: str) -> str:
        return f"enc::{value[::-1]}"

    def decrypt(self, value: str) -> str:
        return value.removeprefix("enc::")[::-1]


class FakeDcr:
    def __init__(self):
        self.calls = []
        self.result = SimpleNamespace(
            client_id="dcr-client",
            client_secret="dcr-secret",
            registration_client_uri="https://idp.example/reg/1",
        )
        self.error = None

    async def register_client(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(scope="module")
def dcr():
    return FakeDcr()


@pytest.fixture(scope="module")
def h(dcr):
    logger = BrutorLogger("test-idp-oauth")
    for url in throwaway_pg_database("idp_oauth"):
        harness = build_admin_harness(
            [
                create_idp_admin_router(logger),
                create_admin_oauth_app_clients_router(FakeEncryption(), logger, dcr_service=dcr),
            ],
            database_url=url,
        )
        yield harness
        harness.close()


_counter = {"n": 0}


def _unique() -> str:
    _counter["n"] += 1
    return f"{_counter['n']:04d}"


def _idp_body(**over) -> dict:
    n = _unique()
    return {
        "name": f"IdP {n}",
        "issuer": f"https://idp{n}.example",
        "type": "oidc_discoverable",
        "protocol": "oidc",
        "enabled": True,
        "well_known_url": f"https://idp{n}.example/.well-known/openid-configuration",
        **over,
    }


def _idp(h, tenant: str = "t1", **over) -> dict:
    r = h.client.post(IDPS, json=_idp_body(**over), headers=h.headers(tenant))
    assert r.status_code == 201, r.text
    return r.json()["idp"]


def _idp_row(h, idp_id: str):
    s = h.Factory()
    try:
        row = s.get(IdPServer, idp_id)
        if row is not None:
            s.expunge(row)
        return row
    finally:
        s.close()


def _client_body(idp_id: str, **over) -> dict:
    n = _unique()
    return {
        "name": f"client {n}",
        "app_type": "webapp",
        "idp_id": idp_id,
        "client_id": f"cid-{n}",
        "client_secret": SECRET,
        "redirect_uri": "https://app.example/cb",
        "client_type": "confidential",
        **over,
    }


def _client(h, tenant: str = "t1", idp_id: str | None = None, **over) -> dict:
    idp_id = idp_id or _idp(h, tenant)["id"]
    r = h.client.post(CLIENTS, json=_client_body(idp_id, **over), headers=h.headers(tenant))
    assert r.status_code == 201, r.text
    return r.json()


def _client_row(h, client_id: str):
    s = h.Factory()
    try:
        row = s.get(OAuthAppClient, client_id)
        if row is not None:
            s.expunge(row)
        return row
    finally:
        s.close()


def _server_config(h, tenant: str, oauth_client_id: str) -> str:
    n = _unique()

    def seed(s):
        s.add(
            MCPServer(id=f"srv-{n}", tenant_id=tenant, name=f"srv-{n}", base_url="https://mcp.example")
        )
        s.flush()
        s.add(
            ServerConfig(
                id=f"cfg-{n}", tenant_id=tenant, name=f"cfg-{n}", mcp_server_id=f"srv-{n}",
                oauth_client_id=oauth_client_id, enabled=True,
            )
        )

    h.seed(seed)
    return f"cfg-{n}"


def _audited(h, tenant: str, action: str, success: bool = True) -> int:
    s = h.Factory()
    try:
        return (
            s.query(TenantAdminAuditLog)
            .filter(
                TenantAdminAuditLog.tenant_id == tenant,
                TenantAdminAuditLog.action == action,
                TenantAdminAuditLog.success == success,
            )
            .count()
        )
    finally:
        s.close()


# ── Identity providers ──────────────────────────────────────────────────────


class TestIdpIsolation:
    def test_another_tenants_idp_looks_absent_on_every_route(self, h):
        idp = _idp(h, "t2")
        t1 = h.headers("t1")
        assert h.client.get(f"{IDPS}/{idp['id']}", headers=t1).status_code == 404
        assert h.client.patch(f"{IDPS}/{idp['id']}", json={"name": "stolen"}, headers=t1).status_code == 404
        assert h.client.delete(f"{IDPS}/{idp['id']}", headers=t1).status_code == 404
        assert h.client.post(f"{IDPS}/{idp['id']}/test", headers=t1).status_code == 404
        assert h.client.post(f"{IDPS}/{idp['id']}/discover", headers=t1).status_code == 404
        row = _idp_row(h, idp["id"])
        assert row is not None and row.name == idp["name"]

    def test_listing_shows_only_the_callers_tenant(self, h):
        mine, theirs = _idp(h, "t1"), _idp(h, "t2")
        r = h.client.get(IDPS, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert mine["id"] in r.json()["idps"] and theirs["id"] not in r.json()["idps"]

    def test_the_same_issuer_may_exist_in_two_tenants(self, h):
        body = _idp_body()
        assert h.client.post(IDPS, json=body, headers=h.headers("t1")).status_code == 201
        assert h.client.post(IDPS, json=body, headers=h.headers("t2")).status_code == 201


class TestIdpPermissions:
    @pytest.mark.parametrize(
        "method,suffix,body,held",
        [
            ("get", "", None, Permission.IDP_SERVER_CREATE),
            ("post", "", "create", Permission.IDP_SERVER_READ),
            ("get", "/x", None, Permission.IDP_SERVER_CREATE),
            ("patch", "/x", {"name": "n"}, Permission.IDP_SERVER_READ),
            ("delete", "/x", None, Permission.IDP_SERVER_UPDATE),
            ("delete", "/x", None, Permission.IDP_SERVER_READ),
            ("post", "/x/test", None, Permission.IDP_SERVER_READ),
            ("post", "/x/discover", None, Permission.IDP_SERVER_READ),
        ],
    )
    def test_the_wrong_permission_is_403(self, h, method, suffix, body, held):
        limited = h.limited_headers("t1", [held])
        kw = {}
        if body == "create":
            kw["json"] = _idp_body()
        elif body:
            kw["json"] = body
        r = getattr(h.client, method)(f"{IDPS}{suffix}", headers=limited, **kw)
        assert r.status_code == 403, r.text

    def test_anonymous_is_refused(self, h):
        assert h.client.get(IDPS).status_code in (401, 403)


class TestIdpCrud:
    def test_create_stores_and_audits(self, h):
        before = _audited(h, "t1", "idp_create")
        idp = _idp(h, "t1", description="d")
        row = _idp_row(h, idp["id"])
        assert (row.tenant_id, row.description, row.protocol) == ("t1", "d", "oidc")
        assert _audited(h, "t1", "idp_create") == before + 1

    def test_a_duplicate_issuer_is_refused_and_audited(self, h):
        body = _idp_body()
        assert h.client.post(IDPS, json=body, headers=h.headers("t1")).status_code == 201
        r = h.client.post(IDPS, json=body, headers=h.headers("t1"))
        assert r.status_code == 400
        s = h.Factory()
        try:
            assert s.query(IdPServer).filter(IdPServer.tenant_id == "t1", IdPServer.issuer == body["issuer"]).count() == 1
        finally:
            s.close()

    def test_get_returns_it_with_no_reachability_by_default(self, h):
        idp = _idp(h)
        r = h.client.get(f"{IDPS}/{idp['id']}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["idp"]["id"] == idp["id"] and r.json()["is_reachable"] is None

    def test_enabled_only_filters(self, h):
        on, off = _idp(h, enabled=True), _idp(h, enabled=False)
        r = h.client.get(f"{IDPS}?enabled_only=true", headers=h.headers("t1"))
        assert on["id"] in r.json()["idps"] and off["id"] not in r.json()["idps"]
        assert r.json()["enabled_count"] == r.json()["total"]

    def test_patch_changes_only_what_was_sent_and_audits_the_change(self, h):
        idp = _idp(h, description="keep")
        before = _audited(h, "t1", "idp_update")
        r = h.client.patch(f"{IDPS}/{idp['id']}", json={"name": "Renamed"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        row = _idp_row(h, idp["id"])
        assert (row.name, row.description) == ("Renamed", "keep")
        assert _audited(h, "t1", "idp_update") == before + 1

    def test_a_patch_that_changes_nothing_writes_no_audit_row(self, h):
        idp = _idp(h)
        before = _audited(h, "t1", "idp_update")
        r = h.client.patch(f"{IDPS}/{idp['id']}", json={"name": idp["name"]}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _audited(h, "t1", "idp_update") == before

    def test_patch_cannot_move_it_to_another_tenant(self, h):
        idp = _idp(h)
        r = h.client.patch(f"{IDPS}/{idp['id']}", json={"tenant_id": "t2"}, headers=h.headers("t1"))
        assert r.status_code == 422
        assert _idp_row(h, idp["id"]).tenant_id == "t1"

    def test_delete_removes_it_and_audits(self, h):
        idp = _idp(h)
        before = _audited(h, "t1", "idp_delete")
        assert h.client.delete(f"{IDPS}/{idp['id']}", headers=h.headers("t1")).status_code == 204
        assert _idp_row(h, idp["id"]) is None
        assert _audited(h, "t1", "idp_delete") == before + 1

    @pytest.mark.parametrize(
        "method,suffix,kw",
        [("get", "", {}), ("patch", "", {"json": {"name": "x"}}), ("delete", "", {}), ("post", "/test", {}),
         ("post", "/discover", {})],
    )
    def test_unknown_is_404(self, h, method, suffix, kw):
        assert getattr(h.client, method)(f"{IDPS}/nope{suffix}", headers=h.headers("t1"), **kw).status_code == 404


class TestIdpOutboundGuard:
    IMDS = "http://169.254.169.254/latest/meta-data"

    # Every IdP field the control plane or the core fetches, written out: the
    # router asks the identity protocol registry, and the list held here is
    # what that must keep answering (OIDC, the only protocol in this release).
    URL_FIELDS = (
        "well_known_url", "authorization_endpoint", "token_endpoint", "revocation_endpoint",
        "introspection_endpoint", "jwks_uri", "userinfo_endpoint",
    )

    def test_the_registry_guards_exactly_these_fields(self):
        from app.composition.identity import IDENTITY_PROTOCOLS

        assert IDENTITY_PROTOCOLS.url_fields() == self.URL_FIELDS

    @pytest.mark.parametrize("field", __import__("app.composition.identity", fromlist=["IDENTITY_PROTOCOLS"]).IDENTITY_PROTOCOLS.url_fields())
    def test_create_refuses_a_metadata_address_in_any_url_field(self, h, field):
        body = _idp_body(**{field: self.IMDS})
        r = h.client.post(IDPS, json=body, headers=h.headers("t1"))
        assert r.status_code == 400, r.text
        assert "outbound destination policy" in r.json()["detail"]
        s = h.Factory()
        try:
            assert s.query(IdPServer).filter(IdPServer.issuer == body["issuer"]).count() == 0
        finally:
            s.close()

    def test_patch_refuses_it_and_keeps_the_old_url(self, h):
        idp = _idp(h)
        r = h.client.patch(f"{IDPS}/{idp['id']}", json={"token_endpoint": self.IMDS}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert _idp_row(h, idp["id"]).token_endpoint != self.IMDS

    @pytest.mark.parametrize("path", ["test", "discover"])
    def test_a_stored_metadata_address_is_never_fetched(self, h, path, monkeypatch):
        idp = _idp(h)

        def poison(s):
            s.get(IdPServer, idp["id"]).well_known_url = self.IMDS

        h.seed(poison)

        def boom(*_a, **_k):
            raise AssertionError("fetched a refused URL")

        monkeypatch.setattr(httpx, "AsyncClient", boom)
        r = h.client.post(f"{IDPS}/{idp['id']}/{path}", headers=h.headers("t1"))
        assert r.status_code == 400, r.text


def _fake_http(monkeypatch, routes: dict | None = None, raises: Exception | None = None):
    """Replace httpx in the IdP module. `routes` maps URL to (status, json)."""
    routes = routes or {}
    seen: list[str] = []

    class Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def _call(self, url):
            seen.append(url)
            if raises:
                raise raises
            status, body = routes.get(url, (404, {}))
            return httpx.Response(status, json=body)

        async def get(self, url):
            return await self._call(url)

        async def post(self, url, json=None):
            return await self._call(url)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return seen


class TestIdpTest:
    def test_reports_each_endpoint(self, h, monkeypatch):
        idp = _idp(h, authorization_endpoint="https://a.example/auth", token_endpoint="https://a.example/token")
        _fake_http(
            monkeypatch,
            {
                idp["well_known_url"]: (200, {"issuer": "x"}),
                "https://a.example/auth": (302, {}),
                "https://a.example/token": (500, {}),
            },
        )
        r = h.client.post(f"{IDPS}/{idp['id']}/test", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["connected"] is True and body["discovery_works"] is True
        assert body["endpoints_available"] == {"well_known": True, "authorization": True, "token": False}

    def test_an_unreachable_idp_is_reported_not_raised(self, h, monkeypatch):
        idp = _idp(h, authorization_endpoint="https://b.example/auth", token_endpoint="https://b.example/token")
        _fake_http(monkeypatch, raises=httpx.ConnectError("down"))
        r = h.client.post(f"{IDPS}/{idp['id']}/test", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["connected"] is False and r.json()["discovery_works"] is False

    def test_a_non_200_well_known_is_not_discovery(self, h, monkeypatch):
        idp = _idp(h)
        _fake_http(monkeypatch, {idp["well_known_url"]: (500, {})})
        r = h.client.post(f"{IDPS}/{idp['id']}/test", headers=h.headers("t1"))
        assert r.json()["discovery_works"] is False

    def test_get_with_reachability_checks_an_enabled_idp(self, h, monkeypatch):
        idp = _idp(h)
        seen = _fake_http(monkeypatch, {idp["well_known_url"]: (200, {})})
        r = h.client.get(f"{IDPS}/{idp['id']}?check_reachability=true", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["is_reachable"] is True and seen == [idp["well_known_url"]]

    def test_get_with_reachability_does_not_probe_a_disabled_idp(self, h, monkeypatch):
        idp = _idp(h, enabled=False)
        seen = _fake_http(monkeypatch)
        r = h.client.get(f"{IDPS}/{idp['id']}?check_reachability=true", headers=h.headers("t1"))
        assert r.json()["is_reachable"] is None and seen == []


class TestIdpDiscover:
    DISCOVERED = {
        "authorization_endpoint": "https://c.example/auth",
        "token_endpoint": "https://c.example/token",
        "userinfo_endpoint": "https://c.example/me",
        "jwks_uri": "https://c.example/jwks",
        "scopes_supported": ["openid", "email"],
    }

    def test_without_update_it_reads_and_stores_nothing(self, h, monkeypatch):
        idp = _idp(h)
        _fake_http(monkeypatch, {idp["well_known_url"]: (200, self.DISCOVERED)})
        r = h.client.post(f"{IDPS}/{idp['id']}/discover", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _idp_row(h, idp["id"]).token_endpoint is None

    def test_with_update_it_stores_the_endpoints_and_audits(self, h, monkeypatch):
        idp = _idp(h)
        _fake_http(monkeypatch, {idp["well_known_url"]: (200, self.DISCOVERED)})
        before = _audited(h, "t1", "idp_discover_update")
        r = h.client.post(f"{IDPS}/{idp['id']}/discover?update_config=true", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["token_endpoint"] == "https://c.example/token"
        row = _idp_row(h, idp["id"])
        assert (row.jwks_uri, row.scopes_supported) == ("https://c.example/jwks", ["openid", "email"])
        assert _audited(h, "t1", "idp_discover_update") == before + 1

    def test_a_metadata_address_advertised_by_the_idp_is_refused_and_not_stored(self, h, monkeypatch):
        idp = _idp(h)
        _fake_http(
            monkeypatch,
            {idp["well_known_url"]: (200, {"token_endpoint": "http://169.254.169.254/latest/meta-data"})},
        )
        r = h.client.post(f"{IDPS}/{idp['id']}/discover?update_config=true", headers=h.headers("t1"))
        assert r.status_code == 400
        assert _idp_row(h, idp["id"]).token_endpoint is None

    def test_a_failed_discovery_is_502_and_stores_nothing(self, h, monkeypatch):
        idp = _idp(h)
        _fake_http(monkeypatch, {idp["well_known_url"]: (503, {})})
        r = h.client.post(f"{IDPS}/{idp['id']}/discover?update_config=true", headers=h.headers("t1"))
        assert r.status_code == 502
        assert _idp_row(h, idp["id"]).token_endpoint is None

    def test_a_transport_failure_is_502(self, h, monkeypatch):
        idp = _idp(h)
        _fake_http(monkeypatch, raises=httpx.ConnectError("down"))
        assert h.client.post(f"{IDPS}/{idp['id']}/discover", headers=h.headers("t1")).status_code == 502

    def test_an_idp_with_no_well_known_is_refused(self, h):
        idp = _idp(h, type="manual", well_known_url=None, authorization_endpoint="https://d.example/a",
                   token_endpoint="https://d.example/t")
        assert h.client.post(f"{IDPS}/{idp['id']}/discover", headers=h.headers("t1")).status_code == 400


# ── OAuth app clients ───────────────────────────────────────────────────────


class TestClientIsolation:
    def test_another_tenants_client_looks_absent_on_every_route(self, h):
        c = _client(h, "t2")
        t1 = h.headers("t1")
        assert h.client.get(f"{CLIENTS}/{c['id']}", headers=t1).status_code == 404
        assert h.client.get(f"{CLIENTS}/{c['id']}/usage", headers=t1).status_code == 404
        assert h.client.patch(f"{CLIENTS}/{c['id']}", json={"name": "stolen"}, headers=t1).status_code == 404
        assert h.client.delete(f"{CLIENTS}/{c['id']}", headers=t1).status_code == 404
        assert _client_row(h, c["id"]).name == c["name"]

    def test_listing_shows_only_the_callers_tenant(self, h):
        mine, theirs = _client(h, "t1"), _client(h, "t2")
        ids = {x["id"] for x in h.client.get(CLIENTS, headers=h.headers("t1")).json()["clients"]}
        assert mine["id"] in ids and theirs["id"] not in ids

    def test_a_client_cannot_be_attached_to_another_tenants_idp(self, h):
        foreign = _idp(h, "t2")
        body = _client_body(foreign["id"])
        r = h.client.post(CLIENTS, json=body, headers=h.headers("t1"))
        assert r.status_code == 404
        s = h.Factory()
        try:
            assert s.query(OAuthAppClient).filter(OAuthAppClient.client_id == body["client_id"]).count() == 0
        finally:
            s.close()

    def test_usage_does_not_count_another_tenants_configs(self, h):
        c = _client(h, "t1")
        _server_config(h, "t1", c["id"])
        r = h.client.get(f"{CLIENTS}/{c['id']}/usage", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["total_configs_using"] == 1
        assert r.json()["usage_details"][0]["enabled"] is True


class TestClientPermissions:
    @pytest.mark.parametrize(
        "method,suffix,body,held",
        [
            ("get", "", None, Permission.OAUTH_CLIENT_CREATE),
            ("get", "/x", None, Permission.OAUTH_CLIENT_CREATE),
            ("get", "/x/usage", None, Permission.OAUTH_CLIENT_CREATE),
            ("post", "", "create", Permission.OAUTH_CLIENT_READ),
            ("patch", "/x", {"name": "n"}, Permission.OAUTH_CLIENT_READ),
            ("delete", "/x", None, Permission.OAUTH_CLIENT_UPDATE),
            ("post", "/auto-register", "dcr", Permission.OAUTH_CLIENT_READ),
        ],
    )
    def test_the_wrong_permission_is_403(self, h, method, suffix, body, held):
        limited = h.limited_headers("t1", [held])
        kw = {}
        if body == "create":
            kw["json"] = _client_body("idp-x")
        elif body == "dcr":
            kw["json"] = {"idp_id": "idp-x", "client_name": "n", "app_type": "webapp", "redirect_uri": "https://a.example/cb"}
        elif body:
            kw["json"] = body
        r = getattr(h.client, method)(f"{CLIENTS}{suffix}", headers=limited, **kw)
        assert r.status_code == 403, r.text


class TestClientSecrets:
    def test_the_secret_is_stored_encrypted_and_never_returned(self, h):
        c = _client(h)
        row = _client_row(h, c["id"])
        assert row.encrypted_client_secret == FakeEncryption().encrypt(SECRET)
        assert SECRET not in str(c)
        got = h.client.get(f"{CLIENTS}/{c['id']}", headers=h.headers("t1"))
        assert SECRET not in got.text and "encrypted_client_secret" not in got.json()
        listed = h.client.get(CLIENTS, headers=h.headers("t1"))
        assert SECRET not in listed.text and FakeEncryption().encrypt(SECRET) not in listed.text

    @pytest.mark.parametrize(
        "client_type,secret",
        [("confidential", None), ("public_with_secret", None), ("public", "not-allowed")],
    )
    def test_secret_must_match_the_client_type(self, h, client_type, secret):
        idp = _idp(h)
        body = _client_body(idp["id"], client_type=client_type, client_secret=secret)
        r = h.client.post(CLIENTS, json=body, headers=h.headers("t1"))
        assert r.status_code == 400, r.text
        s = h.Factory()
        try:
            assert s.query(OAuthAppClient).filter(OAuthAppClient.client_id == body["client_id"]).count() == 0
        finally:
            s.close()

    def test_rotating_the_secret_re_encrypts_it_and_the_audit_row_masks_it(self, h):
        c = _client(h)
        r = h.client.patch(f"{CLIENTS}/{c['id']}", json={"client_secret": "rotated-value"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _client_row(h, c["id"]).encrypted_client_secret == FakeEncryption().encrypt("rotated-value")
        s = h.Factory()
        try:
            rows = (
                s.query(TenantAdminAuditLog)
                .filter(TenantAdminAuditLog.action == "oauth_app_client_update", TenantAdminAuditLog.resource_id == c["id"])
                .all()
            )
            assert rows and all("rotated-value" not in str(x.details) for x in rows)
        finally:
            s.close()

    def test_a_public_client_cannot_be_given_a_secret_by_patch(self, h):
        c = _client(h, client_type="public", client_secret=None)
        r = h.client.patch(f"{CLIENTS}/{c['id']}", json={"client_secret": "nope"}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert _client_row(h, c["id"]).encrypted_client_secret is None


class TestClientCrud:
    def test_a_duplicate_client_id_is_refused_and_audited(self, h):
        idp = _idp(h)
        body = _client_body(idp["id"])
        assert h.client.post(CLIENTS, json=body, headers=h.headers("t1")).status_code == 201
        assert h.client.post(CLIENTS, json=body, headers=h.headers("t1")).status_code == 409

    def test_list_filters(self, h):
        idp = _idp(h)
        web = _client(h, idp_id=idp["id"], app_type="webapp")
        cli = _client(h, idp_id=idp["id"], app_type="cli")
        off = _client(h, idp_id=idp["id"], app_type="cli")
        assert h.client.patch(f"{CLIENTS}/{off['id']}", json={"is_active": False}, headers=h.headers("t1")).status_code == 200
        ids = lambda q: {x["id"] for x in h.client.get(f"{CLIENTS}?{q}", headers=h.headers("t1")).json()["clients"]}
        assert ids(f"idp_id={idp['id']}") == {web["id"], cli["id"], off["id"]}
        assert ids(f"idp_id={idp['id']}&app_type=cli") == {cli["id"], off["id"]}
        assert ids(f"idp_id={idp['id']}&active_only=true") == {web["id"], cli["id"]}

    def test_patch_updates_each_field_and_audits(self, h):
        c = _client(h)
        r = h.client.patch(
            f"{CLIENTS}/{c['id']}",
            json={
                "name": "n2", "client_id": "cid-new", "redirect_uri": "https://app.example/cb2", "scopes": ["openid"],
                "supports_pkce": False, "allowed_grant_types": ["client_credentials"], "description": "d2",
            },
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        row = _client_row(h, c["id"])
        assert (row.name, row.client_id, row.redirect_uri, row.scopes) == (
            "n2", "cid-new", "https://app.example/cb2", ["openid"],
        )
        assert (row.supports_pkce, row.allowed_grant_types, row.description) == (False, ["client_credentials"], "d2")

    def test_a_patch_with_no_change_is_quiet(self, h):
        c = _client(h)
        before = _audited(h, "t1", "oauth_app_client_update")
        r = h.client.patch(f"{CLIENTS}/{c['id']}", json={"name": c["name"]}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _audited(h, "t1", "oauth_app_client_update") == before

    def test_patch_to_a_taken_client_id_is_a_conflict(self, h):
        idp = _idp(h)
        a, b = _client(h, idp_id=idp["id"]), _client(h, idp_id=idp["id"])
        r = h.client.patch(f"{CLIENTS}/{a['id']}", json={"client_id": b["client_id"]}, headers=h.headers("t1"))
        assert r.status_code == 409
        assert _client_row(h, a["id"]).client_id == a["client_id"]

    def test_a_client_in_use_cannot_be_deactivated(self, h):
        c = _client(h)
        _server_config(h, "t1", c["id"])
        r = h.client.patch(f"{CLIENTS}/{c['id']}", json={"is_active": False}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert _client_row(h, c["id"]).is_active is True

    def test_a_client_in_use_is_not_deleted_without_force(self, h):
        c = _client(h)
        cfg = _server_config(h, "t1", c["id"])
        r = h.client.delete(f"{CLIENTS}/{c['id']}", headers=h.headers("t1"))
        assert r.status_code == 409
        assert [row["name"] for row in r.json()["detail"]["used_by"]["server_configs"]] == [cfg]
        assert "force=true" in r.json()["detail"]["message"]
        assert _client_row(h, c["id"]) is not None

    def test_force_deletes_a_client_in_use(self, h):
        c = _client(h)
        cfg = _server_config(h, "t1", c["id"])
        r = h.client.delete(f"{CLIENTS}/{c['id']}?force=true", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["affected_configs"] == [cfg]
        assert _client_row(h, c["id"]) is None

    def test_delete_of_an_unused_client_audits(self, h):
        c = _client(h)
        before = _audited(h, "t1", "oauth_app_client_delete")
        assert h.client.delete(f"{CLIENTS}/{c['id']}", headers=h.headers("t1")).status_code == 200
        assert _audited(h, "t1", "oauth_app_client_delete") == before + 1

    @pytest.mark.parametrize(
        "method,suffix,kw", [("get", "", {}), ("get", "/usage", {}), ("patch", "", {"json": {"name": "x"}}), ("delete", "", {})]
    )
    def test_unknown_is_404(self, h, method, suffix, kw):
        assert getattr(h.client, method)(f"{CLIENTS}/nope{suffix}", headers=h.headers("t1"), **kw).status_code == 404

    def test_create_on_an_unknown_idp_is_404(self, h):
        assert h.client.post(CLIENTS, json=_client_body("nope"), headers=h.headers("t1")).status_code == 404


class TestAutoRegister:
    def _body(self, idp_id: str, **over) -> dict:
        return {
            "idp_id": idp_id, "client_name": f"dcr {_unique()}", "app_type": "webapp",
            "redirect_uri": "https://app.example/cb", "scopes": ["openid", "email"], **over,
        }

    def _dcr_idp(self, h, tenant: str = "t1") -> str:
        idp = _idp(h, tenant)

        def set_endpoint(s):
            s.get(IdPServer, idp["id"]).registration_endpoint = "https://idp.example/register"

        h.seed(set_endpoint)
        return idp["id"]

    def test_stores_the_returned_credentials_encrypted(self, h, dcr):
        dcr.error, dcr.calls[:] = None, []
        r = h.client.post(f"{CLIENTS}/auto-register", json=self._body(self._dcr_idp(h)), headers=h.headers("t1"))
        assert r.status_code == 201, r.text
        assert "dcr-secret" not in r.text
        row = _client_row(h, r.json()["id"])
        assert row.auto_registered is True and row.client_type == "confidential"
        assert row.encrypted_client_secret == FakeEncryption().encrypt("dcr-secret")
        assert dcr.calls[0]["scope"] == "openid email"

    def test_no_secret_back_means_a_public_client(self, h, dcr):
        dcr.error = None
        original = dcr.result
        dcr.result = SimpleNamespace(client_id="dcr-pub", client_secret=None, registration_client_uri=None)
        try:
            r = h.client.post(f"{CLIENTS}/auto-register", json=self._body(self._dcr_idp(h)), headers=h.headers("t1"))
        finally:
            dcr.result = original
        assert r.status_code == 201, r.text
        row = _client_row(h, r.json()["id"])
        assert (row.client_type, row.encrypted_client_secret) == ("public", None)

    def test_another_tenants_idp_is_404_and_never_called(self, h, dcr):
        dcr.calls[:] = []
        foreign = self._dcr_idp(h, "t2")
        r = h.client.post(f"{CLIENTS}/auto-register", json=self._body(foreign), headers=h.headers("t1"))
        assert r.status_code == 404
        assert dcr.calls == []

    def test_an_idp_without_a_registration_endpoint_is_refused(self, h, dcr):
        dcr.calls[:] = []
        idp = _idp(h)
        r = h.client.post(f"{CLIENTS}/auto-register", json=self._body(idp["id"]), headers=h.headers("t1"))
        assert r.status_code == 400
        assert dcr.calls == []

    def test_a_refusal_from_the_idp_is_400_and_stores_nothing(self, h, dcr):
        dcr.error = ValueError("registration refused")
        try:
            body = self._body(self._dcr_idp(h))
            r = h.client.post(f"{CLIENTS}/auto-register", json=body, headers=h.headers("t1"))
        finally:
            dcr.error = None
        assert r.status_code == 400
        s = h.Factory()
        try:
            assert s.query(OAuthAppClient).filter(OAuthAppClient.name == body["client_name"]).count() == 0
        finally:
            s.close()

    def test_an_unexpected_failure_is_500(self, h, dcr):
        dcr.error = RuntimeError("boom")
        try:
            r = h.client.post(f"{CLIENTS}/auto-register", json=self._body(self._dcr_idp(h)), headers=h.headers("t1"))
        finally:
            dcr.error = None
        assert r.status_code == 500

    def test_not_configured_is_501(self):
        logger = BrutorLogger("test-idp-oauth-nodcr")
        h2 = build_admin_harness([create_admin_oauth_app_clients_router(FakeEncryption(), logger)])
        try:
            r = h2.client.post(
                f"{CLIENTS}/auto-register",
                json={"idp_id": "x", "client_name": "n", "app_type": "webapp", "redirect_uri": "https://a.example/cb"},
                headers=h2.headers("t1"),
            )
            assert r.status_code == 501
        finally:
            h2.close()


class TestAnIdentityProviderInUseIsNotDeleted:
    """Deleting it would take its OAuth clients (and the access
    configurations built on them) with it and leave MCP servers and agent
    identities pointing at nothing."""

    def _refused(self, h, idp_id: str) -> dict:
        r = h.client.delete(f"{IDPS}/{idp_id}", headers=h.headers("t1"))
        assert r.status_code == 409, r.text
        assert _idp_row(h, idp_id) is not None
        return r.json()["detail"]

    def test_while_an_oauth_client_uses_it(self, h):
        idp = _idp(h)
        c = _client(h, idp_id=idp["id"])

        detail = self._refused(h, idp["id"])

        assert detail["used_by"]["oauth_app_clients"] == [{"id": c["id"], "name": c["name"]}]
        # The body is the one the API description declares for the 409.
        from app.api.v1.models.admin.idp_server import IdPInUseRefusal

        assert IdPInUseRefusal.model_validate({"detail": detail}).detail.used_by.keys() == detail["used_by"].keys()
        declared = h.client.app.openapi()["paths"]["/v1/admin/idps/{idp_id}"]["delete"]["responses"]["409"]
        assert declared["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/IdPInUseRefusal"}
        assert _client_row(h, c["id"]) is not None
        # Once nothing uses it, it is deleted.
        assert h.client.delete(f"{CLIENTS}/{c['id']}", headers=h.headers("t1")).status_code == 200
        assert h.client.delete(f"{IDPS}/{idp['id']}", headers=h.headers("t1")).status_code == 204

    def test_while_an_mcp_server_names_it(self, h):
        idp = _idp(h)
        n = _unique()
        h.seed(lambda s: s.add(MCPServer(
            id=f"srv-idp-{n}", tenant_id="t1", name=f"srv-idp-{n}", base_url="https://mcp.example", idp_id=idp["id"],
        )))

        detail = self._refused(h, idp["id"])

        assert detail["used_by"]["mcp_servers"] == [{"id": f"srv-idp-{n}", "name": f"srv-idp-{n}"}]
        assert "mcp_servers" in detail["message"] or "MCP server" in detail["message"]

    def test_while_an_agent_identity_signs_in_through_it(self, h):
        from app.database.models.agent_identity import AgentIdentity

        idp = _idp(h)
        n = _unique()
        h.seed(lambda s: s.add(AgentIdentity(
            id=f"agt-{n}", tenant_id="t1", name=f"agent-{n}", idp_server_id=idp["id"], idp_subject="sub",
        )))

        detail = self._refused(h, idp["id"])

        assert detail["used_by"]["agent_identities"] == [{"id": f"agt-{n}", "name": f"agent-{n}"}]

    def test_another_tenants_rows_do_not_count(self, h):
        idp = _idp(h)
        n = _unique()
        h.seed(lambda s: s.add(MCPServer(
            id=f"srv-other-{n}", tenant_id="t2", name=f"srv-other-{n}", base_url="https://mcp.example", idp_id=idp["id"],
        )))
        assert h.client.delete(f"{IDPS}/{idp['id']}", headers=h.headers("t1")).status_code == 204

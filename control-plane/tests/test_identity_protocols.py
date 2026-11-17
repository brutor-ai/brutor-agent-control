# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The identity provider code is split by protocol.

The identity provider administration (`/v1/admin/idps`) asks the identity
protocol registry (`app/composition/identity.py`) and names no protocol but
OIDC. In this release no edition registers another protocol: SAML single
sign-on is not in it (`tests/test_saml_scim_switched_off.py`), so what the
Community composition answers here is what both editions answer.

* `TestFindings*`: defects the recorder showed, fixed.
* `TestCommunity*`: the composition without any protocol but OIDC.

The restrictive answer for a stored row of a protocol that is not registered
(Enterprise data met by a Community composition; the edition guard refuses to start on it,
this is the defence behind that): it is listed and can be read, so the
operator sees it is there, and every action on it is refused with 409 — it is
not changed, deleted, tested, discovered, and no OAuth client is attached to
it. It is never handled as OIDC.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app.composition.identity import (
    IDENTITY_PROTOCOLS,
    IdentityProtocolRegistry,
    IdentityProtocolSpec,
    OIDC_PROTOCOL,
)
from app.database import get_db
from app.database.models.idp_server import IdPServer
from app.database.models.oauth_app_client import OAuthAppClient
from tests.admin_endpoint_harness import build_admin_harness
from tests.principal_matrix import community_app, community_registries, no_network

ROOT = Path(__file__).resolve().parent.parent
IDPS = "/v1/admin/idps"
CLIENTS = "/v1/admin/oauth-app-clients"

SAML_RUNTIME_OPS = {
    ("GET", "/v1/auth/saml/{idp_id}/metadata"),
    ("GET", "/v1/auth/saml/{idp_id}/login"),
    ("POST", "/v1/auth/saml/{idp_id}/acs"),
}
IDP_ADMIN_OPS = {
    ("GET", "/v1/admin/idps"),
    ("POST", "/v1/admin/idps"),
    ("GET", "/v1/admin/idps/{idp_id}"),
    ("PATCH", "/v1/admin/idps/{idp_id}"),
    ("DELETE", "/v1/admin/idps/{idp_id}"),
    ("POST", "/v1/admin/idps/{idp_id}/test"),
    ("POST", "/v1/admin/idps/{idp_id}/discover"),
}
DCR_OP = ("POST", "/v1/auth/dcr/{tenant_id}/{idp_id}/register")

_n = iter(range(1, 100_000))


def operations(app, *prefixes):
    return {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith(prefixes)
        for method in item
    }


def _oidc_body(**over) -> dict:
    n = next(_n)
    return {
        "name": f"OIDC {n}",
        "issuer": f"https://oidc{n}.example",
        "type": "oidc_discoverable",
        "protocol": "oidc",
        "enabled": True,
        "well_known_url": f"https://oidc{n}.example/.well-known/openid-configuration",
        **over,
    }


def _saml_body(**over) -> dict:
    n = next(_n)
    return {
        "name": f"SAML {n}",
        "issuer": f"https://saml{n}.example",
        "type": "generic_saml",
        "protocol": "saml",
        "enabled": True,
        "saml_idp_entity_id": f"urn:saml{n}",
        "saml_idp_sso_url": f"https://saml{n}.example/sso",
        "saml_idp_certificate": "CERT",
        **over,
    }


def _row(h, idp_id):
    s = h.Factory()
    try:
        row = s.get(IdPServer, idp_id)
        if row is not None:
            s.expunge(row)
        return row
    finally:
        s.close()


def _stored(h, protocol, tenant="t1", **columns) -> str:
    """An IdP row written straight into the database: what a composition
    finds there, whoever wrote it."""
    n = next(_n)
    idp_id = f"idp-c8-{n}"
    fields = dict(
        id=idp_id, tenant_id=tenant, name=f"Stored {n}", issuer=f"https://stored{n}.example",
        type="generic_saml", enabled=True,
        saml_idp_entity_id=f"urn:stored{n}", saml_idp_sso_url=f"https://stored{n}.example/sso",
        saml_idp_certificate="CERT", registration_endpoint=f"https://stored{n}.example/register",
        well_known_url=f"https://stored{n}.example/.well-known/openid-configuration",
    )
    fields.update(columns)

    def add(s):
        row = IdPServer(**fields)
        s.add(row)
        s.flush()
        # After the insert, so that the column default does not fill it in.
        row.protocol = protocol

    h.seed(add)
    return idp_id


def _client_body(idp_id: str) -> dict:
    n = next(_n)
    return {
        "name": f"client {n}",
        "app_type": "webapp",
        "idp_id": idp_id,
        "client_id": f"cid-{n}",
        "client_secret": "s3cret-S3cret-s3cret",
        "redirect_uri": "https://app.example/cb",
        "client_type": "confidential",
    }


@pytest.fixture(scope="module")
def h():
    """The composition that runs today, on the harness's database."""
    harness = build_admin_harness([], logger_name="test-c8")
    from tests.app_factory import create_app

    real = create_app()
    harness.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = harness.app.dependency_overrides
    harness.real = real
    harness.loose = TestClient(harness.app, raise_server_exceptions=False)
    yield harness
    harness.close()


@pytest.fixture(scope="module")
def community_client(h):
    app = community_app(h.app.dependency_overrides[get_db])
    return app, TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def community(h, community_client):
    app, client = community_client
    tenant = h.headers("t1")
    with community_registries():
        yield app, client, tenant


# =============================================================================
# Recorder: the composition with every protocol
# =============================================================================


# =============================================================================
# Findings
# =============================================================================


class TestFindingF43ASamlRowIsNotAnOAuthProvider:
    """An OAuth client could be attached to a SAML identity provider (201),
    and the public Dynamic Client Registration endpoint registered a client
    against one and issued it a secret (200). The row was handled as OIDC."""


    def test_auto_registration_is_refused_before_anything_is_fetched(self, h):
        headers = h.headers("t1")
        idp_id = _stored(h, "saml")
        with no_network():
            r = h.client.post(
                f"{CLIENTS}/auto-register",
                json={"idp_id": idp_id, "client_name": "x", "redirect_uri": "https://app.example/cb"},
                headers=headers,
            )
        assert r.status_code == 400, r.text
        assert "not an OAuth provider" in r.json()["detail"]


    def test_an_oidc_row_still_takes_a_client(self, h):
        headers = h.headers("t1")
        idp_id = h.client.post(IDPS, json=_oidc_body(), headers=headers).json()["idp"]["id"]
        assert h.client.post(CLIENTS, json=_client_body(idp_id), headers=headers).status_code == 201

    def test_mcp_oauth_discovery_does_not_reuse_a_saml_row_with_the_issuer(self, h):
        from app.core.logging_system import BrutorLogger
        from app.services.mcp.server.oauth_discovery_helper import OAuthDiscoveryHelper

        issuer = f"https://shared{next(_n)}.example"
        saml_id = _stored(h, "saml", issuer=issuer)
        helper = OAuthDiscoveryHelper.__new__(OAuthDiscoveryHelper)
        helper.logger = BrutorLogger("c8")
        s = h.Factory()
        try:
            assert helper.check_existing_idp_by_issuer(tenant_id="t1", issuer=issuer, db=s) is None
            assert helper.check_existing_idp_by_issuer(tenant_id="t1", issuer=issuer + "/", db=s) is None
        finally:
            s.close()
        oidc_id = _stored(h, "oidc", issuer=issuer + "/")
        s = h.Factory()
        try:
            found = helper.check_existing_idp_by_issuer(tenant_id="t1", issuer=issuer, db=s)
            assert found is not None and found.id == oidc_id != saml_id
        finally:
            s.close()

    @pytest.mark.parametrize("protocol", [None, "ldap", "SAML", ""])
    def test_a_row_of_no_or_an_unknown_protocol_is_not_one_either(self, h, protocol):
        headers = h.headers("t1")
        idp_id = _stored(h, protocol)
        assert _row(h, idp_id).protocol == protocol
        r = h.client.post(CLIENTS, json=_client_body(idp_id), headers=headers)
        assert r.status_code == 400, r.text
        r = h.loose.post(
            f"/v1/auth/dcr/t1/{idp_id}/register",
            json={"redirect_uris": ["https://app.example/cb"], "client_name": "x"},
        )
        assert r.status_code == 404


# =============================================================================
# The registry
# =============================================================================


class TestTheRegistry:

    def test_an_unregistered_protocol_is_nothing(self):
        for value in (None, "", "ldap", "SAML", "oidc ", 1, ["oidc"]):
            assert IDENTITY_PROTOCOLS.get(value) is None
            assert IDENTITY_PROTOCOLS.is_oauth(value) is False
            with pytest.raises(ValueError, match="protocol must be one of"):
                IDENTITY_PROTOCOLS.check_protocol(value)


    def test_registration_refusals(self):
        def spec(**over):
            fields = dict(name="x509", label="X509", fields=frozenset({"x509_ca"}))
            fields.update(over)
            return IdentityProtocolSpec(**fields)

        registry = IdentityProtocolRegistry([OIDC_PROTOCOL])
        for bad in (
            spec(name=""),
            spec(name=" x509"),
            spec(name="X509"),
            spec(name="oidc"),
            spec(feature="free_lunch"),
            spec(fields=frozenset({"name"})),
            spec(fields=frozenset({"jwks_uri"})),
            spec(exclusive_fields=frozenset({"elsewhere"})),
            spec(url_fields=("elsewhere",)),
        ):
            with pytest.raises(ValueError):
                registry.register([bad])
        with pytest.raises(ValueError):
            registry.register([spec(), spec(name="oidc")])
        assert registry.names() == ["oidc"]
        registry.register([spec()])
        assert registry.names() == ["oidc", "x509"]


# =============================================================================
# The Community composition
# =============================================================================


class TestCommunityRoutes:
    def test_only_oidc_is_registered(self, community):
        assert IDENTITY_PROTOCOLS.names() == ["oidc"]
        assert IDENTITY_PROTOCOLS.url_fields() == OIDC_PROTOCOL.url_fields

    def test_the_enterprise_identity_routes_are_absent(self, community):
        app, client, tenant = community
        paths = set(app.openapi()["paths"])
        assert not [p for p in paths if "saml" in p.lower() or "scim" in p.lower()]
        assert not SAML_RUNTIME_OPS & operations(app, "/")
        for method, path in sorted(SAML_RUNTIME_OPS):
            r = client.request(method, path.replace("{idp_id}", "nope"), follow_redirects=False)
            assert r.status_code in (404, 405), (method, path)
            assert r.json() == {"detail": "Not Found"} or r.status_code == 405
        for path in ("/scim/v2/Users", "/scim/v2/ServiceProviderConfig", "/v1/admin/scim-tokens"):
            assert client.get(path, headers=tenant).status_code == 404

    def test_a_stored_saml_row_has_no_sign_in_route(self, h, community):
        _app, client, _tenant = community
        idp_id = _stored(h, "saml")
        for method, path in sorted(SAML_RUNTIME_OPS):
            r = client.request(method, path.replace("{idp_id}", idp_id), follow_redirects=False)
            assert r.status_code in (404, 405)

    def test_no_enterprise_identity_module_is_mounted(self, community):
        app, _client, _tenant = community
        mounted = {
            (context.endpoint or context.original_route.endpoint).__module__
            for context in iter_route_contexts(app.router.routes)
        }
        assert "app.api.v1.endpoints.admin.idp_servers" in mounted
        assert "app.api.v1.endpoints.auth.dcr" in mounted
        assert not [m for m in mounted if "saml" in m or "scim" in m]

    def test_the_oidc_administration_and_registration_routes_are_there(self, community):
        app, _client, _tenant = community
        ops = operations(app, "/v1/admin/idps", "/v1/auth/")
        assert IDP_ADMIN_OPS <= ops
        assert DCR_OP in ops


class TestCommunityOidcWorks:
    def test_create_read_patch_test_delete(self, h, community):
        _app, client, tenant = community
        r = client.post(IDPS, json=_oidc_body(workload_audiences=["agents"]), headers=tenant)
        assert r.status_code == 201, r.text
        idp_id = r.json()["idp"]["id"]
        row = _row(h, idp_id)
        assert row.protocol == "oidc"
        assert row.workload_audiences == ["agents"]
        # Nothing of another protocol was written: the columns hold their defaults.
        assert (row.saml_force_authn, row.saml_want_assertions_signed, row.trust_saml_email_unverified) == (
            False, True, False,
        )
        assert row.target_user_type == "end_user"
        assert row.saml_idp_sso_url is None
        assert idp_id in client.get(IDPS, headers=tenant).json()["idps"]
        r = client.patch(f"{IDPS}/{idp_id}", json={"description": "changed", "jwks_uri": "https://x.example/jwks"}, headers=tenant)
        assert r.status_code == 200, r.text
        assert _row(h, idp_id).jwks_uri == "https://x.example/jwks"
        with no_network():
            r = client.post(f"{IDPS}/{idp_id}/test", headers=tenant)
        assert r.status_code == 200
        r = client.post(CLIENTS, json=_client_body(idp_id), headers=tenant)
        assert r.status_code == 201, r.text
        client_id = r.json()["id"]
        assert client.delete(f"{CLIENTS}/{client_id}", headers=tenant).status_code in (200, 204)
        assert client.delete(f"{IDPS}/{idp_id}", headers=tenant).status_code == 204

    def test_the_saml_fields_are_unknown_fields_even_at_their_old_defaults(self, h, community):
        """The request models no longer declare them: no client may send them."""
        _app, client, tenant = community
        for field, value in (("target_user_type", "end_user"), ("saml_want_assertions_signed", True),
                             ("saml_metadata_url", ""), ("saml_idp_certificate", None)):
            r = client.post(IDPS, json=_oidc_body(**{field: value}), headers=tenant)
            assert r.status_code == 422, (field, r.text)


class TestCommunityRefusesTheEnterpriseProtocol:
    def test_a_saml_provider_cannot_be_created(self, h, community):
        _app, client, tenant = community
        body = _saml_body()
        r = client.post(IDPS, json=body, headers=tenant)
        assert r.status_code == 422, r.text
        assert "SAML single sign-on is not available in this release; identity providers use the protocol 'oidc'." in r.text
        s = h.Factory()
        try:
            assert s.query(IdPServer).filter(IdPServer.issuer == body["issuer"]).count() == 0
        finally:
            s.close()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("saml_idp_sso_url", "https://x.example/sso"),
            ("saml_metadata_url", "https://x.example/metadata"),
            ("saml_metadata_xml", "<EntityDescriptor/>"),
            ("saml_idp_certificate", "CERT"),
            ("saml_attribute_mapping", {"email": "mail"}),
            ("saml_group_to_role_mapping", {"g": "r"}),
            ("saml_group_to_end_user_group_mapping", {"g": "grp"}),
            ("saml_force_authn", True),
            ("saml_want_assertions_signed", False),
            ("trust_saml_email_unverified", True),
            ("target_user_type", "tenant_admin"),
        ],
    )
    def test_no_field_of_it_is_accepted_on_create_or_update(self, h, community, field, value):
        _app, client, tenant = community
        body = _oidc_body(**{field: value})
        r = client.post(IDPS, json=body, headers=tenant)
        assert r.status_code == 422, r.text
        assert "Extra inputs are not permitted" in r.text
        assert field in r.text
        s = h.Factory()
        try:
            assert s.query(IdPServer).filter(IdPServer.issuer == body["issuer"]).count() == 0
        finally:
            s.close()

        idp_id = client.post(IDPS, json=_oidc_body(), headers=tenant).json()["idp"]["id"]
        before = _row(h, idp_id)
        r = client.patch(f"{IDPS}/{idp_id}", json={field: value, "name": "must not be applied"}, headers=tenant)
        assert r.status_code == 422, r.text
        after = _row(h, idp_id)
        assert after.name == before.name
        assert getattr(after, field) == getattr(before, field)

    def test_every_request_field_is_common_or_a_protocols(self):
        """A new field of the request models has to be given to someone:
        otherwise the registry could not tell it from a field of a protocol
        it does not have."""
        from app.api.v1.models.admin.idp_server import IdPConfigRequest, IdPConfigUpdateRequest

        owned = IDENTITY_PROTOCOLS.owned_fields()
        assert set(IdPConfigRequest.model_fields) <= owned
        assert set(IdPConfigUpdateRequest.model_fields) <= owned

    def test_an_oidc_row_cannot_be_turned_into_a_saml_row(self, h, community):
        _app, client, tenant = community
        idp_id = client.post(IDPS, json=_oidc_body(), headers=tenant).json()["idp"]["id"]
        r = client.patch(f"{IDPS}/{idp_id}", json={"protocol": "saml"}, headers=tenant)
        assert r.status_code == 422
        assert _row(h, idp_id).protocol == "oidc"

    def test_the_metadata_address_guard_still_covers_every_oidc_url(self, h, community):
        _app, client, tenant = community
        for field in OIDC_PROTOCOL.url_fields:
            r = client.post(
                IDPS, json=_oidc_body(**{field: "http://169.254.169.254/latest/meta-data"}), headers=tenant
            )
            assert r.status_code == 400, (field, r.text)
            assert "outbound destination policy" in r.json()["detail"]


def _no_fetch(monkeypatch) -> list:
    """Make every outgoing HTTP client record itself and fail: the list
    stays empty when nothing tried to fetch."""
    import httpx

    made: list = []

    class Recorded:
        def __init__(self, *a, **k):
            made.append((a, k))
            raise ConnectionRefusedError("no fetch in this test")

    monkeypatch.setattr(httpx, "AsyncClient", Recorded)
    monkeypatch.setattr(httpx, "Client", Recorded)
    return made


class TestTheFetchRecorderWorks:
    def test_testing_a_registered_row_does_fetch(self, h, community, monkeypatch):
        """So that an empty list below means something."""
        fetched = _no_fetch(monkeypatch)
        _app, client, tenant = community
        idp_id = _stored(h, "oidc")
        client.post(f"{IDPS}/{idp_id}/test", headers=tenant)
        assert fetched != []


@pytest.mark.parametrize("protocol", ["saml", None, "ldap", "OIDC"])
class TestCommunityMeetsAStoredRowOfAnotherProtocol:
    """Shown, and nothing else. `OIDC` in capitals is not `oidc`: an exact,
    registered name or nothing."""

    def test_it_is_listed_and_can_be_read(self, h, community, protocol):
        _app, client, tenant = community
        idp_id = _stored(h, protocol)
        # (`IdPServer.to_dict` shows a row with no protocol as "oidc"; the
        # router decides on the column, which is what is refused below.)
        shown = protocol or "oidc"
        listed = client.get(IDPS, headers=tenant).json()["idps"]
        assert listed[idp_id]["protocol"] == shown
        r = client.get(f"{IDPS}/{idp_id}", headers=tenant)
        assert r.status_code == 200
        assert r.json()["idp"]["protocol"] == shown

    def test_reading_it_with_a_reachability_check_fetches_nothing(self, h, community, protocol, monkeypatch):
        fetched = _no_fetch(monkeypatch)
        _app, client, tenant = community
        idp_id = _stored(h, protocol)
        r = client.get(f"{IDPS}/{idp_id}?check_reachability=true", headers=tenant)
        assert r.status_code == 200
        assert r.json()["is_reachable"] is None
        assert fetched == []

    @pytest.mark.parametrize(
        "method,suffix,body",
        [
            ("PATCH", "", {"name": "taken over"}),
            ("PATCH", "", {"protocol": "oidc"}),
            ("PATCH", "", {"well_known_url": "https://x.example/wk", "enabled": False}),
            ("DELETE", "", None),
            ("POST", "/test", None),
            ("POST", "/discover", None),
            ("POST", "/discover?update_config=true", None),
        ],
    )
    def test_every_action_on_it_is_refused_and_it_stays_as_it_was(
        self, h, community, protocol, method, suffix, body, monkeypatch
    ):
        fetched = _no_fetch(monkeypatch)
        _app, client, tenant = community
        idp_id = _stored(h, protocol)
        before = _row(h, idp_id)
        r = client.request(method, f"{IDPS}/{idp_id}{suffix}", json=body, headers=tenant)
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "identity_protocol_unavailable"
        assert detail["protocol"] == protocol
        assert "not available in this edition" in detail["message"]
        assert fetched == []
        after = _row(h, idp_id)
        assert after is not None
        for column in ("name", "protocol", "enabled", "well_known_url", "authorization_endpoint", "updated_at"):
            assert getattr(after, column) == getattr(before, column), column

    def test_another_tenant_still_sees_nothing_of_it(self, h, community, protocol):
        _app, client, _tenant = community
        idp_id = _stored(h, protocol)
        other = h.headers("t2")
        assert client.get(f"{IDPS}/{idp_id}", headers=other).status_code == 404
        assert client.patch(f"{IDPS}/{idp_id}", json={"name": "x"}, headers=other).status_code == 404
        assert client.delete(f"{IDPS}/{idp_id}", headers=other).status_code == 404

    def test_no_oauth_client_is_attached_to_or_registered_against_it(self, h, community, protocol):
        _app, client, tenant = community
        idp_id = _stored(h, protocol)
        r = client.post(CLIENTS, json=_client_body(idp_id), headers=tenant)
        assert r.status_code == 400, r.text
        with no_network():
            r = client.post(
                f"{CLIENTS}/auto-register",
                json={"idp_id": idp_id, "client_name": "x", "redirect_uri": "https://app.example/cb"},
                headers=tenant,
            )
        assert r.status_code == 400, r.text
        r = client.post(
            f"/v1/auth/dcr/t1/{idp_id}/register",
            json={"redirect_uris": ["https://app.example/cb"], "client_name": "x"},
        )
        assert r.status_code == 404
        s = h.Factory()
        try:
            assert s.query(OAuthAppClient).filter(OAuthAppClient.idp_id == idp_id).count() == 0
        finally:
            s.close()


# =============================================================================
# The Community modules name no protocol but their own
# =============================================================================


def _code_names_and_strings(path):
    tree = ast.parse((ROOT / path).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg}
    names |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    prose = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    }
    strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose
    ]
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    return tree, names, strings, imports


@pytest.mark.parametrize(
    "path",
    [
        "app/api/v1/endpoints/admin/idp_servers.py",
        "app/api/v1/endpoints/admin/oauth_app_clients.py",
        "app/api/v1/endpoints/auth/dcr.py",
        "app/services/mcp/server/oauth_discovery_helper.py",
    ],
)
def test_the_community_identity_code_does_not_name_saml(path):
    _tree, names, strings, imports = _code_names_and_strings(path)
    assert not sorted(n for n in names if "saml" in n.lower())
    assert not sorted(s for s in strings if "saml" in s.lower())
    assert not sorted(m for m in imports if "saml" in m or "scim" in m)


def test_the_request_models_declare_the_columns_and_decide_nothing_about_saml():
    """One schema: the models declare every field. What a SAML field
    means, and when it is allowed, is not theirs to say."""
    tree, names, _strings, imports = _code_names_and_strings("app/api/v1/models/admin/idp_server.py")
    assert not sorted(m for m in imports if "saml" in m or "scim" in m)
    declared = {
        n.target.id
        for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    assert not sorted(n for n in names - declared if "saml" in n.lower())
    compared = [
        c.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Compare)
        for c in [n.left, *n.comparators]
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    ]
    assert compared == []
    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not sorted(f for f in functions if "saml" in f.lower() or "target_user_type" in f)




class TestAnUpdateIsCheckedAgainstTheRowsProtocol:
    """An update is checked against the stored row's protocol. `validate_update` is what the admin
    router asks before it writes an update; here on a registry with a second
    protocol made up for the test, so that the rule is held in the Community
    suite too (the Enterprise suite holds it over HTTP with SAML)."""

    @staticmethod
    def _registry(validate=None) -> IdentityProtocolRegistry:
        other = IdentityProtocolSpec(
            name="other", label="Other", fields=frozenset({"other_url", "other_flag"}),
            exclusive_fields=frozenset({"other_url"}), validate=validate,
            foreign_hint="Set protocol='other' to use these.", instead_hint="Use other_url instead.",
        )
        return IdentityProtocolRegistry([OIDC_PROTOCOL, other])

    def test_a_field_of_another_protocol_is_refused(self):
        registry = self._registry()
        with pytest.raises(ValueError, match=r"protocol=oidc rejects Other-only fields: \['other_url'\]"):
            registry.validate_update({"protocol": "oidc"}, {"other_url": "https://x.example"})
        with pytest.raises(ValueError, match=r"protocol=other rejects OIDC-only fields: \['jwks_uri', 'well_known_url'\]"):
            registry.validate_update(
                {"protocol": "other"}, {"well_known_url": "https://x.example", "jwks_uri": "https://x.example/j"}
            )

    def test_the_rows_own_fields_empty_foreign_ones_and_shared_ones_pass(self):
        registry = self._registry()
        registry.validate_update(
            {"protocol": "oidc"},
            {"well_known_url": "https://x.example", "other_url": "", "other_flag": True, "name": "n"},
        )
        registry.validate_update({"protocol": "other"}, {"other_url": "https://x.example", "jwks_uri": None})
        registry.validate_update({"protocol": "oidc"}, {})

    def test_a_switch_is_checked_on_the_row_it_leaves(self):
        registry = self._registry()
        stored = {"protocol": "other", "other_url": "https://x.example", "well_known_url": None}
        with pytest.raises(ValueError, match=r"protocol=oidc rejects Other-only fields: \['other_url'\]"):
            registry.validate_update(stored, {"protocol": "oidc"})
        registry.validate_update(stored, {"protocol": "oidc", "other_url": None})
        with pytest.raises(ValueError, match="protocol must be one of"):
            registry.validate_update(stored, {"protocol": "ldap"})

    def test_a_row_that_already_holds_foreign_fields_can_still_be_edited(self):
        """Only a switch looks at what is stored: a row written before the
        rule existed is not locked until someone clears it."""
        registry = self._registry()
        stored = {"protocol": "oidc", "other_url": "https://x.example"}
        registry.validate_update(stored, {"enabled": False})
        registry.validate_update(stored, {"other_url": None})

    def test_a_protocols_own_rule_sees_the_row_the_update_leaves(self):
        seen = []

        def validate(request, mine):
            seen.append((request.protocol, request.other_url, request.other_flag, mine))
            if request.other_flag and not request.other_url:
                raise ValueError("other_flag needs other_url")

        registry = self._registry(validate)
        registry.validate_update({"protocol": "other", "other_url": "https://x.example"}, {"other_flag": True})
        assert seen == [("other", "https://x.example", True, True)]
        with pytest.raises(ValueError, match="other_flag needs other_url"):
            registry.validate_update({"protocol": "other", "other_flag": True, "other_url": "u"}, {"other_url": ""})

    def test_a_row_of_a_protocol_that_is_not_registered_is_refused(self):
        with pytest.raises(ValueError, match="protocol must be one of"):
            self._registry().validate_update({"protocol": "ldap"}, {"name": "n"})

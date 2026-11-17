# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
SAML single sign-on and SCIM provisioning are not in the first release, in
either edition.

* No SAML route (`/v1/auth/saml/...`), no SCIM route (`/scim/v2/...`) and no
  SCIM token administration (`/v1/admin/scim-tokens`) is mounted or in the
  recorded OpenAPI documents; each answers 404.
* An identity provider of the protocol `saml` is refused with one sentence,
  on create and on update; a request carrying a field of SAML is refused as
  an unknown field, and the response of an identity provider carries none.
* A stored `saml` row is listed and nothing else (409).
* The SAML variables, and their earlier names, refuse the start.
* The feature ids `saml_sso` and `scim` do not exist; no permission of SCIM
  is registered; the SAML library is in no dependency list, image smoke list
  or CI list of this repository.
* The knowledge-base "derive identity mappings from the SSO email" switches
  are gone from the API (they had no caller without SAML).

These hold whatever the edition under test: the file runs in both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.composition.features import ENTERPRISE_FEATURES
from tests.app_factory import create_app

ROOT = Path(__file__).resolve().parents[1]
IDPS = "/v1/admin/idps"
SAML_SENTENCE = "SAML single sign-on is not available in this release; identity providers use the protocol 'oidc'."
GONE_PREFIXES = ("/v1/auth/saml", "/scim/v2", "/v1/admin/scim-tokens")
SAML_VARIABLES = (
    "BRUTOR_SAML_SP_PRIVATE_KEY",
    "BRUTOR_SAML_SP_CERTIFICATE",
    "BRUTOR_SAML_DEFAULT_RETURN_PATH",
    "SAML_SP_PRIVATE_KEY",
    "SAML_SP_CERTIFICATE",
    "SAML_DEFAULT_RETURN_PATH",
)


def _gone(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in GONE_PREFIXES)


@pytest.fixture(scope="module")
def h():
    from tests.admin_endpoint_harness import build_admin_harness

    harness = build_admin_harness([], logger_name="test-saml-scim-off")
    real = create_app()
    harness.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = harness.app.dependency_overrides
    harness.real = real
    harness.loose = TestClient(harness.app, raise_server_exceptions=False)
    yield harness
    harness.close()


class TestNoRoute:
    def test_no_route_of_saml_or_scim_is_mounted(self, h):
        mounted = {getattr(r, "path", "") for r in h.real.routes}
        assert sorted(p for p in mounted if _gone(p) or "saml" in p.lower() or "scim" in p.lower()) == []
        paths = set(h.real.openapi()["paths"])
        assert sorted(p for p in paths if "saml" in p.lower() or "scim" in p.lower()) == []

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/v1/auth/saml/x/metadata"),
            ("GET", "/v1/auth/saml/x/login"),
            ("POST", "/v1/auth/saml/x/acs"),
            ("GET", "/scim/v2/ServiceProviderConfig"),
            ("GET", "/scim/v2/Users"),
            ("POST", "/scim/v2/Users"),
            ("GET", "/scim/v2/Groups"),
            ("GET", "/v1/admin/scim-tokens"),
            ("POST", "/v1/admin/scim-tokens"),
        ],
    )
    def test_each_answers_404(self, h, method, path):
        r = h.loose.request(method, path, headers=h.headers("t1"), follow_redirects=False)
        assert r.status_code == 404, (method, path, r.status_code)

    # The Enterprise document: `enterprise/tests/test_enterprise_openapi_document.py`.
    @pytest.mark.parametrize("document", ["tests/fixtures/openapi_snapshot_community.json"])
    def test_no_recorded_document_offers_them(self, document):
        text = (ROOT / document).read_text()
        doc = json.loads(text)
        assert sorted(p for p in doc["paths"] if "saml" in p.lower() or "scim" in p.lower()) == []
        assert "saml_" not in text
        assert "auto_derive_from_oidc" not in text
        assert "kb_oidc_auto_derive_enabled" not in text


_seq = iter(range(100, 100_000))


def _oidc_body(n: int, **over) -> dict:
    n = next(_seq)
    return {
        "name": f"OIDC off {n}",
        "issuer": f"https://off{n}.example",
        "type": "oidc_discoverable",
        "protocol": "oidc",
        "enabled": True,
        "well_known_url": f"https://off{n}.example/.well-known/openid-configuration",
        **over,
    }


class TestTheProtocolIsRefused:
    def test_a_saml_provider_is_refused_with_the_sentence(self, h):
        r = h.client.post(IDPS, json=_oidc_body(1, protocol="saml", well_known_url=None), headers=h.headers("t1"))
        assert r.status_code == 422, r.text
        assert SAML_SENTENCE in r.text

    def test_an_update_to_saml_is_refused_with_the_sentence(self, h):
        r = h.client.post(IDPS, json=_oidc_body(2), headers=h.headers("t1"))
        assert r.status_code == 201, r.text
        idp_id = r.json()["idp"]["id"]
        r = h.client.patch(f"{IDPS}/{idp_id}", json={"protocol": "saml"}, headers=h.headers("t1"))
        assert r.status_code == 422, r.text
        assert SAML_SENTENCE in r.text

    @pytest.mark.parametrize(
        "field,value",
        [
            ("saml_metadata_url", ""),
            ("saml_idp_sso_url", "https://x.example/sso"),
            ("saml_want_assertions_signed", True),
            ("target_user_type", "end_user"),
            ("trust_saml_email_unverified", False),
        ],
    )
    def test_a_field_of_saml_is_an_unknown_field(self, h, field, value):
        r = h.client.post(IDPS, json=_oidc_body(3, **{field: value}), headers=h.headers("t1"))
        assert r.status_code == 422, r.text
        assert field in r.text
        r = h.client.post(IDPS, json=_oidc_body(4), headers=h.headers("t1"))
        idp_id = r.json()["idp"]["id"]
        r = h.client.patch(f"{IDPS}/{idp_id}", json={field: value}, headers=h.headers("t1"))
        assert r.status_code == 422, r.text

    def test_the_response_carries_no_field_of_saml(self, h):
        r = h.client.post(IDPS, json=_oidc_body(5), headers=h.headers("t1"))
        idp = r.json()["idp"]
        assert sorted(k for k in idp if "saml" in k or k == "target_user_type") == []

    def test_a_stored_saml_row_is_listed_and_nothing_else(self, h):
        from app.database.models.idp_server import IdPServer

        def add(s):
            row = IdPServer(id="idp-off-saml", tenant_id="t1", name="Stored SAML", issuer="https://stored-off.example",
                            type="generic_saml", enabled=True)
            s.add(row)
            s.flush()
            row.protocol = "saml"

        h.seed(add)
        assert "idp-off-saml" in h.client.get(IDPS, headers=h.headers("t1")).json()["idps"]
        for method, suffix, body in (("PATCH", "", {"name": "x"}), ("DELETE", "", None), ("POST", "/test", None)):
            r = h.loose.request(method, f"{IDPS}/idp-off-saml{suffix}", json=body, headers=h.headers("t1"))
            assert r.status_code == 409, (method, r.text)
            assert r.json()["detail"]["error"] == "identity_protocol_unavailable"


@pytest.mark.parametrize("name", SAML_VARIABLES)
def test_a_saml_variable_is_not_read(monkeypatch, name):
    """No retired name is kept: a SAML variable is not registered, so
    nothing reads it, and it does not stop the start."""
    from app.env_registry import REGISTRY

    monkeypatch.setenv(name, "value-that-must-not-leak")
    for enterprise in (False, True):
        view = REGISTRY.view(enterprise=enterprise)
        assert view.find(name) is None and view.problems(missing=False) == []


def test_the_feature_ids_do_not_exist():
    assert "saml_sso" not in ENTERPRISE_FEATURES
    assert "scim" not in ENTERPRISE_FEATURES


def test_no_permission_of_scim_is_registered():
    from tests.app_factory import every_permission

    assert [p for p in every_permission() if "scim" in str(p).lower()] == []


def test_the_saml_library_is_in_no_list_of_this_repository():
    from tests import workflows

    for path in (
        ROOT / "pyproject.toml",
        ROOT / "tests/packaging.py",
        ROOT / "tests/test_packaging.py",
        ROOT / "tests/edition_boundary.py",
        workflows.workflow_path("ci.yml"),
        ROOT / "scripts/smoke_images.sh",
    ):
        text = path.read_text().lower()
        assert "onelogin" not in text and "python3-saml" not in text and "xmlsec" not in text, path


def test_the_sso_identity_derivation_fields_are_gone():
    from app.api.v1.models.admin.tenant_settings import TenantSettingsUpdate

    assert "kb_oidc_auto_derive_enabled" not in TenantSettingsUpdate.model_fields

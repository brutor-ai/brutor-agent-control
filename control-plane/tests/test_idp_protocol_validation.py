# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# IdP request models: the protocol and its fields

The identity provider request models take the protocol `oidc` (OIDC and
plain OAuth 2.0), the only one in this release, and refuse any other: SAML
single sign-on is not in this release, so `protocol: "saml"` is refused
with its own sentence, and a field of SAML is an unknown field.

Tests cover:
  1. Valid OIDC payloads accepted.
  2. A SAML field is refused as an unknown field, on create and update.
  3. Invalid protocol value rejected; `saml` with its sentence.
  4. Update model accepts None for protocol (partial patches).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from pydantic import ValidationError

from app.api.v1.models.admin.idp_server import (
    IdPConfigRequest,
    IdPConfigUpdateRequest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _oidc_kwargs(**overrides):
    """Smallest valid OIDC payload. Tests override individual fields."""
    base = dict(
        name="Google",
        issuer="https://accounts.google.com",
        type="oidc_discoverable",
        protocol="oidc",
        enabled=True,
        well_known_url="https://accounts.google.com/.well-known/openid-configuration",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_oidc_minimal_payload_validates(self):
        m = IdPConfigRequest(**_oidc_kwargs())
        assert m.protocol == "oidc"
        assert m.well_known_url is not None

    def test_oidc_with_manual_endpoints_validates(self):
        m = IdPConfigRequest(**_oidc_kwargs(
            type="oidc",
            well_known_url=None,
            authorization_endpoint="https://idp.example/oauth2/authorize",
            token_endpoint="https://idp.example/oauth2/token",
            jwks_uri="https://idp.example/.well-known/jwks.json",
        ))
        assert m.token_endpoint == "https://idp.example/oauth2/token"


# ---------------------------------------------------------------------------
# 2. A SAML field is an unknown field
# ---------------------------------------------------------------------------


class TestSamlFieldsAreUnknown:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("saml_metadata_url", "https://idp/saml/metadata"),
            ("saml_force_authn", False),
            ("saml_want_assertions_signed", True),
            ("target_user_type", "end_user"),
            ("trust_saml_email_unverified", False),
        ],
    )
    def test_refused_on_create_and_update(self, field, value):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            IdPConfigRequest(**_oidc_kwargs(**{field: value}))
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            IdPConfigUpdateRequest(**{field: value})


# ---------------------------------------------------------------------------
# 5. Invalid protocol value
# ---------------------------------------------------------------------------


class TestInvalidProtocolValue:
    @pytest.mark.parametrize("bad", ["OIDC", "SAML", "saml2", "ldap", ""])
    def test_invalid_protocol_rejected(self, bad):
        with pytest.raises(ValidationError) as exc:
            IdPConfigRequest(**_oidc_kwargs(protocol=bad))
        assert "protocol must be one of" in str(exc.value)

    def test_saml_is_refused_with_its_sentence(self):
        sentence = "SAML single sign-on is not available in this release; identity providers use the protocol 'oidc'."
        with pytest.raises(ValidationError, match=sentence.replace(".", r"\.").replace("(", r"\(")):
            IdPConfigRequest(**_oidc_kwargs(protocol="saml"))
        with pytest.raises(ValidationError, match="SAML single sign-on is not available in this release"):
            IdPConfigUpdateRequest(protocol="saml")


# ---------------------------------------------------------------------------
# 6. Update model: protocol Optional + partial-patch behaviour
# ---------------------------------------------------------------------------


class TestUpdateRequest:
    """The update model allows partial patches (every field Optional).
    Protocol can be None (operator didn't touch it). Each field can
    be patched individually."""

    def test_update_with_only_name(self):
        m = IdPConfigUpdateRequest(name="Renamed IdP")
        assert m.name == "Renamed IdP"
        assert m.protocol is None


    def test_update_protocol_validated_when_supplied(self):
        with pytest.raises(ValidationError):
            IdPConfigUpdateRequest(protocol="garbage")

    def test_update_protocol_none_allowed(self):
        # Operator patching unrelated fields shouldn't have to send
        # protocol.
        m = IdPConfigUpdateRequest(enabled=False)
        assert m.protocol is None


# ---------------------------------------------------------------------------
# SAML OAuth-flag normalisation
# ---------------------------------------------------------------------------


class TestOauthFlags:
    """The OAuth flags of an OIDC request are kept as sent."""


    def test_oidc_request_preserves_pkce_true(self):
        # Regression: the normaliser must NOT touch OIDC rows.
        m = IdPConfigRequest(**_oidc_kwargs(supports_pkce=True))
        assert m.supports_pkce is True


    def test_update_oidc_protocol_preserves_pkce_true(self):
        m = IdPConfigUpdateRequest(protocol="oidc", supports_pkce=True)
        assert m.supports_pkce is True

    def test_update_without_protocol_does_not_touch_flags(self):
        # Operator patching an unrelated field shouldn't have their
        # PKCE flag silently rewritten — they didn't set protocol.
        m = IdPConfigUpdateRequest(supports_pkce=True)
        assert m.supports_pkce is True

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# RFC 7591 inbound DCR endpoint

Locks down the contract for `POST /v1/auth/dcr/{tenant_id}/{idp_id}/register`:

  1. Happy path returns RFC 7591 §3 response shape with
     non-empty client_id + client_secret.
  2. The persisted `oauth_app_clients` row carries
     `auto_registered=True` (the load-bearing audit signal).
  3. The persisted secret is the ENCRYPTED ciphertext — the
     plaintext never lands on disk.
  4. Unknown tenant + IdP combination → 404 RFC 7591 error
     envelope (not a stack trace).
  5. Disabled IdP → 404 same shape (operators opt out of inbound
     DCR by disabling the IdP row).
  6. Per-tenant quota is honoured (returns 429 when exceeded).
  7. Invalid redirect_uris (fragment, non-http) → 422 on Pydantic
     validation.

These tests use FastAPI's TestClient + an in-memory SQLite. The
EncryptionService stub returns "ENCRYPTED:<plaintext>" so we can
assert ciphertext-vs-plaintext without depending on real Fernet.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.auth.dcr import (
    MAX_AUTO_REGISTERED_CLIENTS_PER_TENANT,
    create_dcr_router,
)
from app.database import Base, get_db
from app.database.models.idp_server import IdPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.tenant import Tenant


class _StubEncryptionService:
    def encrypt(self, plaintext: str) -> str:
        return f"ENCRYPTED:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        if ciphertext.startswith("ENCRYPTED:"):
            return ciphertext[len("ENCRYPTED:"):]
        raise ValueError("stub can't decrypt non-stub ciphertext")


@pytest.fixture
def app_and_db():
    """Build a minimal FastAPI app with just the DCR router + an
    in-memory SQLite. Tenant + IdP + oauth_app_clients tables only;
    the rest of the schema is irrelevant for this endpoint."""
    # TestClient runs requests on a worker thread; the default
    # in-memory SQLite is thread-bound. StaticPool + the
    # check_same_thread escape hatch shares one connection across
    # the test thread and TestClient's worker thread, which is what
    # FastAPI's documented test pattern recommends.
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[
            Tenant.__table__,
            IdPServer.__table__,
            OAuthAppClient.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, future=True)

    # Seed a tenant + IdP
    seed = Session()
    seed.add(Tenant(
        id="t1", name="t1", display_name="t1",
        primary_contact_email="t1@example.invalid",
    ))
    seed.add(IdPServer(
        id="idp-1", tenant_id="t1",
        name="Test IdP",
        issuer="https://idp.example.invalid",
        type="oidc_discoverable",
        protocol="oidc",
        enabled=True,
    ))
    seed.add(IdPServer(
        id="idp-disabled", tenant_id="t1",
        name="Disabled IdP",
        issuer="https://disabled.example.invalid",
        type="oidc_discoverable",
        protocol="oidc",
        enabled=False,
    ))
    seed.commit()
    seed.close()

    app = FastAPI()
    encryption = _StubEncryptionService()
    router = create_dcr_router(encryption_service=encryption)
    app.include_router(router, prefix="/v1")

    # Override the DB dep so each request gets a session bound to
    # our in-memory engine.
    def _override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()
    app.dependency_overrides[get_db] = _override_get_db

    yield TestClient(app), Session
    engine.dispose()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_register_returns_rfc7591_response_shape(self, app_and_db):
        client, _ = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={
                "redirect_uris": ["https://myclient.example.com/callback"],
                "client_name": "My MCP Client",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Required RFC 7591 §3 fields
        assert body["client_id"].startswith("app-")
        assert len(body["client_secret"]) >= 32
        assert body["client_secret_expires_at"] == 0
        assert body["client_id_issued_at"] > 0
        # Echoed-back request fields (RFC 7591 §3.2.1)
        assert body["redirect_uris"] == ["https://myclient.example.com/callback"]
        assert body["client_name"] == "My MCP Client"
        # Defaults applied when caller didn't supply
        assert body["grant_types"] == ["authorization_code", "refresh_token"]
        assert body["response_types"] == ["code"]

    def test_persisted_row_marked_auto_registered(self, app_and_db):
        # The load-bearing audit signal — admins use this column to
        # find self-registered clients and revoke them.
        client, Session = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={
                "redirect_uris": ["https://x.example.com/cb"],
                "client_name": "X",
            },
        )
        client_id = resp.json()["client_id"]
        db = Session()
        try:
            row = (
                db.query(OAuthAppClient)
                .filter(OAuthAppClient.client_id == client_id)
                .first()
            )
            assert row is not None
            assert row.auto_registered is True
            assert row.tenant_id == "t1"
            assert row.idp_id == "idp-1"
        finally:
            db.close()

    def test_persisted_secret_is_encrypted_not_plaintext(self, app_and_db):
        # The plaintext secret only appears in the HTTP response.
        # The DB row stores the ciphertext. A regression here would
        # leak credentials at rest.
        client, Session = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={"redirect_uris": ["https://x.example.com/cb"]},
        )
        plaintext = resp.json()["client_secret"]
        client_id = resp.json()["client_id"]

        db = Session()
        try:
            row = (
                db.query(OAuthAppClient)
                .filter(OAuthAppClient.client_id == client_id)
                .first()
            )
            # Stub encryption is `ENCRYPTED:<plaintext>`. The real
            # Fernet primitive produces a different ciphertext —
            # what matters is that the column is wrapped, not raw
            # plaintext. The stub assertion confirms the encrypt
            # path was actually called.
            assert row.encrypted_client_secret == f"ENCRYPTED:{plaintext}"
            assert row.encrypted_client_secret.startswith("ENCRYPTED:")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 2. Validation + error responses
# ---------------------------------------------------------------------------


class TestErrorResponses:
    def test_unknown_idp_returns_404_rfc7591_envelope(self, app_and_db):
        client, _ = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-nonexistent/register",
            json={"redirect_uris": ["https://x.example.com/cb"]},
        )
        assert resp.status_code == 404
        # RFC 7591 envelope nested in FastAPI's `detail`.
        detail = resp.json()["detail"]
        assert detail["error"] == "invalid_client_metadata"
        assert "not found" in detail["error_description"].lower()

    def test_disabled_idp_returns_404(self, app_and_db):
        # Operator opt-out: disable the IdP to stop accepting DCR.
        client, _ = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-disabled/register",
            json={"redirect_uris": ["https://x.example.com/cb"]},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "disabled" in detail["error_description"].lower()

    def test_redirect_uri_with_fragment_rejected(self, app_and_db):
        client, _ = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={"redirect_uris": ["https://x.example.com/cb#fragment"]},
        )
        # Pydantic validation → 422
        assert resp.status_code == 422

    def test_non_http_redirect_uri_rejected(self, app_and_db):
        client, _ = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={"redirect_uris": ["javascript:alert(1)"]},
        )
        assert resp.status_code == 422

    def test_localhost_redirect_uri_allowed(self, app_and_db):
        # Desktop / CLI clients need http://localhost. Don't block.
        client, _ = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={"redirect_uris": ["http://localhost:8080/cb"]},
        )
        assert resp.status_code == 200

    def test_empty_redirect_uris_rejected(self, app_and_db):
        client, _ = app_and_db
        resp = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={"redirect_uris": []},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Per-tenant quota
# ---------------------------------------------------------------------------


class TestQuota:
    def test_quota_exceeded_returns_429(self, app_and_db, monkeypatch):
        # Patch the quota to a small value so we can exercise it
        # without registering 1000 clients. We use monkeypatch so
        # the original constant is restored after the test.
        monkeypatch.setattr(
            "app.api.v1.endpoints.auth.dcr.MAX_AUTO_REGISTERED_CLIENTS_PER_TENANT",
            3,
        )
        client, _ = app_and_db
        for i in range(3):
            r = client.post(
                "/v1/auth/dcr/t1/idp-1/register",
                json={
                    "redirect_uris": [f"https://x{i}.example.com/cb"],
                    "client_name": f"Client {i}",
                },
            )
            assert r.status_code == 200, r.text

        # The 4th request hits the cap.
        r = client.post(
            "/v1/auth/dcr/t1/idp-1/register",
            json={
                "redirect_uris": ["https://x-quota.example.com/cb"],
                "client_name": "Over quota",
            },
        )
        assert r.status_code == 429
        detail = r.json()["detail"]
        assert "quota" in detail["error_description"].lower()

    def test_quota_default_is_sensible(self):
        # If the quota constant is accidentally reduced to ~1 in
        # a future refactor, every customer's CI breaks. Lock the
        # floor at 100 — fewer than 100 self-registered clients is
        # not a realistic enterprise scenario.
        assert MAX_AUTO_REGISTERED_CLIENTS_PER_TENANT >= 100

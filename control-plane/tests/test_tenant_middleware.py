# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
TenantMiddleware runs on every control-plane request. The order it trusts sources
in is the isolation boundary: a signed tenant must beat one anyone can type into
a header, and a tenant that is unknown, suspended or inactive must stop the
request before a handler runs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app.middleware.tenant as tenant_middleware
from app.database.models.api_key import APIKey
from app.database.models.tenant import Tenant
from app.middleware.tenant import TenantMiddleware
from app.services import auth_read_model
from app.services.tenant.tenant_service import TenantContext
from tests.admin_endpoint_harness import JWT_SECRET, build_admin_harness


def _token(secret: str = JWT_SECRET, **claims) -> dict:
    claims.setdefault("exp", int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()))
    return {"Authorization": f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"}


@pytest.fixture(scope="module")
def db_harness():
    harness = build_admin_harness([], extra_tenants=("t-suspended", "t-inactive"))

    def seed(s):
        s.get(Tenant, "t-suspended").suspended = True
        s.get(Tenant, "t-suspended").suspension_reason = "unpaid"
        s.get(Tenant, "t-inactive").is_active = False
        s.get(Tenant, "t2").is_default = True

    harness.seed(seed)
    yield harness
    harness.close()


class Sessions:
    """Stands in for get_db, and records whether each session it handed out was closed."""

    def __init__(self, factory):
        self.factory = factory
        self.opened = []

    def __call__(self):
        session = self.factory()
        closed = {"value": False}
        original = session.close

        def close():
            closed["value"] = True
            original()

        session.close = close
        self.opened.append(closed)
        yield session

    @property
    def all_closed(self) -> bool:
        return all(c["value"] for c in self.opened)


@pytest.fixture()
def sessions(db_harness, monkeypatch):
    s = Sessions(db_harness.Factory)
    monkeypatch.setattr(tenant_middleware, "get_db", s)
    auth_read_model.clear_all()
    yield s
    auth_read_model.clear_all()


@pytest.fixture()
def client(sessions):
    app = FastAPI()
    app.add_middleware(TenantMiddleware)
    app.state.handled = []

    def echo(request: Request):
        app.state.handled.append(request.url.path)
        return {
            "state": getattr(request.state, "tenant_id", None),
            "source": getattr(request.state, "tenant_source", None),
            "context": TenantContext.get_tenant_id(),
        }

    for path in ("/v1/echo", "/health", "/v1/thing/icon", "/v1/public/tenants/x/theme", "/v1/admin/tenants/x"):
        app.add_api_route(path, echo)

    @app.get("/v1/boom")
    def boom():
        raise RuntimeError("handler failed")

    with TestClient(app, raise_server_exceptions=False) as c:
        c.handled = app.state.handled
        yield c


class TestResolutionOrder:
    def test_a_signed_tenant_beats_the_header(self, client):
        r = client.get("/v1/echo", headers={**_token(tenant_id="t1", admin_type="tenant"), "X-Tenant-ID": "t2"})
        assert r.status_code == 200, r.text
        assert r.json() == {"state": "t1", "source": "jwt", "context": "t1"}
        assert r.headers["X-Tenant-ID"] == "t1"

    def test_a_forged_token_cannot_name_a_tenant(self, client):
        r = client.get("/v1/echo", headers={**_token(secret="wrong", tenant_id="t1"), "X-Tenant-ID": "t2"})
        assert r.json()["state"] == "t2" and r.json()["source"] == "header"

    def test_an_expired_token_cannot_name_a_tenant(self, client):
        headers = _token(tenant_id="t1", exp=int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()))
        r = client.get("/v1/echo", headers={**headers, "X-Tenant-ID": "t2"})
        assert r.json()["state"] == "t2"

    def test_a_system_admin_names_the_tenant_by_header(self, client):
        r = client.get("/v1/echo", headers={**_token(admin_type="system"), "X-Tenant-ID": "t1"})
        assert r.json() == {"state": "t1", "source": "header", "context": "t1"}

    def test_no_credential_and_no_header_is_the_default_tenant(self, client):
        r = client.get("/v1/echo")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "t2"
        assert r.headers["X-Tenant-Source"] == r.json()["source"]

    def test_an_api_key_names_its_tenant_and_beats_the_header(self, client, db_harness):
        raw = "sk_brutor_api_middleware_test_key"

        def seed(s):
            if s.get(APIKey, "key-mw") is None:
                s.add(
                    APIKey(
                        id="key-mw", tenant_id="t1", name="mw", key_hash=hashlib.sha256(raw.encode()).hexdigest(),
                        key_prefix=raw[:12], scope="tenant", enabled=True, revoked=False,
                    )
                )

        db_harness.seed(seed)
        r = client.get("/v1/echo", headers={"Authorization": f"Bearer {raw}", "X-Tenant-ID": "t2"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "t1" and r.json()["source"] == "api_key"

    def test_a_revoked_api_key_cannot_name_a_tenant(self, client, db_harness):
        raw = "sk_brutor_api_middleware_revoked_key"

        def seed(s):
            if s.get(APIKey, "key-rev") is None:
                s.add(
                    APIKey(
                        id="key-rev", tenant_id="t1", name="rev", key_hash=hashlib.sha256(raw.encode()).hexdigest(),
                        key_prefix=raw[:12], scope="tenant", enabled=True, revoked=True,
                    )
                )

        db_harness.seed(seed)
        r = client.get("/v1/echo", headers={"Authorization": f"Bearer {raw}", "X-Tenant-ID": "t2"})
        assert r.json()["state"] == "t2" and r.json()["source"] == "header"


class TestRefusal:
    @pytest.mark.parametrize(
        "tenant,code", [("nope", 404), ("t-suspended", 403), ("t-inactive", 403)]
    )
    def test_a_bad_tenant_stops_the_request_before_the_handler(self, client, sessions, tenant, code):
        r = client.get("/v1/echo", headers={"X-Tenant-ID": tenant})
        assert r.status_code == code
        assert "detail" in r.json()
        assert client.handled == []
        assert sessions.all_closed
        assert TenantContext.get_tenant_id() is None

    def test_a_signed_token_for_a_suspended_tenant_is_refused(self, client):
        r = client.get("/v1/echo", headers=_token(tenant_id="t-suspended", admin_type="tenant"))
        assert r.status_code == 403
        assert client.handled == []

    def test_a_tenant_suspended_after_it_was_vouched_for_is_refused_at_once(self, client, db_harness):
        """The read model caches "active"; the ORM event must clear it, or a
        suspension would not bite for the whole TTL."""
        headers = {"X-Tenant-ID": "t1"}
        assert client.get("/v1/echo", headers=headers).status_code == 200
        assert client.get("/v1/echo", headers=headers).status_code == 200

        def suspend(s):
            s.get(Tenant, "t1").suspended = True

        def restore(s):
            s.get(Tenant, "t1").suspended = False

        db_harness.seed(suspend)
        try:
            assert client.get("/v1/echo", headers=headers).status_code == 403
        finally:
            db_harness.seed(restore)

    def test_a_setup_failure_is_500_without_the_detail_and_runs_no_handler(self, client, monkeypatch):
        def explode(_db, _tenant_id):
            raise RuntimeError("database credentials: hunter2")

        monkeypatch.setattr(tenant_middleware.TenantService, "validate_tenant_cached", explode)
        r = client.get("/v1/echo", headers={"X-Tenant-ID": "t1"})
        assert r.status_code == 500
        assert r.json() == {"detail": "Error processing tenant context"}
        assert client.handled == []

    def test_a_handler_failure_still_closes_the_session_and_clears_the_context(self, client, sessions):
        r = client.get("/v1/boom", headers={"X-Tenant-ID": "t1"})
        assert r.status_code == 500
        assert sessions.all_closed
        assert TenantContext.get_tenant_id() is None


class TestSuccessCleanup:
    def test_the_session_is_closed_and_the_context_cleared_afterwards(self, client, sessions):
        assert client.get("/v1/echo", headers={"X-Tenant-ID": "t1"}).status_code == 200
        assert sessions.opened and sessions.all_closed
        assert TenantContext.get_tenant_id() is None


class TestExcludedPaths:
    @pytest.mark.parametrize("path", ["/health", "/v1/public/tenants/x/theme", "/v1/admin/tenants/x", "/v1/thing/icon"])
    def test_public_paths_skip_tenant_resolution(self, client, sessions, path):
        r = client.get(path, headers={"X-Tenant-ID": "nope"})
        assert r.status_code == 200
        assert r.json()["state"] is None
        assert sessions.opened == []
        assert "X-Tenant-ID" not in r.headers

    def test_a_guarded_path_with_the_same_header_is_refused(self, client):
        assert client.get("/v1/echo", headers={"X-Tenant-ID": "nope"}).status_code == 404



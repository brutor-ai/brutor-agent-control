# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Accepted audiences for workload-identity tokens

The core authenticates an agent by a JWT its IdP issued. It
requires the token's `aud` to be one of `idp_servers.workload_audiences`;
an IdP with none authenticates no workloads. These tests pin the admin API
half: the field validates, round-trips through create / update / get, and
an omitted value reads back as `[]` (nothing accepted), never as "any".
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.idp_servers import create_idp_admin_router
from app.api.v1.models.admin.idp_server import (
    IdPConfigRequest,
    IdPConfigUpdateRequest,
)
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.tenant import Tenant
from tests.test_end_user_groups_endpoints import (
    REQUIRED_TABLES,
    _create_admin_role_with_all_perms,
)

JWT_SECRET = os.environ["JWT_SECRET"]


def _oidc(**overrides):
    base = dict(
        name="Agents IdP",
        issuer="https://idp.example.com",
        type="manual",
        protocol="oidc",
        jwks_uri="https://idp.example.com/jwks.json",
        enabled=True,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_entries_are_trimmed_and_deduplicated_in_order(self):
        req = IdPConfigRequest(
            **_oidc(workload_audiences=[" brutor-gateway ", "api://x", "brutor-gateway"])
        )
        assert req.workload_audiences == ["brutor-gateway", "api://x"]

    @pytest.mark.parametrize("bad", [[""], ["   "], ["ok", ""]])
    def test_an_empty_entry_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            IdPConfigRequest(**_oidc(workload_audiences=bad))
        with pytest.raises(ValidationError):
            IdPConfigUpdateRequest(workload_audiences=bad)

    def test_too_many_entries_are_rejected(self):
        with pytest.raises(ValidationError):
            IdPConfigRequest(
                **_oidc(workload_audiences=[f"aud-{i}" for i in range(21)])
            )

    def test_omitted_is_none_on_create_and_unset_on_update(self):
        assert IdPConfigRequest(**_oidc()).workload_audiences is None
        update = IdPConfigUpdateRequest(name="x")
        assert "workload_audiences" not in update.model_dump(exclude_unset=True)


# ---------------------------------------------------------------------------
# Endpoint round-trip
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        s = SessionFactory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app = FastAPI()
    logger = BrutorLogger("test")
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_idp_admin_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        setup.add(Tenant(
            id="t1", name="t1", display_name="Tenant One",
            primary_contact_email="admin@t1.example",
            is_active=True, is_default=False, is_trial=False,
            suspended=False, billing_plan="self_hosted",
        ))
        setup.flush()
        role = _create_admin_role_with_all_perms(setup, "t1")
        auth.create_tenant_admin(
            username="admin1", email="admin1@t1.example",
            password="Secret123!", tenant_id="t1", db=setup, role_id=role.id,
        )
        setup.commit()
    finally:
        setup.close()

    c = TestClient(app)
    r = c.post(
        "/v1/admin/users/tenant/login",
        json={"username": "admin1", "password": "Secret123!", "tenant_id": "t1"},
    )
    assert r.status_code == 200, r.text
    c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    yield c
    app.dependency_overrides.clear()
    engine.dispose()


def test_workload_audiences_round_trip_through_create_update_and_get(client):
    r = client.post(
        "/v1/admin/idps",
        json=_oidc(workload_audiences=["brutor-gateway", " api://agents "]),
    )
    assert r.status_code == 201, r.text
    idp = r.json()["idp"]
    assert idp["workload_audiences"] == ["brutor-gateway", "api://agents"]

    r = client.patch(
        f"/v1/admin/idps/{idp['id']}", json={"workload_audiences": ["only-this"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["idp"]["workload_audiences"] == ["only-this"]

    r = client.get(f"/v1/admin/idps/{idp['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["idp"]["workload_audiences"] == ["only-this"]

    # Clearing it means: this IdP authenticates no workloads.
    r = client.patch(f"/v1/admin/idps/{idp['id']}", json={"workload_audiences": []})
    assert r.status_code == 200, r.text
    assert r.json()["idp"]["workload_audiences"] == []


def test_an_idp_created_without_audiences_reads_back_as_none_accepted(client):
    r = client.post("/v1/admin/idps", json=_oidc())
    assert r.status_code == 201, r.text
    assert r.json()["idp"]["workload_audiences"] == []


def test_an_empty_audience_is_rejected_by_the_api(client):
    r = client.post("/v1/admin/idps", json=_oidc(workload_audiences=[""]))
    assert r.status_code == 422, r.text

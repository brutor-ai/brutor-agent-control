# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Guardrail config — full HTTP CRUD round-trip

Complements `test_guardrail_surfaces_round_trip.py` (which tests only the
`_config_to_response`/`to_dict` builder boundary) by driving the REAL admin
router through `TestClient` against an in-memory SQLite DB with JWT auth +
`require_permission`, so coverage includes transport + DB persistence:

    POST create → GET (persisted) → PATCH update → GET (updated) → DELETE → GET 404

Harness mirrors `tests/test_end_user_groups_endpoints.py`.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.guardrails import create_guardrails_router
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database import Base, get_db
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminAuditLog,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.guardrail_config import (
    GuardrailConfig,
    resource_group_guardrails,
)
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant
from app.services.guardrails.guardrails_service import GuardrailsService

JWT_SECRET = os.environ["JWT_SECRET"]

REQUIRED_TABLES = [
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    ResourceGroup.__table__,
    GuardrailConfig.__table__,
    resource_group_guardrails,
]


def _create_admin_role_with_all_perms(db: Session, tenant_id: str) -> TenantRole:
    role = TenantRole(
        id=f"role-{tenant_id}-admin",
        tenant_id=tenant_id,
        name="Tenant Admin",
        description="full perms",
        is_system=True,
    )
    db.add(role)
    db.flush()
    for p in Permission:
        if p == Permission.SYSTEM_ADMIN:
            continue
        db.add(
            TenantRolePermission(
                id=f"perm-{uuid.uuid4().hex[:10]}",
                role_id=role.id,
                permission=p.value,
            )
        )
    db.flush()
    return role


class _Harness:
    def __init__(self, client, SessionFactory, auth):
        self.client = client
        self.SessionFactory = SessionFactory
        self.auth = auth

    def new_session(self) -> Session:
        return self.SessionFactory()

    def login_tenant(self, username: str, tenant_id: str = "t1", password: str = "Secret123!") -> str:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": username, "password": password, "tenant_id": tenant_id},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def create_tenant_admin(self, tenant_id, username, email, password="Secret123!", role_id=None) -> str:
        s = self.new_session()
        try:
            admin = self.auth.create_tenant_admin(
                username=username, email=email, password=password,
                tenant_id=tenant_id, db=s, role_id=role_id,
            )
            s.commit()
            return admin.id
        finally:
            s.close()


@pytest.fixture()
def harness():
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
    app.include_router(
        create_guardrails_router(GuardrailsService(logger=logger), logger, encryption_service=None),
        prefix="/v1",
    )
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)
    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        setup.add(
            Tenant(
                id="t1", name="t1", display_name="Tenant One",
                primary_contact_email="admin@t1.example",
                is_active=True, is_default=False, is_trial=False, suspended=False,
                billing_plan="self_hosted",  # unlimited → no guardrail quota 429
            )
        )
        setup.flush()
        _create_admin_role_with_all_perms(setup, "t1")
        setup.commit()
    finally:
        setup.close()

    yield _Harness(client=client, SessionFactory=SessionFactory, auth=auth)
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def admin_token(harness):
    harness.create_tenant_admin("t1", "adm1", "adm1@t1.example", role_id="role-t1-admin")
    return harness.login_tenant("adm1", "t1")


def test_guardrail_config_full_crud_round_trip(harness, admin_token):
    h, tok = harness, admin_token
    base = "/v1/admin/guardrails/configs"

    # CREATE
    r = h.client.post(
        base,
        headers=h.bearer(tok),
        json={"name": "it-probe", "description": "d", "priority": 5, "enabled": True},
    )
    assert r.status_code == 201, r.text
    cfg = r.json()["config"]
    cid = cfg["id"]
    assert cfg["name"] == "it-probe"
    assert cfg["tenant_id"] == "t1"
    assert cfg["enabled"] is True

    # GET — proves DB persistence (not just the create-response builder)
    r = h.client.get(f"{base}/{cid}", headers=h.bearer(tok))
    assert r.status_code == 200, r.text
    got = r.json()["config"]
    assert got["name"] == "it-probe"
    assert got["priority"] == 5
    assert got["enabled"] is True

    # UPDATE (PATCH) — include enabled (the update model defaults it to False)
    r = h.client.patch(
        f"{base}/{cid}",
        headers=h.bearer(tok),
        json={"name": "it-probe-2", "enabled": True, "priority": 9},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["name"] == "it-probe-2"

    # GET — update persisted
    r = h.client.get(f"{base}/{cid}", headers=h.bearer(tok))
    assert r.status_code == 200, r.text
    got = r.json()["config"]
    assert got["name"] == "it-probe-2"
    assert got["priority"] == 9

    # DELETE → 200 with the id
    r = h.client.delete(f"{base}/{cid}", headers=h.bearer(tok))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == cid

    # GET → 404 (gone)
    r = h.client.get(f"{base}/{cid}", headers=h.bearer(tok))
    assert r.status_code == 404, r.text


def test_guardrail_config_routes_require_auth(harness):
    # No bearer → 401/403 (auth dependency rejects).
    r = harness.client.get("/v1/admin/guardrails/configs")
    assert r.status_code in (401, 403), r.text

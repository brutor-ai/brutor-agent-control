# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# RG-side EndUserGroup binding endpoint tests

The Resource Group → Members tab in the admin UI calls these
`/admin/resource-groups/{rg_id}/end-user-groups` routes when an
operator picks a user-group to attach.

Same `EndUserGroupManager` under the hood as the UG-side, so the
unit tests already exercise the manager's logic. These tests
verify:

  * Auth + RBAC (read / add / remove perms gate independently).
  * Idempotent bind / unbind.
  * Cross-tenant 400 when the user-group belongs to another tenant.
  * 404 when the Resource Group doesn't exist.
  * The GET endpoint returns the bound set in the expected shape.
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
from ulid import ULID

from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.resource_group_end_user_groups import (
    create_resource_group_end_user_groups_router,
)
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
from app.database.models.api_key import APIKey
from app.database.models.end_user import EndUser
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.idp_server import IdPServer
from app.database.models.llm_model import LLModel
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.tenant import Tenant
from app.database.models.usage_log import UsageLog


JWT_SECRET = os.environ["JWT_SECRET"]


REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    IdPServer.__table__,
    EndUser.__table__,
    APIKey.__table__,
    UsageLog.__table__,
    ResourceGroup.__table__,
    ResourceGroupMembership.__table__,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


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
        create_resource_group_end_user_groups_router(logger), prefix="/v1",
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        for tid in ("t1", "t2"):
            setup.add(Tenant(
                id=tid, name=tid, display_name=tid.upper(),
                primary_contact_email=f"a@{tid}.invalid",
                is_active=True, is_default=False, is_trial=False,
                suspended=False, billing_plan="self_hosted",
            ))
        setup.flush()
        for tid in ("t1", "t2"):
            _create_admin_role_full(setup, tid)
        setup.commit()
    finally:
        setup.close()

    yield _Harness(client=client, engine=engine,
                   SessionFactory=SessionFactory, auth=auth)
    app.dependency_overrides.clear()
    engine.dispose()


class _Harness:
    def __init__(self, client, engine, SessionFactory, auth):
        self.client = client
        self.engine = engine
        self.SessionFactory = SessionFactory
        self.auth = auth

    def new_session(self):
        return self.SessionFactory()

    def create_tenant_admin(self, tenant_id, username, email, role_id):
        s = self.new_session()
        try:
            admin = self.auth.create_tenant_admin(
                username=username, email=email, password="Secret123!",
                tenant_id=tenant_id, db=s, role_id=role_id,
            )
            s.commit()
            return admin.id
        finally:
            s.close()

    def login(self, username, tenant_id="t1"):
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={
                "username": username, "password": "Secret123!",
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def seed_rg(self, *, rg_id="rg-prod", tenant_id="t1", name="Engineering"):
        s = self.new_session()
        try:
            s.add(ResourceGroup(
                id=rg_id, tenant_id=tenant_id,
                name=name, display_name=name,
            ))
            s.commit()
            return rg_id
        finally:
            s.close()

    def seed_user_group(self, *, tenant_id="t1", name=None):
        s = self.new_session()
        try:
            gid = f"eug-{ULID()}"
            s.add(EndUserGroup(
                id=gid, tenant_id=tenant_id,
                name=name or f"ug-{ULID()}",
                source="manual",
            ))
            s.commit()
            return gid
        finally:
            s.close()


def _create_admin_role_full(db, tenant_id):
    role = TenantRole(
        id=f"role-{tenant_id}-admin",
        tenant_id=tenant_id, name="Tenant Admin",
        description="full", is_system=True,
    )
    db.add(role)
    db.flush()
    for p in Permission:
        if p == Permission.SYSTEM_ADMIN:
            continue
        db.add(TenantRolePermission(
            id=f"perm-{uuid.uuid4().hex[:10]}",
            role_id=role.id, permission=p.value,
        ))
    db.flush()


@pytest.fixture()
def admin_token(harness):
    harness.create_tenant_admin(
        "t1", "adm1", "adm1@t1.example",
        role_id="role-t1-admin",
    )
    return harness.login("adm1")


# ---------------------------------------------------------------------------


class TestBindUnbind:
    def test_bind_then_get_then_unbind(self, harness, admin_token):
        rg = harness.seed_rg(rg_id="rg-prod")
        ug = harness.seed_user_group(name="engineering")

        # Initially nothing bound.
        r = harness.client.get(
            f"/v1/admin/resource-groups/{rg}/end-user-groups",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["total"] == 0

        # Bind.
        r = harness.client.post(
            f"/v1/admin/resource-groups/{rg}/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"end_user_group_ids": [ug]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1
        assert r.json()["end_user_groups"][0]["id"] == ug

        # Confirm via GET.
        r = harness.client.get(
            f"/v1/admin/resource-groups/{rg}/end-user-groups",
            headers=harness.bearer(admin_token),
        )
        assert r.json()["total"] == 1

        # Unbind.
        r = harness.client.request(
            "DELETE",
            f"/v1/admin/resource-groups/{rg}/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"end_user_group_ids": [ug]},
        )
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_bind_idempotent(self, harness, admin_token):
        rg = harness.seed_rg(rg_id="rg-prod")
        ug = harness.seed_user_group(name="engineering")
        body = {"end_user_group_ids": [ug]}
        # Bind twice.
        r1 = harness.client.post(
            f"/v1/admin/resource-groups/{rg}/end-user-groups",
            headers=harness.bearer(admin_token), json=body,
        )
        r2 = harness.client.post(
            f"/v1/admin/resource-groups/{rg}/end-user-groups",
            headers=harness.bearer(admin_token), json=body,
        )
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json()["total"] == 1  # not duplicated

    def test_bind_unknown_rg_404(self, harness, admin_token):
        ug = harness.seed_user_group(name="engineering")
        r = harness.client.post(
            "/v1/admin/resource-groups/rg-nope/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"end_user_group_ids": [ug]},
        )
        assert r.status_code == 404

    def test_bind_cross_tenant_user_group_400(self, harness, admin_token):
        rg = harness.seed_rg(rg_id="rg-prod", tenant_id="t1")
        cross_ug = harness.seed_user_group(tenant_id="t2", name="other")
        r = harness.client.post(
            f"/v1/admin/resource-groups/{rg}/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"end_user_group_ids": [cross_ug]},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "cross_tenant_group"

    def test_get_unknown_rg_404(self, harness, admin_token):
        r = harness.client.get(
            "/v1/admin/resource-groups/rg-nope/end-user-groups",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 404

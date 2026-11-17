# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression tests for POST /v1/admin/end-users (create_end_user).

Focus: creating an end user WITH `group_ids`. That path previously 500'd on
every call — it built a `ResourceGroupMembership` with a string `id` (the PK
is Integer autoincrement) and a non-existent `membership_type` kwarg, and
called `ulid.new()` (the ulid-py API; this project uses python-ulid). The
path had no test, so the breakage shipped silently.

Drives the real FastAPI router against in-memory SQLite via TestClient.
Harness mirrors tests/test_end_user_groups_endpoints.py.
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
from app.api.v1.endpoints.admin.end_users import create_end_user_router
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.end_user.services.authentication import EndUserAuthentication
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

JWT_SECRET = os.environ["JWT_SECRET"]

REQUIRED_TABLES = [
    LLModel.__table__,
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    SystemAdmin.__table__,
    TenantAdmin.__table__,
    EndUser.__table__,
    SystemAdminSession.__table__,
    TenantAdminSession.__table__,
    SystemAdminAuditLog.__table__,
    TenantAdminAuditLog.__table__,
    IdPServer.__table__,
    ResourceGroup.__table__,
    ResourceGroupMembership.__table__,
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
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
    def __init__(self, client, SessionFactory):
        self.client = client
        self.SessionFactory = SessionFactory

    def new_session(self) -> Session:
        return self.SessionFactory()

    def login(self, username: str, tenant_id: str = "t1", password: str = "Secret123!") -> str:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": username, "password": password, "tenant_id": tenant_id},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def seed_resource_group(self, *, rg_id: str, tenant_id: str = "t1", name: str = "Engineering") -> str:
        s = self.new_session()
        try:
            s.add(ResourceGroup(id=rg_id, tenant_id=tenant_id, name=name, display_name=name))
            s.commit()
            return rg_id
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
    end_user_auth = EndUserAuthentication()
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(create_end_user_router(end_user_auth, logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)
    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)

    setup = SessionFactory()
    try:
        setup.add(
            Tenant(
                id="t1",
                name="t1",
                display_name="Tenant One",
                primary_contact_email="admin@t1.example",
                is_active=True,
                is_default=False,
                is_trial=False,
                suspended=False,
                billing_plan="self_hosted",
            )
        )
        setup.flush()
        role = _create_admin_role_with_all_perms(setup, "t1")
        auth.create_tenant_admin(
            username="admin1",
            email="admin1@t1.example",
            password="Secret123!",
            tenant_id="t1",
            db=setup,
            role_id=role.id,
        )
        setup.commit()
    finally:
        setup.close()

    yield _Harness(client=client, SessionFactory=SessionFactory)
    app.dependency_overrides.clear()
    engine.dispose()


def test_create_end_user_with_group_ids_succeeds_and_binds_membership(harness):
    rg_id = harness.seed_resource_group(rg_id="rg-eng")
    token = harness.login("admin1")

    r = harness.client.post(
        "/v1/admin/end-users",
        headers=harness.bearer(token),
        json={
            "username": "newuser",
            "email": "newuser@example.com",
            "password": "Password123!",
            "group_ids": [rg_id],
            "send_welcome_email": False,
        },
    )
    assert r.status_code == 200, r.text

    s = harness.new_session()
    try:
        user = s.query(EndUser).filter(EndUser.username == "newuser").one()
        # Primary group is set to the first successfully-added group.
        assert user.primary_group_id == rg_id

        memberships = (
            s.query(ResourceGroupMembership)
            .filter(ResourceGroupMembership.user_id == user.id)
            .all()
        )
        assert len(memberships) == 1
        m = memberships[0]
        # Integer autoincrement PK was assigned (not a string).
        assert isinstance(m.id, int)
        assert m.tenant_id == "t1"
        assert m.group_id == rg_id
        assert m.role == "member"
        # added_by stamps the creating admin for the audit trail.
        assert m.added_by is not None
    finally:
        s.close()


def test_create_end_user_without_groups_has_no_membership(harness):
    token = harness.login("admin1")
    r = harness.client.post(
        "/v1/admin/end-users",
        headers=harness.bearer(token),
        json={
            "username": "solo",
            "email": "solo@example.com",
            "password": "Password123!",
            "send_welcome_email": False,
        },
    )
    assert r.status_code == 200, r.text

    s = harness.new_session()
    try:
        user = s.query(EndUser).filter(EndUser.username == "solo").one()
        assert user.primary_group_id is None
        count = (
            s.query(ResourceGroupMembership)
            .filter(ResourceGroupMembership.user_id == user.id)
            .count()
        )
        assert count == 0
    finally:
        s.close()

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Regression test for the Add-Member primary-group behaviour. The
admin UI surfaces a "Make this the primary group for the selected
users" checkbox; the backend must:

  * Respect `make_primary=False` on the N-th join (user already
    has a primary group) — primary stays unchanged.
  * Respect `make_primary=True` — primary is promoted to the new
    group.
  * Auto-promote on the first join regardless of `make_primary` —
    this is the documented "first group is always primary"
    invariant; a user with no primary cannot use the portal.

This file pins those three contracts in place so the next refactor
of `add_group_members` doesn't silently regress the UI checkbox.

Run: pytest tests/test_resource_group_membership_primary.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


# SQLite doesn't know about Postgres JSONB. One of the models we
# pull in transitively (system_alert.context) uses it directly,
# and `Base.metadata.create_all` then trips on the unsupported
# type. Re-route JSONB to plain JSON when emitting DDL for the
# SQLite dialect — same on-disk shape, indistinguishable for the
# in-memory tests below.
@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"

from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router
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
from app.database.models.end_user_group import EndUserGroup, end_user_group_members
from app.database.models.notification import Notification
from app.database.models.resource_group import ResourceGroup
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.tenant import Tenant
from app.services.usage_limit_enforcer import UsageLimitEnforcer


JWT_SECRET = os.environ["JWT_SECRET"]


# Import every database module so SQLAlchemy registers their
# tables on `Base.metadata` before `create_all`. The handler
# under test touches several relationships on `ResourceGroup`
# (llm_models / server_configs / agent_skills / user-group
# bindings) — listing each one individually here would be a
# game of whack-a-mole, so we just import the package and let
# `create_all()` stand up the whole schema.
import app.database.models  # noqa: F401  — registers all tables


@pytest.fixture()
def harness():
    """Fresh in-memory SQLite + mounted resource-groups router per test.

    StaticPool makes every connection see the same in-memory DB; both
    routers (resource-groups + admin-users for the login endpoint)
    are mounted because we need a real JWT to call the protected
    endpoints. invalidate_proxy_cache is fail-soft when there's no
    Redis on `app.state`, so we leave it unmocked — its post-write
    publish is best-effort anyway.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
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

    logger = BrutorLogger("test")
    app = FastAPI()
    app.include_router(create_admin_users_router(logger), prefix="/v1")
    app.include_router(
        create_resource_groups_router(UsageLimitEnforcer(logger), logger),
        prefix="/v1",
    )
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        # Tenant + role with all admin permissions so we can drive
        # /v1/admin/resource-groups/* through a tenant-admin JWT.
        tenant = Tenant(
            id="t1",
            name="t1",
            display_name="Tenant One",
            organization_name="Tenant One",
            primary_contact_email="admin@t1.example",
            is_active=True,
            is_default=False,
            is_trial=False,
            suspended=False,
            billing_plan="self_hosted",
            max_admin_users=None,
        )
        setup.add(tenant)
        setup.flush()

        role = TenantRole(
            id="role-t1-admin",
            tenant_id="t1",
            name="Tenant Admin",
            description="Test default tenant admin",
            is_system=True,
        )
        setup.add(role)
        setup.flush()
        for p in Permission:
            if p == Permission.SYSTEM_ADMIN:
                continue
            setup.add(
                TenantRolePermission(
                    id=f"perm-{uuid.uuid4().hex[:10]}",
                    role_id=role.id,
                    permission=p.value,
                )
            )
        setup.flush()

        auth.create_tenant_admin(
            username="tadmin",
            email="tadmin@t1.example",
            password="Secret123!",
            tenant_id="t1",
            db=setup,
            role_id=role.id,
        )

        # Two groups: Default (the user's seed primary) and Engineering
        # (the second group we'll add them to). Engineering deliberately
        # lives in a different hierarchy so the "already in this
        # hierarchy" skip doesn't fire.
        for gid, gname in [("rg-default", "Default"), ("rg-eng", "Engineering")]:
            setup.add(
                ResourceGroup(
                    id=gid,
                    tenant_id="t1",
                    parent_group_id=None,
                    hierarchy_level=0,
                    hierarchy_path=gname,
                    name=gname,
                    display_name=gname,
                    is_active=True,
                )
            )
        setup.flush()

        # User u1 is already a member of Default with Default as
        # primary — mirrors the scenario the admin reported testing.
        u1 = EndUser(
            tenant_id="t1",
            username="user1",
            email="user1@t1.example",
            password_hash="x",
            is_active=True,
            is_verified=True,
            primary_group_id="rg-default",
        )
        setup.add(u1)
        setup.flush()
        setup.add(
            ResourceGroupMembership(
                tenant_id="t1",
                user_id=u1.id,
                group_id="rg-default",
                role="member",
            )
        )

        # User u2 has NO primary yet — exercises the first-join branch.
        u2 = EndUser(
            tenant_id="t1",
            username="user2",
            email="user2@t1.example",
            password_hash="x",
            is_active=True,
            is_verified=True,
            primary_group_id=None,
        )
        setup.add(u2)
        setup.flush()

        setup.commit()

        u1_id = u1.id
        u2_id = u2.id
    finally:
        setup.close()

    yield _Harness(
        client=client,
        SessionFactory=SessionFactory,
        u1_id=u1_id,
        u2_id=u2_id,
    )
    app.dependency_overrides.clear()
    engine.dispose()


class _Harness:
    def __init__(self, client, SessionFactory, u1_id, u2_id):
        self.client = client
        self.SessionFactory = SessionFactory
        self.u1_id = u1_id
        self.u2_id = u2_id

    def login(self) -> str:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "tadmin", "password": "Secret123!", "tenant_id": "t1"},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def primary_of(self, user_id: int) -> str | None:
        s = self.SessionFactory()
        try:
            row = s.query(EndUser).filter(EndUser.id == user_id).one()
            return row.primary_group_id
        finally:
            s.close()


def test_n_th_join_make_primary_false_keeps_existing_primary(harness):
    """User already has primary=Default. Adding them to Engineering
    with `make_primary=false` must NOT touch primary — this is the
    scenario reported as 'the checkbox is ignored'."""
    token = harness.login()

    r = harness.client.post(
        "/v1/admin/resource-groups/rg-eng/members",
        headers=harness.bearer(token),
        json={
            "user_ids": [str(harness.u1_id)],
            "role": "member",
            "make_primary": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["added"]) == 1
    # `primary` flag in the response payload reflects whether THIS
    # call promoted the user. Should be False.
    assert body["added"][0]["primary"] is False

    # The authoritative check — read the DB after the call.
    assert harness.primary_of(harness.u1_id) == "rg-default"


def test_n_th_join_make_primary_true_promotes(harness):
    """User already has primary=Default. Same call with the box
    checked — primary should flip to Engineering."""
    token = harness.login()

    r = harness.client.post(
        "/v1/admin/resource-groups/rg-eng/members",
        headers=harness.bearer(token),
        json={
            "user_ids": [str(harness.u1_id)],
            "role": "member",
            "make_primary": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"][0]["primary"] is True

    assert harness.primary_of(harness.u1_id) == "rg-eng"


def test_first_join_auto_promotes_regardless_of_flag(harness):
    """User u2 has no primary. Adding to Engineering with the box
    UNCHECKED should still set primary — the documented first-join
    invariant (otherwise the user lands in the portal with no
    workspace). UI compensates by force-checking the checkbox in
    this case; this test pins the backend half."""
    token = harness.login()

    r = harness.client.post(
        "/v1/admin/resource-groups/rg-eng/members",
        headers=harness.bearer(token),
        json={
            "user_ids": [str(harness.u2_id)],
            "role": "member",
            "make_primary": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"][0]["primary"] is True

    assert harness.primary_of(harness.u2_id) == "rg-eng"


def test_default_missing_make_primary_treated_as_false(harness):
    """Pydantic must default `make_primary` to False when the client
    omits the field — protects against accidental promotion if a
    future API client forgets to include the flag."""
    token = harness.login()

    r = harness.client.post(
        "/v1/admin/resource-groups/rg-eng/members",
        headers=harness.bearer(token),
        json={"user_ids": [str(harness.u1_id)], "role": "member"},
    )
    assert r.status_code == 200, r.text

    # u1 had primary=Default; omitted flag must not promote.
    assert harness.primary_of(harness.u1_id) == "rg-default"

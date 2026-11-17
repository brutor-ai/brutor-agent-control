# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# End-to-end tests for /v1/admin/end-user-groups

Drives the real FastAPI router against an in-memory SQLite DB via
TestClient, so coverage includes:

  * JWT auth — every endpoint requires bearer token.
  * `require_permission` — 403 when caller is missing
    END_USER_GROUP_* perms.
  * Tenant isolation — admin in T1 cannot read/mutate groups in T2.
  * Idempotent membership + RG-binding semantics through the wire.
  * 4xx mapping for service-layer errors (name conflict → 409,
    rename of SAML group → 409, cross-tenant user → 400).
  * Detail payload shape (members + resource_groups inlined).
  * SAML-sourced groups are LIST-only via the API; create body
    silently strips source/source_external_id/idp_server_id.

The structure mirrors `tests/test_admin_rbac_endpoints.py` so the
harness, login flow, and seeding helpers are familiar to anyone
who's worked on the RBAC suite.
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
from app.api.v1.endpoints.admin.end_user_groups import (
    create_end_user_groups_router,
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


# =============================================================================
# Harness
# =============================================================================


@pytest.fixture()
def harness():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionFactory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False
    )

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
    app.include_router(create_end_user_groups_router(logger), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        for tid, dn in [("t1", "Tenant One"), ("t2", "Tenant Two")]:
            setup.add(Tenant(
                id=tid, name=tid, display_name=dn,
                primary_contact_email=f"admin@{tid}.example",
                is_active=True, is_default=False,
                is_trial=False, suspended=False,
                billing_plan="self_hosted",
            ))
        setup.flush()
        for tid in ("t1", "t2"):
            _create_admin_role_with_all_perms(setup, tid)
        _create_readonly_role(setup, "t1")
        setup.commit()
    finally:
        setup.close()

    yield _Harness(
        client=client, engine=engine,
        SessionFactory=SessionFactory, auth=auth,
    )
    app.dependency_overrides.clear()
    engine.dispose()


class _Harness:
    def __init__(self, client, engine, SessionFactory, auth):
        self.client = client
        self.engine = engine
        self.SessionFactory = SessionFactory
        self.auth = auth

    def new_session(self) -> Session:
        return self.SessionFactory()

    def login_tenant(
        self, username: str, tenant_id: str = "t1",
        password: str = "Secret123!",
    ) -> str:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={
                "username": username,
                "password": password,
                "tenant_id": tenant_id,
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def create_tenant_admin(
        self, tenant_id: str, username: str, email: str,
        password: str = "Secret123!", role_id=None,
    ) -> str:
        s = self.new_session()
        try:
            admin = self.auth.create_tenant_admin(
                username=username, email=email,
                password=password, tenant_id=tenant_id,
                db=s, role_id=role_id,
            )
            s.commit()
            return admin.id
        finally:
            s.close()

    def seed_user(self, tenant_id: str = "t1", username: str = "alice") -> int:
        s = self.new_session()
        try:
            u = EndUser(
                tenant_id=tenant_id, username=username,
                email=f"{username}@example.invalid",
                password_hash="x" * 60,
            )
            s.add(u)
            s.flush()
            s.commit()
            return u.id
        finally:
            s.close()

    def seed_resource_group(
        self, *, rg_id: str, tenant_id: str = "t1", name: str = "Engineering",
    ) -> str:
        s = self.new_session()
        try:
            rg = ResourceGroup(
                id=rg_id, tenant_id=tenant_id, name=name,
                display_name=name,
            )
            s.add(rg)
            s.commit()
            return rg.id
        finally:
            s.close()

    def seed_saml_group(
        self, *, tenant_id: str = "t1", name: str = "saml-eng",
    ) -> str:
        # A group an earlier SAML sign-in wrote (nothing writes one in
        # this release): seeded directly, past the model's source check,
        # so the rename-rejection test has something to rename.
        from ulid import ULID
        s = self.new_session()
        try:
            idp = IdPServer(
                id="idp-okta", tenant_id=tenant_id, name="Okta",
                issuer="https://acme.okta.com", type="generic_saml",
                protocol="saml", enabled=True,
            )
            s.add(idp)
            g = EndUserGroup(
                id=f"eug-{ULID()}", tenant_id=tenant_id,
                name=name, source="manual",
                source_external_id="okta-eng",
                idp_server_id="idp-okta",
            )
            s.add(g)
            s.flush()
            s.execute(EndUserGroup.__table__.update().where(EndUserGroup.id == g.id).values(source="saml"))
            s.commit()
            return g.id
        finally:
            s.close()


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
        db.add(TenantRolePermission(
            id=f"perm-{uuid.uuid4().hex[:10]}",
            role_id=role.id, permission=p.value,
        ))
    db.flush()
    return role


def _create_readonly_role(db: Session, tenant_id: str) -> TenantRole:
    """Seed a 'Tenant Read-Only' role with ONLY end-user-group READ.
    Used for the 403-on-missing-perm tests."""
    role = TenantRole(
        id=f"role-{tenant_id}-ro",
        tenant_id=tenant_id,
        name="Tenant Read-Only",
        description="read-only",
        is_system=False,
    )
    db.add(role)
    db.flush()
    db.add(TenantRolePermission(
        id=f"perm-{uuid.uuid4().hex[:10]}",
        role_id=role.id,
        permission=Permission.END_USER_GROUP_READ.value,
    ))
    db.flush()
    return role


# =============================================================================
# Auth + RBAC
# =============================================================================


class TestAuthAndRbac:
    def test_unauthenticated_request_rejected(self, harness):
        r = harness.client.get("/v1/admin/end-user-groups")
        assert r.status_code in (401, 403)

    def test_readonly_role_can_list_but_not_create(self, harness):
        # Create an admin with the read-only role.
        s = harness.new_session()
        ro_role_id = "role-t1-ro"
        s.close()
        harness.create_tenant_admin(
            tenant_id="t1", username="readonly",
            email="ro@t1.example", role_id=ro_role_id,
        )
        token = harness.login_tenant("readonly")

        # Read works.
        r = harness.client.get(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200, r.text

        # Create is rejected.
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(token),
            json={"name": "engineering"},
        )
        assert r.status_code == 403, r.text


# =============================================================================
# CRUD happy path
# =============================================================================


@pytest.fixture()
def admin_token(harness):
    harness.create_tenant_admin(
        tenant_id="t1", username="adm1", email="adm1@t1.example",
        role_id="role-t1-admin",
    )
    return harness.login_tenant("adm1")


class TestCrud:
    def test_create_minimum(self, harness, admin_token):
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "engineering"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "engineering"
        assert body["source"] == "manual"
        assert body["tenant_id"] == "t1"
        assert body["id"].startswith("eug-")
        assert body["members"] == []
        assert body["resource_groups"] == []

    def test_create_with_initial_members(self, harness, admin_token):
        uid = harness.seed_user(username="alice")
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={
                "name": "engineering",
                "display_name": "Engineering",
                "initial_member_ids": [uid],
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["member_count"] == 1
        assert [m["username"] for m in body["members"]] == ["alice"]

    def test_create_strips_source_fields_from_body(self, harness, admin_token):
        # The Pydantic create model ignores source/source_external_id/
        # idp_server_id by design — even if a malicious client sends
        # them, they must not stick. (Smoke-tests the request-shape
        # protection at the API layer.)
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={
                "name": "evil",
                "source": "saml",
                "source_external_id": "spoof",
                "idp_server_id": "idp-evil",
            },
        )
        assert r.status_code == 201
        assert r.json()["source"] == "manual"
        assert r.json()["source_external_id"] is None

    def test_duplicate_name_409(self, harness, admin_token):
        harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "engineering"},
        )
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "engineering"},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "name_conflict"

    def test_list_paginates(self, harness, admin_token):
        for n in ["alpha", "beta", "gamma", "delta"]:
            harness.client.post(
                "/v1/admin/end-user-groups",
                headers=harness.bearer(admin_token),
                json={"name": n},
            )
        r = harness.client.get(
            "/v1/admin/end-user-groups?limit=2&offset=0",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["pagination"]["total"] == 4
        assert body["pagination"]["returned"] == 2
        assert len(body["groups"]) == 2

    def test_list_filter_by_source(self, harness, admin_token):
        harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "manual-1"},
        )
        harness.seed_saml_group(name="saml-eng")
        r = harness.client.get(
            "/v1/admin/end-user-groups?source=manual",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["pagination"]["total"] == 1
        assert body["groups"][0]["name"] == "manual-1"
        # SAML is not a source in this release.
        r = harness.client.get(
            "/v1/admin/end-user-groups?source=saml",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code in (400, 422), r.text

    def test_get_detail_includes_members_and_rgs(self, harness, admin_token):
        uid = harness.seed_user(username="alice")
        rg_id = harness.seed_resource_group(rg_id="rg-prod")
        # Create the user group with the user as a member.
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng", "initial_member_ids": [uid]},
        )
        gid = r.json()["id"]
        # Bind it to the RG.
        r = harness.client.post(
            f"/v1/admin/end-user-groups/{gid}/resource-groups",
            headers=harness.bearer(admin_token),
            json={"resource_group_ids": [rg_id]},
        )
        assert r.status_code == 200, r.text
        # Fetch detail.
        r = harness.client.get(
            f"/v1/admin/end-user-groups/{gid}",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert [m["username"] for m in body["members"]] == ["alice"]
        assert [rg["id"] for rg in body["resource_groups"]] == [rg_id]

    def test_get_unknown_404(self, harness, admin_token):
        r = harness.client.get(
            "/v1/admin/end-user-groups/eug-nope",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 404

    def test_patch_rename_manual(self, harness, admin_token):
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng"},
        )
        gid = r.json()["id"]
        r = harness.client.patch(
            f"/v1/admin/end-user-groups/{gid}",
            headers=harness.bearer(admin_token),
            json={"name": "engineering", "description": "All engineers"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "engineering"
        assert r.json()["description"] == "All engineers"

    def test_patch_rename_saml_group_409(self, harness, admin_token):
        gid = harness.seed_saml_group(name="saml-eng")
        r = harness.client.patch(
            f"/v1/admin/end-user-groups/{gid}",
            headers=harness.bearer(admin_token),
            json={"name": "renamed"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "rename_not_allowed"

    def test_delete_then_get_404(self, harness, admin_token):
        r = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng"},
        )
        gid = r.json()["id"]
        r = harness.client.delete(
            f"/v1/admin/end-user-groups/{gid}",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 204
        r = harness.client.get(
            f"/v1/admin/end-user-groups/{gid}",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 404

    def test_delete_idempotent(self, harness, admin_token):
        # Repeat delete must not 5xx — manager swallows the missing row.
        r = harness.client.delete(
            "/v1/admin/end-user-groups/eug-nope",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 204


# =============================================================================
# Membership endpoints
# =============================================================================


class TestMembership:
    def test_add_then_remove(self, harness, admin_token):
        uid_a = harness.seed_user(username="alice")
        uid_b = harness.seed_user(username="bob")
        gid = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng"},
        ).json()["id"]

        # Add — idempotent, second call no-op.
        for _ in range(2):
            r = harness.client.post(
                f"/v1/admin/end-user-groups/{gid}/members",
                headers=harness.bearer(admin_token),
                json={"end_user_ids": [uid_a, uid_b]},
            )
            assert r.status_code == 200
            assert r.json()["member_count"] == 2

        # Remove one.
        r = harness.client.request(
            "DELETE",
            f"/v1/admin/end-user-groups/{gid}/members",
            headers=harness.bearer(admin_token),
            json={"end_user_ids": [uid_a]},
        )
        assert r.status_code == 200
        usernames = [m["username"] for m in r.json()["members"]]
        assert usernames == ["bob"]

    def test_set_authoritative_replace(self, harness, admin_token):
        uid_a = harness.seed_user(username="alice")
        uid_b = harness.seed_user(username="bob")
        uid_c = harness.seed_user(username="carol")
        gid = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng", "initial_member_ids": [uid_a, uid_b]},
        ).json()["id"]
        # Replace with {alice, carol}.
        r = harness.client.put(
            f"/v1/admin/end-user-groups/{gid}/members",
            headers=harness.bearer(admin_token),
            json={"end_user_ids": [uid_a, uid_c]},
        )
        assert r.status_code == 200
        usernames = sorted(m["username"] for m in r.json()["members"])
        assert usernames == ["alice", "carol"]

    def test_set_empty_clears_roster(self, harness, admin_token):
        uid = harness.seed_user(username="alice")
        gid = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng", "initial_member_ids": [uid]},
        ).json()["id"]
        r = harness.client.put(
            f"/v1/admin/end-user-groups/{gid}/members",
            headers=harness.bearer(admin_token),
            json={"end_user_ids": []},
        )
        assert r.status_code == 200
        assert r.json()["member_count"] == 0

    def test_cross_tenant_user_400(self, harness, admin_token):
        cross = harness.seed_user(tenant_id="t2", username="eve")
        gid = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng"},
        ).json()["id"]
        r = harness.client.post(
            f"/v1/admin/end-user-groups/{gid}/members",
            headers=harness.bearer(admin_token),
            json={"end_user_ids": [cross]},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "cross_tenant_user"


# =============================================================================
# RG binding endpoints
# =============================================================================


class TestRgBinding:
    def test_bind_then_unbind(self, harness, admin_token):
        rg_id = harness.seed_resource_group(rg_id="rg-prod")
        gid = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng"},
        ).json()["id"]
        # Bind.
        r = harness.client.post(
            f"/v1/admin/end-user-groups/{gid}/resource-groups",
            headers=harness.bearer(admin_token),
            json={"resource_group_ids": [rg_id]},
        )
        assert r.status_code == 200
        assert [rg["id"] for rg in r.json()["resource_groups"]] == [rg_id]
        # Repeat — idempotent.
        r = harness.client.post(
            f"/v1/admin/end-user-groups/{gid}/resource-groups",
            headers=harness.bearer(admin_token),
            json={"resource_group_ids": [rg_id]},
        )
        assert r.status_code == 200
        assert len(r.json()["resource_groups"]) == 1
        # Unbind.
        r = harness.client.request(
            "DELETE",
            f"/v1/admin/end-user-groups/{gid}/resource-groups",
            headers=harness.bearer(admin_token),
            json={"resource_group_ids": [rg_id]},
        )
        assert r.status_code == 200
        assert r.json()["resource_groups"] == []

    def test_bind_unknown_user_group_404(self, harness, admin_token):
        rg_id = harness.seed_resource_group(rg_id="rg-prod")
        r = harness.client.post(
            "/v1/admin/end-user-groups/eug-nope/resource-groups",
            headers=harness.bearer(admin_token),
            json={"resource_group_ids": [rg_id]},
        )
        assert r.status_code == 404

    def test_bind_unknown_rg_404(self, harness, admin_token):
        gid = harness.client.post(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
            json={"name": "eng"},
        ).json()["id"]
        r = harness.client.post(
            f"/v1/admin/end-user-groups/{gid}/resource-groups",
            headers=harness.bearer(admin_token),
            json={"resource_group_ids": ["rg-missing"]},
        )
        assert r.status_code == 404


# =============================================================================
# Tenant isolation
# =============================================================================


class TestTenantIsolation:
    def test_t1_admin_cannot_list_t2_group(self, harness, admin_token):
        # Seed a group directly into t2 — bypass the API since t1's
        # admin token can't create in t2.
        s = harness.new_session()
        try:
            from ulid import ULID
            s.add(EndUserGroup(
                id=f"eug-{ULID()}", tenant_id="t2",
                name="other-eng", source="manual",
            ))
            s.commit()
        finally:
            s.close()
        r = harness.client.get(
            "/v1/admin/end-user-groups",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 200
        names = [g["name"] for g in r.json()["groups"]]
        assert "other-eng" not in names

    def test_t1_admin_cannot_read_t2_group_by_id(self, harness, admin_token):
        s = harness.new_session()
        try:
            g = EndUserGroup(
                id="eug-t2-only", tenant_id="t2",
                name="t2-only", source="manual",
            )
            s.add(g)
            s.commit()
        finally:
            s.close()
        # Same path, wrong tenant — must 404 (NOT 403, because 403
        # would leak the group's existence).
        r = harness.client.get(
            "/v1/admin/end-user-groups/eug-t2-only",
            headers=harness.bearer(admin_token),
        )
        assert r.status_code == 404

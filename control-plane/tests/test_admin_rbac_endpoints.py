# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the admin RBAC router
(app/api/v1/endpoints/admin/admin_rbac.py).

These drive the real FastAPI router against an in-memory SQLite DB via
TestClient, so they cover:
  - JWT auth (HTTPBearer extraction + verify_token round-trip)
  - require_permission dependency (403 with audit log on missing perm)
  - require_permission bypass for system admins via system:admin
  - Multi-tenant isolation — tenant admin in T1 cannot read/create in T2
  - Role CRUD (is_system guard, name uniqueness, permission validation)
  - Role assignment + permission escalation guard (system:admin grant)
  - Session list / revoke
  - Logout
  - /me

Run: pytest tests/test_admin_rbac_endpoints.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.api.v1.endpoints.admin.admin_rbac import create_admin_users_router
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
from app.database.models.tenant import Tenant


JWT_SECRET = os.environ["JWT_SECRET"]

# Tables we need for the RBAC router. EndUser is pulled in transitively by
# TenantService.check_quota — listed so create_all stands it up.
from app.database.models.end_user import EndUser  # noqa: E402
from tests.app_factory import harness_extras


REQUIRED_TABLES = [
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
]


# =============================================================================
# Test harness — shared-memory SQLite + FastAPI TestClient
# =============================================================================


@pytest.fixture()
def harness():
    yield from build_harness()


def build_harness():
    """Build a fresh in-memory DB + FastAPI app + TestClient per test.

    StaticPool means every connection sees the same in-memory DB, and
    check_same_thread=False lets TestClient's worker thread reach in.
    """
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
    # The system administrator's login is a router of its own.
    for _router in harness_extras().routers(BrutorLogger('test')):
        app.include_router(_router, prefix="/v1")
    app.include_router(create_admin_users_router(BrutorLogger("test")), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    # Seed a system admin and two tenants with default "Tenant Admin" roles
    # so every test starts from a known state.
    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        harness_extras().seed_principals(auth, setup, "Secret123!")
        t1 = _create_tenant(setup, "t1", "Tenant One")
        t2 = _create_tenant(setup, "t2", "Tenant Two")
        _create_tenant_admin_role(setup, t1.id, Permission)
        _create_tenant_admin_role(setup, t2.id, Permission)
        setup.commit()
    finally:
        setup.close()

    yield _Harness(
        client=client,
        engine=engine,
        SessionFactory=SessionFactory,
        auth=auth,
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

    def login_system(self, username: str = "sysadmin", password: str = "Secret123!") -> str:
        r = self.client.post(
            "/v1/admin/users/system/login",
            json={"username": username, "password": password},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def login_manager(self) -> str:
        """Sign in as an administrator who manages tenant t1's administrators
        and roles. Here that is a full-permission administrator of t1 (the
        credential the Community Edition has). The Enterprise half runs the
        same tests signed in as the system administrator
        (`enterprise/tests/test_admin_rbac_endpoints.py` replaces this)."""
        s = self.new_session()
        try:
            exists = s.query(TenantAdmin).filter_by(tenant_id="t1", username="manager_t1").first() is not None
        finally:
            s.close()
        if not exists:
            self.create_tenant_admin("t1", "manager_t1", "manager@t1.example")
        return self.login_tenant("manager_t1", "t1")

    def login_tenant(
        self, username: str, tenant_id: str, password: str = "Secret123!"
    ) -> str:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": username, "password": password, "tenant_id": tenant_id},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def create_tenant_admin(
        self,
        tenant_id: str,
        username: str,
        email: str,
        password: str = "Secret123!",
        role_id: str | None = None,
    ) -> str:
        """Returns the new admin's id (not the ORM object — that would be
        a detached instance once the seed session closes)."""
        s = self.new_session()
        try:
            admin = self.auth.create_tenant_admin(
                username=username,
                email=email,
                password=password,
                tenant_id=tenant_id,
                db=s,
                role_id=role_id,
            )
            s.commit()
            return admin.id
        finally:
            s.close()


def _create_tenant(db: Session, tenant_id: str, display_name: str) -> Tenant:
    t = Tenant(
        id=tenant_id,
        name=tenant_id,
        display_name=display_name,
        organization_name=display_name,
        primary_contact_email=f"admin@{tenant_id}.example",
        is_active=True,
        is_default=False,
        is_trial=False,
        suspended=False,
        billing_plan="self_hosted",
        max_admin_users=None,
    )
    db.add(t)
    db.flush()
    return t


def _create_tenant_admin_role(db: Session, tenant_id: str, perm_enum) -> TenantRole:
    """Seed a 'Tenant Admin' role with ALL admin + roles permissions.

    Granting the full set mirrors production seeding (see
    TenantProvisioner) and lets tests drive every endpoint guarded by
    require_permission(...) using the tenant admin.
    """
    role = TenantRole(
        id=f"role-{tenant_id}-admin",
        tenant_id=tenant_id,
        name="Tenant Admin",
        description="Test default tenant admin",
        is_system=True,
    )
    db.add(role)
    db.flush()
    # Non-system_admin permissions (the one guarded elevation).
    for p in perm_enum:
        if p == perm_enum.SYSTEM_ADMIN:
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


def _create_readonly_role(db: Session, tenant_id: str) -> TenantRole:
    """Seed a 'Tenant Read-Only' role with no permissions — used to test 403."""
    role = TenantRole(
        id=f"role-{tenant_id}-ro",
        tenant_id=tenant_id,
        name="Tenant Read-Only",
        description="Test read-only role (no perms) ",
        is_system=False,
    )
    db.add(role)
    db.flush()
    return role


# =============================================================================
# System admin login
# =============================================================================


# =============================================================================
# Tenant admin login
# =============================================================================


class TestTenantAdminLogin:
    def test_login_ok(self, harness):
        harness.create_tenant_admin(
            tenant_id="t1",
            username="bob",
            email="bob@t1.example",
        )
        r = harness.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "bob", "password": "Secret123!", "tenant_id": "t1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"]
        assert body["tenant_id"] == "t1"
        assert body["tenant_display_name"] == "Tenant One"
        assert "is_trial" in body

    def test_login_wrong_password_returns_401(self, harness):
        harness.create_tenant_admin(
            tenant_id="t1", username="bob", email="bob@t1.example"
        )
        r = harness.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "bob", "password": "wrong-pw", "tenant_id": "t1"},
        )
        assert r.status_code == 401

    def test_login_wrong_tenant_returns_401(self, harness):
        harness.create_tenant_admin(
            tenant_id="t1", username="bob", email="bob@t1.example"
        )
        r = harness.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "bob", "password": "Secret123!", "tenant_id": "t2"},
        )
        assert r.status_code == 401

    def test_2fa_challenge_returns_temp_token(self, harness):
        admin_id = harness.create_tenant_admin(
            tenant_id="t1", username="bob", email="bob@t1.example"
        )
        # Enable TOTP directly.
        s = harness.new_session()
        try:
            db_admin = s.query(TenantAdmin).filter_by(id=admin_id).one()
            db_admin.totp_enabled = True
            db_admin.totp_secret = "encrypted-secret"
            s.commit()
        finally:
            s.close()

        r = harness.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "bob", "password": "Secret123!", "tenant_id": "t1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["requires_2fa"] is True
        assert "temp_token" in body
        assert "access_token" not in body


# =============================================================================
# Auth + identity (/me, /logout)
# =============================================================================


class TestMeAndLogout:

    def test_me_without_token_returns_401(self, harness):
        r = harness.client.get("/v1/admin/users/me")
        assert r.status_code == 401

    def test_me_with_invalid_token_returns_401(self, harness):
        r = harness.client.get(
            "/v1/admin/users/me", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert r.status_code == 401

    def test_logout_deactivates_session(self, harness):
        token = harness.login_manager()
        r = harness.client.post(
            "/v1/admin/users/logout", headers=harness.bearer(token)
        )
        assert r.status_code == 200
        # Post-logout, same token must be rejected by /me.
        r2 = harness.client.get("/v1/admin/users/me", headers=harness.bearer(token))
        assert r2.status_code == 401


# =============================================================================
# Create tenant admin
# =============================================================================


class TestCreateTenantAdmin:


    def test_tenant_admin_cannot_create_admin_in_other_tenant(self, harness):
        harness.create_tenant_admin(
            tenant_id="t1", username="bob", email="bob@t1.example"
        )
        token = harness.login_tenant("bob", "t1")

        r = harness.client.post(
            "/v1/admin/users/tenant",
            headers=harness.bearer(token),
            json={
                "tenant_id": "t2",  # Different tenant!
                "username": "intruder",
                "email": "intruder@t2.example",
                "password": "Secret123!",
            },
        )
        assert r.status_code == 403

    def test_tenant_admin_can_create_in_own_tenant(self, harness):
        harness.create_tenant_admin(
            tenant_id="t1", username="bob", email="bob@t1.example"
        )
        token = harness.login_tenant("bob", "t1")

        r = harness.client.post(
            "/v1/admin/users/tenant",
            headers=harness.bearer(token),
            json={
                "tenant_id": "t1",
                "username": "alice",
                "email": "alice@t1.example",
                "password": "Secret123!",
            },
        )
        assert r.status_code == 201

    def test_unauthenticated_returns_401(self, harness):
        r = harness.client.post(
            "/v1/admin/users/tenant",
            json={
                "tenant_id": "t1",
                "username": "alice",
                "email": "alice@t1.example",
                "password": "Secret123!",
            },
        )
        assert r.status_code == 401

    def test_duplicate_username_returns_409(self, harness):
        token = harness.login_manager()
        body = {
            "tenant_id": "t1",
            "username": "alice",
            "email": "alice@t1.example",
            "password": "Secret123!",
        }
        harness.client.post(
            "/v1/admin/users/tenant", headers=harness.bearer(token), json=body
        )
        r = harness.client.post(
            "/v1/admin/users/tenant",
            headers=harness.bearer(token),
            json={**body, "email": "different@t1.example"},
        )
        assert r.status_code == 409


# =============================================================================
# List tenant admins
# =============================================================================


class TestListTenantAdmins:

    def test_tenant_admin_only_sees_own_tenant(self, harness):
        harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        harness.create_tenant_admin("t2", "carol", "carol@t2.example")
        token = harness.login_tenant("bob", "t1")

        r = harness.client.get(
            "/v1/admin/users/tenant", headers=harness.bearer(token)
        )
        assert r.status_code == 200
        usernames = {a["username"] for a in r.json()["admins"]}
        assert "bob" in usernames
        # carol belongs to t2 — must not leak.
        assert "carol" not in usernames

    def test_tenant_admin_cannot_override_filter_to_other_tenant(self, harness):
        harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        harness.create_tenant_admin("t2", "carol", "carol@t2.example")
        token = harness.login_tenant("bob", "t1")

        # Tenant admin requests t2 via query param — server must ignore
        # and still filter by their own tenant.
        r = harness.client.get(
            "/v1/admin/users/tenant?tenant_id=t2",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200
        usernames = {a["username"] for a in r.json()["admins"]}
        assert "carol" not in usernames


# =============================================================================
# require_permission — 403 with audit log
# =============================================================================


class TestRequirePermission:
    def test_admin_without_permission_gets_403_and_is_audited(self, harness):
        # Create a custom read-only role and assign it to a tenant admin.
        s = harness.new_session()
        try:
            ro_role = _create_readonly_role(s, "t1")
            s.commit()
            readonly_role_id = ro_role.id
        finally:
            s.close()

        harness.create_tenant_admin(
            tenant_id="t1",
            username="readonly",
            email="ro@t1.example",
            role_id=readonly_role_id,
        )
        token = harness.login_tenant("readonly", "t1")

        # List endpoint requires ADMIN_USER_READ — readonly role has nothing.
        r = harness.client.get(
            "/v1/admin/users/tenant", headers=harness.bearer(token)
        )
        assert r.status_code == 403
        assert "Permission required" in r.json()["detail"]

        # Denial audit entry must survive the HTTPException rollback —
        # UnifiedAdminAuditLogger commits on its own dedicated session so
        # the security audit trail is preserved even when the handler
        # aborts with an exception.
        s = harness.new_session()
        try:
            logs = (
                s.query(TenantAdminAuditLog)
                .filter(
                    TenantAdminAuditLog.tenant_id == "t1",
                    TenantAdminAuditLog.success == False,  # noqa: E712
                    TenantAdminAuditLog.resource_type == "permission",
                )
                .all()
            )
            assert len(logs) >= 1
        finally:
            s.close()


# =============================================================================
# Role CRUD
# =============================================================================


class TestRoleCRUD:


    def test_non_system_admin_cannot_grant_system_admin_permission(self, harness):
        # A tenant admin has every admin-level permission but NOT system:admin.
        # Attempting to mint a role that includes system:admin must be blocked.
        harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        token = harness.login_tenant("bob", "t1")

        r = harness.client.post(
            "/v1/admin/users/roles",
            headers=harness.bearer(token),
            json={
                "name": "Escalation",
                "permissions": [Permission.SYSTEM_ADMIN.value],
            },
        )
        assert r.status_code == 403

    def test_list_roles_returns_seeded_default(self, harness):
        token = harness.login_manager()
        r = harness.client.get(
            "/v1/admin/users/roles?tenant_id=t1", headers=harness.bearer(token)
        )
        assert r.status_code == 200
        body = r.json()
        names = {role["name"] for role in body["roles"]}
        assert "Tenant Admin" in names
        assert body["system_roles"] >= 1

    def test_create_custom_role(self, harness):
        token = harness.login_manager()
        r = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token),
            json={
                "name": "Developer",
                "description": "Read-only dev",
                "permissions": [Permission.MCP_SERVER_READ.value],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Developer"
        assert body["is_system"] is False
        assert Permission.MCP_SERVER_READ.value in body["permissions"]

    def test_create_role_rejects_duplicate_name(self, harness):
        token = harness.login_manager()
        body = {
            "name": "Developer",
            "permissions": [Permission.MCP_SERVER_READ.value],
        }
        harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token),
            json=body,
        )
        r = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token),
            json=body,
        )
        assert r.status_code == 400
        assert "already exists" in r.json()["detail"]

    def test_create_role_rejects_invalid_permission(self, harness):
        token = harness.login_manager()
        r = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token),
            json={
                "name": "Bogus",
                "permissions": ["not:a:real:permission"],
            },
        )
        assert r.status_code == 400

    def test_update_system_role_forbidden(self, harness):
        token = harness.login_manager()
        # Find the seeded Tenant Admin system role for t1.
        s = harness.new_session()
        try:
            sys_role = s.query(TenantRole).filter_by(
                tenant_id="t1", is_system=True, name="Tenant Admin"
            ).one()
            sys_role_id = sys_role.id
        finally:
            s.close()

        r = harness.client.patch(
            f"/v1/admin/users/roles/{sys_role_id}?tenant_id=t1",
            headers=harness.bearer(token),
            json={"name": "New System Name"},
        )
        assert r.status_code == 403
        assert "system roles" in r.json()["detail"]

    def test_update_custom_role_permissions_replaces_them(self, harness):
        token = harness.login_manager()
        create = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token),
            json={
                "name": "Dev",
                "permissions": [Permission.MCP_SERVER_READ.value],
            },
        )
        role_id = create.json()["id"]

        upd = harness.client.patch(
            f"/v1/admin/users/roles/{role_id}?tenant_id=t1",
            headers=harness.bearer(token),
            json={
                "permissions": [
                    Permission.MCP_SERVER_READ.value,
                    Permission.MCP_SERVER_UPDATE.value,
                ]
            },
        )
        assert upd.status_code == 200
        body = upd.json()
        assert set(body["permissions"]) == {
            Permission.MCP_SERVER_READ.value,
            Permission.MCP_SERVER_UPDATE.value,
        }

    def test_delete_custom_role(self, harness):
        token = harness.login_manager()
        create = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token),
            json={
                "name": "Dev",
                "permissions": [Permission.MCP_SERVER_READ.value],
            },
        )
        role_id = create.json()["id"]
        r = harness.client.delete(
            f"/v1/admin/users/roles/{role_id}?tenant_id=t1",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200

    def test_delete_system_role_forbidden(self, harness):
        token = harness.login_manager()
        s = harness.new_session()
        try:
            sys_role_id = (
                s.query(TenantRole)
                .filter_by(tenant_id="t1", is_system=True)
                .first()
                .id
            )
        finally:
            s.close()
        r = harness.client.delete(
            f"/v1/admin/users/roles/{sys_role_id}?tenant_id=t1",
            headers=harness.bearer(token),
        )
        assert r.status_code == 403

    def test_get_role_returns_404_for_wrong_tenant(self, harness):
        # A role in t1 must be invisible to a caller scoped to t2.
        token_manager = harness.login_manager()
        create = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token_manager),
            json={
                "name": "Dev",
                "permissions": [Permission.MCP_SERVER_READ.value],
            },
        )
        role_id = create.json()["id"]

        harness.create_tenant_admin("t2", "carol", "carol@t2.example")
        token_t2 = harness.login_tenant("carol", "t2")
        r = harness.client.get(
            f"/v1/admin/users/roles/{role_id}", headers=harness.bearer(token_t2)
        )
        assert r.status_code == 404


# =============================================================================
# Assign role — cross-tenant & escalation
# =============================================================================


# =============================================================================
# Permissions catalog + sessions
# =============================================================================


# =============================================================================
# Audit log export — GET /audit-logs/export
# =============================================================================


def _seed_tenant_audit(db, *, tenant_id, admin_id, action, success=True, ip=None):
    """Insert a TenantAdminAuditLog row for export tests."""
    entry = TenantAdminAuditLog(
        id=f"al-{uuid.uuid4().hex[:12]}",
        tenant_id=tenant_id,
        admin_id=admin_id,
        action=action,
        success=success,
        ip_address=ip,
    )
    db.add(entry)
    db.flush()
    return entry


class TestAuditLogExport:
    def test_csv_export_streams_metadata_then_rows(self, harness):
        admin_id = harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        # Seed two audit rows for this tenant
        s = harness.new_session()
        try:
            _seed_tenant_audit(s, tenant_id="t1", admin_id=admin_id, action="login")
            _seed_tenant_audit(
                s, tenant_id="t1", admin_id=admin_id, action="create_user", success=False
            )
            s.commit()
        finally:
            s.close()

        token = harness.login_tenant("bob", "t1")
        r = harness.client.get(
            "/v1/admin/users/audit-logs/export?format=csv",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        assert "audit-logs-" in r.headers["content-disposition"]

        body = r.text
        # Schema sentinel + meta + headers + 3 data rows (login, create_user, +
        # the export action itself written after seeding into the audit trail).
        comment_lines = [ln for ln in body.splitlines() if ln.startswith("#")]
        assert any("schema_version=1.0" in c for c in comment_lines)
        assert any("resource: audit_logs" in c for c in comment_lines)
        assert any(c.startswith("# row_count:") for c in comment_lines)

        # Header row immediately after comments
        non_comment = [ln for ln in body.splitlines() if not ln.startswith("#") and ln]
        assert non_comment[0].startswith("id,timestamp,admin_id,admin_type,tenant_id")
        # At least our two seeded rows are present
        assert "login" in body
        assert "create_user" in body

    def test_jsonl_export_first_line_is_meta(self, harness):
        admin_id = harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        s = harness.new_session()
        try:
            _seed_tenant_audit(s, tenant_id="t1", admin_id=admin_id, action="login")
            s.commit()
        finally:
            s.close()
        token = harness.login_tenant("bob", "t1")

        r = harness.client.get(
            "/v1/admin/users/audit-logs/export?format=jsonl",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/x-ndjson")

        import json as _json
        lines = [ln for ln in r.text.splitlines() if ln]
        first = _json.loads(lines[0])
        assert "_meta" in first
        assert first["_meta"]["resource"] == "audit_logs"
        assert first["_meta"]["format"] == "jsonl"
        # Every other line is a JSON record with the expected keys
        for line in lines[1:]:
            rec = _json.loads(line)
            assert {"id", "action", "success", "timestamp"}.issubset(rec.keys())

    def test_tenant_admin_cannot_scope_other_tenant(self, harness):
        # Bob is admin of t1; Alice is admin of t2.
        harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        harness.create_tenant_admin("t2", "alice", "alice@t2.example")
        token = harness.login_tenant("bob", "t1")

        # Bob asks for t2's audit log explicitly — must be rejected.
        r = harness.client.get(
            "/v1/admin/users/audit-logs/export?format=csv&tenant_id=t2",
            headers=harness.bearer(token),
        )
        assert r.status_code == 403, r.text

    def test_tenant_admin_only_sees_own_tenant_rows(self, harness):
        bob_id = harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        alice_id = harness.create_tenant_admin("t2", "alice", "alice@t2.example")
        s = harness.new_session()
        try:
            _seed_tenant_audit(
                s, tenant_id="t1", admin_id=bob_id, action="bob_only_action"
            )
            _seed_tenant_audit(
                s, tenant_id="t2", admin_id=alice_id, action="alice_only_action"
            )
            s.commit()
        finally:
            s.close()

        token = harness.login_tenant("bob", "t1")
        r = harness.client.get(
            "/v1/admin/users/audit-logs/export?format=csv",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200
        assert "bob_only_action" in r.text
        assert "alice_only_action" not in r.text

    def test_export_records_audit_trail_entry(self, harness):
        admin_id = harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        token = harness.login_tenant("bob", "t1")

        r = harness.client.get(
            "/v1/admin/users/audit-logs/export?format=csv",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200

        # The export itself should now appear in the audit log table.
        s = harness.new_session()
        try:
            entries = (
                s.query(TenantAdminAuditLog)
                .filter(
                    TenantAdminAuditLog.action == "export_audit_logs",
                    TenantAdminAuditLog.tenant_id == "t1",
                    TenantAdminAuditLog.admin_id == admin_id,
                )
                .all()
            )
            assert len(entries) >= 1
            entry = entries[0]
            assert entry.success is True
            assert entry.details and entry.details.get("format") == "csv"
            assert entry.details.get("scope") == "tenant"
        finally:
            s.close()

    def test_the_actions_filter_applies(self, harness):
        admin_id = harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        s = harness.new_session()
        try:
            _seed_tenant_audit(s, tenant_id="t1", admin_id=admin_id, action="login")
            _seed_tenant_audit(s, tenant_id="t1", admin_id=admin_id, action="logout")
            _seed_tenant_audit(s, tenant_id="t1", admin_id=admin_id, action="create_user")
            s.commit()
        finally:
            s.close()
        token = harness.login_tenant("bob", "t1")

        r = harness.client.get(
            "/v1/admin/users/audit-logs/export?format=csv&actions=login&actions=logout",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200
        # The named actions, matched exactly as the list matches them: login
        # and logout, and not create_user.
        assert ",login," in r.text
        assert ",logout," in r.text
        assert "create_user" not in r.text


class TestAssignRole:

    def test_assign_role_within_tenant(self, harness):
        token = harness.login_manager()
        create = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(token),
            json={
                "name": "Dev",
                "permissions": [Permission.MCP_SERVER_READ.value],
            },
        )
        role_id = create.json()["id"]

        admin_id = harness.create_tenant_admin(
            "t1", "bob", "bob@t1.example"
        )
        r = harness.client.patch(
            f"/v1/admin/users/tenant/{admin_id}/role?tenant_id=t1",
            headers=harness.bearer(token),
            json={"role_id": role_id},
        )
        assert r.status_code == 200, r.text

        s = harness.new_session()
        try:
            db_admin = s.query(TenantAdmin).filter_by(id=admin_id).one()
            assert db_admin.role_id == role_id
        finally:
            s.close()


class TestPermissionsAndSessions:
    def test_list_permissions_returns_catalog(self, harness):
        token = harness.login_manager()
        r = harness.client.get(
            "/v1/admin/users/permissions", headers=harness.bearer(token)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total_permissions"] > 0
        all_perm_strings = {
            p["name"]
            for perms in body["permissions_by_category"].values()
            for p in perms
        }
        assert Permission.ADMIN_USER_READ.value in all_perm_strings

    def test_list_tenant_sessions(self, harness):
        harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        # A login mints a session row.
        harness.login_tenant("bob", "t1")
        token = harness.login_manager()

        r = harness.client.get(
            "/v1/admin/users/tenant/sessions?tenant_id=t1",
            headers=harness.bearer(token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1

    def test_revoke_session_kills_token(self, harness):
        admin_id = harness.create_tenant_admin("t1", "bob", "bob@t1.example")
        bob_token = harness.login_tenant("bob", "t1")
        manager_token = harness.login_manager()

        # Find bob's session id.
        s = harness.new_session()
        try:
            session_row = (
                s.query(TenantAdminSession)
                .filter_by(admin_id=admin_id, is_active=True)
                .first()
            )
            assert session_row is not None
            session_id = session_row.id
        finally:
            s.close()

        # Revoke through the session route.
        r = harness.client.post(
            f"/v1/admin/users/tenant/sessions/{session_id}/revoke?tenant_id=t1",
            headers=harness.bearer(manager_token),
        )
        assert r.status_code == 200, r.text

        # Bob's token is no longer valid.
        r2 = harness.client.get(
            "/v1/admin/users/me", headers=harness.bearer(bob_token)
        )
        assert r2.status_code == 401


class TestAnAdminCannotRaiseTheirOwnPermissions:
    def _custom_role(self, harness, permissions) -> str:
        r = harness.client.post(
            "/v1/admin/users/roles?tenant_id=t1",
            headers=harness.bearer(harness.login_manager()),
            json={"name": f"Role {uuid.uuid4().hex[:8]}", "permissions": permissions},
        )
        assert r.status_code < 300, r.text
        return r.json()["id"]

    def _limited_admin(self, harness, permissions):
        name = f"lim{uuid.uuid4().hex[:8]}"
        role_id = self._custom_role(harness, permissions)
        admin_id = harness.create_tenant_admin("t1", name, f"{name}@t1.example", role_id=role_id)
        return admin_id, role_id, harness.login_tenant(name, "t1")

    def test_roles_assign_does_not_reach_your_own_account(self, harness):
        admin_id, own_role, token = self._limited_admin(harness, [Permission.ROLES_ASSIGN.value])
        bigger = self._custom_role(
            harness, [Permission.ROLES_ASSIGN.value, Permission.ADMIN_USER_UPDATE.value]
        )
        r = harness.client.patch(
            f"/v1/admin/users/tenant/{admin_id}/role",
            headers=harness.bearer(token),
            json={"role_id": bigger},
        )
        assert r.status_code == 403, r.text
        s = harness.new_session()
        try:
            assert s.query(TenantAdmin).filter_by(id=admin_id).one().role_id == own_role
        finally:
            s.close()

    def test_roles_assign_still_works_on_someone_else(self, harness):
        _, _, token = self._limited_admin(harness, [Permission.ROLES_ASSIGN.value])
        other = harness.create_tenant_admin("t1", "carol", "carol@t1.example")
        role = self._custom_role(harness, [Permission.MCP_SERVER_READ.value])
        r = harness.client.patch(
            f"/v1/admin/users/tenant/{other}/role",
            headers=harness.bearer(token),
            json={"role_id": role},
        )
        assert r.status_code == 200, r.text

    def test_roles_update_cannot_add_to_the_role_you_hold(self, harness):
        _, own_role, token = self._limited_admin(harness, [Permission.ROLES_UPDATE.value])
        r = harness.client.patch(
            f"/v1/admin/users/roles/{own_role}",
            headers=harness.bearer(token),
            json={"permissions": [Permission.ROLES_UPDATE.value, Permission.ADMIN_USER_UPDATE.value]},
        )
        assert r.status_code == 403, r.text

    def test_roles_update_can_rename_your_role_and_edit_others(self, harness):
        _, own_role, token = self._limited_admin(harness, [Permission.ROLES_UPDATE.value])
        r = harness.client.patch(
            f"/v1/admin/users/roles/{own_role}",
            headers=harness.bearer(token),
            json={"description": "renamed"},
        )
        assert r.status_code == 200, r.text
        other_role = self._custom_role(harness, [Permission.MCP_SERVER_READ.value])
        r = harness.client.patch(
            f"/v1/admin/users/roles/{other_role}",
            headers=harness.bearer(token),
            json={"permissions": [Permission.MCP_SERVER_READ.value]},
        )
        assert r.status_code == 200, r.text


class TestResettingAnotherAdminsPasswordEndsTheirSessions:
    def test_their_sessions_are_deactivated(self, harness):
        admin_id = harness.create_tenant_admin("t1", "dave", "dave@t1.example")
        harness.login_tenant("dave", "t1")
        assert self._active(harness, admin_id) == 1
        r = harness.client.patch(
            f"/v1/admin/users/tenant/{admin_id}",
            headers=harness.bearer(harness.login_manager()),
            json={"password": "Reset12345!"},
        )
        assert r.status_code == 200, r.text
        assert self._active(harness, admin_id) == 0

    def _active(self, harness, admin_id) -> int:
        s = harness.new_session()
        try:
            return s.query(TenantAdminSession).filter_by(admin_id=admin_id, is_active=True).count()
        finally:
            s.close()

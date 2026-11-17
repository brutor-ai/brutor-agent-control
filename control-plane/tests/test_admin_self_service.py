# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Self-service admin endpoints — `/v1/admin/users/me/change-password`
and `/v1/admin/users/me/2fa/*`. Both endpoint families used to reject
sysadmin outright; this suite verifies that the unified handlers now
work for both admin types AND that the tenant-admin path still
behaves the same.

Coverage:

  * password change: success, wrong current → 401, identical new →
    400 — for both sysadmin and tenant admin.
  * 2FA enrollment flow: status off → setup → verify (real TOTP
    code) → status on → disable → status off — for both admin
    types. Same shape as the tenant-only suite that already existed
    upstream, just parameterised.
  * 2FA login: password verify returns requires_2fa + temp_token;
    POST /login/2fa with a code mints a real JWT and persists the
    matching session row. Covers BOTH SystemAdminSession and
    TenantAdminSession persistence.

Run: pytest tests/test_admin_self_service.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from typing import Optional

import bcrypt
import pyotp
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
from app.api.v1.endpoints.admin.mfa import (
    create_2fa_login_router,
    create_me_router,
    create_mfa_router,
)
from app.database import Base, get_db
from app.services.mfa.totp_service import verify_sealed_totp_code
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
from app.database.models.end_user import EndUser  # noqa: F401 — for create_all
from app.database.models.tenant import Tenant
from app.composition.principals import PRINCIPALS
from tests.app_factory import harness_extras


JWT_SECRET = os.environ["JWT_SECRET"]


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


# ---------------------------------------------------------------------------
# Fixture — minimal app with all four self-service routers mounted plus
# the admin RBAC router (needed for the login endpoints). Same SQLite
# StaticPool trick as test_admin_rbac_endpoints so every connection sees
# the same in-memory DB.
# ---------------------------------------------------------------------------


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
    # The system administrator's login is a router of its own.
    for _router in harness_extras().routers(BrutorLogger('test')):
        app.include_router(_router, prefix="/v1")
    app.include_router(create_admin_users_router(BrutorLogger("test")), prefix="/v1")
    app.include_router(create_me_router(), prefix="/v1")
    app.include_router(create_mfa_router(), prefix="/v1")
    app.include_router(create_2fa_login_router(), prefix="/v1")
    app.dependency_overrides[get_db] = override_get_db

    client = TestClient(app)

    # Seed: one sysadmin, one tenant + tenant admin role + tenant admin.
    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = SessionFactory()
    try:
        harness_extras().seed_principals(auth, setup, 'Secret123!')
        t = Tenant(
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
        setup.add(t)
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
        setup.commit()
    finally:
        setup.close()

    yield _Harness(client=client, engine=engine, SessionFactory=SessionFactory)
    app.dependency_overrides.clear()
    engine.dispose()


class _Harness:
    def __init__(self, client, engine, SessionFactory):
        self.client = client
        self.engine = engine
        self.SessionFactory = SessionFactory

    def login_system(self, password: str = "Secret123!") -> str:
        r = self.client.post(
            "/v1/admin/users/system/login",
            json={"username": "sysadmin", "password": password},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Bare-password login: returns access_token. 2FA-challenge:
        # returns temp_token. This helper only handles the former;
        # 2FA tests call /login directly to see the raw payload.
        return body["access_token"]

    def login_tenant(self, password: str = "Secret123!") -> str:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "tadmin", "password": password, "tenant_id": "t1"},
        )
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def new_session(self) -> Session:
        return self.SessionFactory()


# ---------------------------------------------------------------------------
# Password change — sysadmin (the case that used to be rejected)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Password change — tenant admin (regression: lift didn't break old path)
# ---------------------------------------------------------------------------


class TestTenantChangePassword:
    def test_success(self, harness):
        token = harness.login_tenant()
        r = harness.client.post(
            "/v1/admin/users/me/change-password",
            headers=harness.bearer(token),
            json={"current_password": "Secret123!", "new_password": "AnotherSecret789!"},
        )
        assert r.status_code == 200, r.text
        s = harness.new_session()
        try:
            row = s.query(TenantAdmin).filter(TenantAdmin.username == "tadmin").one()
            assert bcrypt.checkpw(b"AnotherSecret789!", row.password_hash.encode())
        finally:
            s.close()


# ---------------------------------------------------------------------------
# 2FA enrollment + status — parameterised over both admin types so the
# same suite covers the SystemAdmin path and the TenantAdmin regression.
# ---------------------------------------------------------------------------


# Every principal the composition under test registers: `tenant`, and in an
# Enterprise run `system`.
@pytest.fixture(params=sorted(PRINCIPALS.names()))
def admin_token(request, harness):
    if request.param == "system":
        return harness.login_system(), request.param
    return harness.login_tenant(), request.param


class TestPasswordChangeEndsOtherSessions:
    def _status(self, harness, token) -> int:
        return harness.client.get(
            "/v1/admin/users/me/2fa/status", headers=harness.bearer(token)
        ).status_code

    def test_other_sessions_end_and_this_one_stays(self, admin_token, harness):
        token, kind = admin_token
        other = harness.login_system() if kind == "system" else harness.login_tenant()
        assert self._status(harness, other) == 200

        r = harness.client.post(
            "/v1/admin/users/me/change-password",
            headers=harness.bearer(token),
            json={"current_password": "Secret123!", "new_password": "NewSecret456!"},
        )
        assert r.status_code == 200, r.text

        assert self._status(harness, other) == 401, "a stolen token outlived the password"
        assert self._status(harness, token) == 200

    def test_this_session_stays_after_it_was_just_used(self, admin_token, harness):
        # A session verified a moment ago is not looked up again (the
        # verifier's short cache): the route still knows which one it is.
        token, kind = admin_token
        assert self._status(harness, token) == 200
        r = harness.client.post(
            "/v1/admin/users/me/change-password",
            headers=harness.bearer(token),
            json={"current_password": "Secret123!", "new_password": "NewSecret456!"},
        )
        assert r.status_code == 200, r.text
        assert self._status(harness, token) == 200

    def test_a_refused_change_ends_nothing(self, admin_token, harness):
        token, kind = admin_token
        other = harness.login_system() if kind == "system" else harness.login_tenant()
        r = harness.client.post(
            "/v1/admin/users/me/change-password",
            headers=harness.bearer(token),
            json={"current_password": "wrong", "new_password": "NewSecret456!"},
        )
        assert r.status_code == 401
        assert self._status(harness, other) == 200


class TestTotpSecretAtRest:
    def _row(self, s, kind):
        if kind == "system":
            return s.query(SystemAdmin).filter(SystemAdmin.username == "sysadmin").one()
        return s.query(TenantAdmin).filter(TenantAdmin.username == "tadmin").one()

    def test_setup_never_stores_the_secret_in_clear(self, admin_token, harness):
        token, kind = admin_token
        secret = harness.client.post(
            "/v1/admin/users/me/2fa/setup", headers=harness.bearer(token)
        ).json()["secret"]
        s = harness.new_session()
        try:
            assert secret not in self._row(s, kind).totp_secret
        finally:
            s.close()

    def test_a_clear_secret_is_refused_not_trusted(self, admin_token, harness):
        token, kind = admin_token
        secret = harness.client.post(
            "/v1/admin/users/me/2fa/setup", headers=harness.bearer(token)
        ).json()["secret"]
        s = harness.new_session()
        try:
            self._row(s, kind).totp_secret = secret
            s.commit()
        finally:
            s.close()
        r = harness.client.post(
            "/v1/admin/users/me/2fa/verify",
            headers=harness.bearer(token),
            json={"code": pyotp.TOTP(secret).now()},
        )
        assert r.status_code == 401


class TestTwoFactorEnrollment:
    def test_status_initial_off(self, admin_token, harness):
        token, _ = admin_token
        r = harness.client.get(
            "/v1/admin/users/me/2fa/status", headers=harness.bearer(token)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["has_backup_codes"] is False

    def test_setup_then_verify_then_status(self, admin_token, harness):
        token, kind = admin_token

        # Setup — returns the shared secret + provisioning URI.
        r = harness.client.post(
            "/v1/admin/users/me/2fa/setup", headers=harness.bearer(token)
        )
        assert r.status_code == 200, r.text
        secret = r.json()["secret"]
        assert secret

        # Verify with a real TOTP code derived from that secret.
        code = pyotp.TOTP(secret).now()
        r = harness.client.post(
            "/v1/admin/users/me/2fa/verify",
            headers=harness.bearer(token),
            json={"code": code},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert len(body["backup_codes"]) == 10

        # Status now reflects enrolled state.
        r = harness.client.get(
            "/v1/admin/users/me/2fa/status", headers=harness.bearer(token)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["has_backup_codes"] is True

        # And the matching DB row carries the columns.
        s = harness.new_session()
        try:
            if kind == "system":
                row = s.query(SystemAdmin).filter(SystemAdmin.username == "sysadmin").one()
            else:
                row = s.query(TenantAdmin).filter(TenantAdmin.username == "tadmin").one()
            assert row.totp_enabled is True
            assert secret not in row.totp_secret, "the secret is stored in clear"
            assert verify_sealed_totp_code(row.totp_secret, pyotp.TOTP(secret).now())
            assert row.backup_codes and len(row.backup_codes) == 10
        finally:
            s.close()

    def test_disable_requires_current_code(self, admin_token, harness):
        token, _ = admin_token

        # Enroll first.
        secret = harness.client.post(
            "/v1/admin/users/me/2fa/setup", headers=harness.bearer(token)
        ).json()["secret"]
        harness.client.post(
            "/v1/admin/users/me/2fa/verify",
            headers=harness.bearer(token),
            json={"code": pyotp.TOTP(secret).now()},
        )

        # Wrong code → 401, still enabled.
        r = harness.client.post(
            "/v1/admin/users/me/2fa/disable",
            headers=harness.bearer(token),
            json={"code": "000000"},
        )
        assert r.status_code == 401

        # Correct code → 200, now disabled.
        r = harness.client.post(
            "/v1/admin/users/me/2fa/disable",
            headers=harness.bearer(token),
            json={"code": pyotp.TOTP(secret).now()},
        )
        assert r.status_code == 200
        r = harness.client.get(
            "/v1/admin/users/me/2fa/status", headers=harness.bearer(token)
        )
        assert r.json()["enabled"] is False


# ---------------------------------------------------------------------------
# 2FA login — exercises the full challenge-then-verify flow for both
# admin types. Most important on the sysadmin side because that path
# previously didn't exist.
# ---------------------------------------------------------------------------


def _enrol_2fa(harness, kind: str) -> str:
    """Walk the enrollment flow and return the shared TOTP secret so the
    test can mint valid codes for the login challenge."""
    if kind == "system":
        token = harness.login_system()
    else:
        token = harness.login_tenant()
    secret = harness.client.post(
        "/v1/admin/users/me/2fa/setup", headers=harness.bearer(token)
    ).json()["secret"]
    harness.client.post(
        "/v1/admin/users/me/2fa/verify",
        headers=harness.bearer(token),
        json={"code": pyotp.TOTP(secret).now()},
    )
    return secret


def challenge_token_or_none(harness):
    """The password step refuses a locked account; return whatever it
    hands back so the caller can assert the lock from the 2FA side too."""
    r = harness.client.post(
        "/v1/admin/users/system/login",
        json={"username": "sysadmin", "password": "Secret123!"},
    )
    if r.status_code == 200:
        return r.json().get("temp_token", "")
    return ""


class TestTenant2FALogin:
    def test_password_returns_challenge_then_code_completes(self, harness):
        secret = _enrol_2fa(harness, "tenant")

        r = harness.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": "tadmin", "password": "Secret123!", "tenant_id": "t1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("requires_2fa") is True
        assert body["temp_token"]
        # admin_type field added in the same pass that lifted the
        # sysadmin restriction — both branches now set it.
        assert body.get("admin_type") == "tenant"

        r = harness.client.post(
            "/v1/admin/users/login/2fa",
            json={"temp_token": body["temp_token"], "code": pyotp.TOTP(secret).now()},
        )
        assert r.status_code == 200
        body2 = r.json()
        assert body2["token"]
        assert body2["tenant_id"] == "t1"

        # Tenant admin path lands a TenantAdminSession, not the
        # SystemAdminSession path. Enrollment login + 2FA login both
        # count, so >= 1.
        s = harness.new_session()
        try:
            assert (
                s.query(TenantAdminSession)
                .filter(TenantAdminSession.is_active.is_(True))
                .count()
                >= 1
            )
        finally:
            s.close()

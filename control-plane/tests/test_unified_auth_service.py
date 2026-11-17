# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for UnifiedAuthService — the JWT-minting, session-managing,
password-verifying service used by every admin login in the platform.

Covers:
  - password hashing / verification
  - system admin login: happy path, missing user, bad password,
    inactive / locked / auto-lock after max failed attempts
  - tenant admin login: happy path, tenant resolution by email,
    2FA challenge short-circuit, per-tenant isolation, trial fields
  - JWT creation + verify_token round-trip (both admin types)
  - verify_token: expired session, deactivated session, unknown token,
    tampered token, invalid admin_type claim
  - logout: deactivates session, works without exp check
  - create_system_admin / create_tenant_admin: happy path +
    uniqueness conflicts + tenant role resolution
  - unlock_admin: both admin types

Run: pytest tests/test_unified_auth_service.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.constants.admin import AdminType
from app.constants.tenant import TenantDefaultRole
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.database import Base
from app.database.models.admin import (
    SystemAdmin,
    SystemAdminSession,
    TenantAdmin,
    TenantAdminSession,
    TenantRole,
    TenantRolePermission,
)
from app.database.models.tenant import Tenant


JWT_SECRET = "test-jwt-secret-not-for-production"


# =============================================================================
# SQLite in-memory fixture
# =============================================================================
#
# We stand up a minimal schema (admin + tenant tables) on SQLite so the
# service runs against a real session. Avoids touching Postgres-specific
# models (system_alert JSONB) by selectively creating only the tables
# UnifiedAuthService touches.


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    tables = [
        Tenant.__table__,
        TenantRole.__table__,
        TenantRolePermission.__table__,
        SystemAdmin.__table__,
        TenantAdmin.__table__,
        SystemAdminSession.__table__,
        TenantAdminSession.__table__,
    ]
    Base.metadata.create_all(bind=engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def svc() -> UnifiedAuthService:
    return UnifiedAuthService(jwt_secret=JWT_SECRET, jwt_expiry_hours=8)


def _mk_tenant(db: Session, tenant_id: str = "t1", display_name: str = "Tenant One") -> Tenant:
    tenant = Tenant(
        id=tenant_id,
        name=tenant_id,
        display_name=display_name,
        organization_name=display_name,
        primary_contact_email=f"admin@{tenant_id}.local",
        is_active=True,
        is_default=False,
        is_trial=False,
        suspended=False,
        billing_plan="self_hosted",
    )
    db.add(tenant)
    db.flush()
    return tenant


def _mk_tenant_admin_role(db: Session, tenant_id: str) -> TenantRole:
    role = TenantRole(
        id=f"role-{uuid.uuid4().hex[:8]}",
        tenant_id=tenant_id,
        name=TenantDefaultRole.ADMIN_USER.value,
        description="Default tenant admin role",
        is_system=True,
    )
    db.add(role)
    db.flush()
    db.add(
        TenantRolePermission(
            id=f"perm-{uuid.uuid4().hex[:8]}",
            role_id=role.id,
            permission="tenant:admin",
        )
    )
    db.flush()
    return role


# =============================================================================
# Password hashing
# =============================================================================


class TestPasswordHashing:
    def test_hash_round_trips(self, svc: UnifiedAuthService):
        hashed = svc._hash_password("hunter2-long")
        assert hashed != "hunter2-long"
        assert svc._verify_password("hunter2-long", hashed) is True
        assert svc._verify_password("wrong", hashed) is False

    def test_verify_bad_hash_returns_false_not_raises(self, svc: UnifiedAuthService):
        # Corrupt bcrypt hash — service should log + return False, not propagate.
        assert svc._verify_password("anything", "not-a-real-bcrypt-hash") is False


# =============================================================================
# create_system_admin
# =============================================================================


# =============================================================================
# login_system_admin
# =============================================================================


# =============================================================================
# create_tenant_admin
# =============================================================================


class TestCreateTenantAdmin:
    def test_creates_tenant_admin_with_default_role(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        role = _mk_tenant_admin_role(db, tenant.id)
        db.commit()

        admin = svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        assert admin.tenant_id == tenant.id
        assert admin.username == "bob"
        assert admin.role_id == role.id

    def test_raises_when_default_role_missing(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        db.commit()

        # No "Tenant Admin" role exists — should raise with a clear message.
        with pytest.raises(ValueError, match="Default .* role not found"):
            svc.create_tenant_admin(
                username="bob",
                email="bob@t1.example",
                password="Secret123!",
                tenant_id=tenant.id,
                db=db,
            )

    def test_rejects_duplicate_within_tenant(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        db.commit()

        svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        with pytest.raises(ValueError, match="already exists"):
            svc.create_tenant_admin(
                username="bob",
                email="different@t1.example",
                password="Secret123!",
                tenant_id=tenant.id,
                db=db,
            )

    def test_allows_same_username_across_tenants(
        self, db: Session, svc: UnifiedAuthService
    ):
        t1 = _mk_tenant(db, tenant_id="t1")
        t2 = _mk_tenant(db, tenant_id="t2")
        _mk_tenant_admin_role(db, t1.id)
        _mk_tenant_admin_role(db, t2.id)
        db.commit()

        a1 = svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=t1.id,
            db=db,
        )
        a2 = svc.create_tenant_admin(
            username="bob",
            email="bob@t2.example",
            password="Secret123!",
            tenant_id=t2.id,
            db=db,
        )
        db.commit()

        assert a1.id != a2.id
        assert a1.tenant_id != a2.tenant_id

    def test_rejects_unknown_role_id(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        db.commit()

        with pytest.raises(ValueError, match="not found"):
            svc.create_tenant_admin(
                username="bob",
                email="bob@t1.example",
                password="Secret123!",
                tenant_id=tenant.id,
                db=db,
                role_id="role-does-not-exist",
            )


# =============================================================================
# login_tenant_admin
# =============================================================================


class TestLoginTenantAdmin:
    def test_successful_login_with_explicit_tenant_id(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        result = svc.login_tenant_admin(
            username="bob",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        assert result is not None
        assert result["admin_type"] == AdminType.TENANT_ADMIN_TYPE.value
        assert result["tenant_id"] == tenant.id
        assert result["tenant_display_name"] == tenant.display_name
        assert result["is_trial"] is False
        assert result["suspended"] is False

        payload = jwt.decode(result["access_token"], JWT_SECRET, algorithms=["HS256"])
        assert payload["tenant_id"] == tenant.id
        assert payload["role_name"] == TenantDefaultRole.ADMIN_USER.value
        assert "tenant:admin" in payload["permissions"]

    def test_login_resolves_tenant_from_username_when_not_provided(
        self, db: Session, svc: UnifiedAuthService
    ):
        # No tenant id — caller doesn't know the tenant yet; service
        # resolves it from the unique username.
        tenant = _mk_tenant(db, tenant_id="saas-1")
        _mk_tenant_admin_role(db, tenant.id)
        svc.create_tenant_admin(
            username="owner",
            email="owner@saas-1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        result = svc.login_tenant_admin(
            username="owner", password="Secret123!", tenant_id=None, db=db
        )
        db.commit()

        assert result is not None
        assert result["tenant_id"] == tenant.id

    def test_login_isolates_cross_tenant_usernames(
        self, db: Session, svc: UnifiedAuthService
    ):
        # Two tenants, same-named user. Explicit tenant_id must pick the right row.
        t1 = _mk_tenant(db, tenant_id="t1")
        t2 = _mk_tenant(db, tenant_id="t2")
        _mk_tenant_admin_role(db, t1.id)
        _mk_tenant_admin_role(db, t2.id)
        svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="PassT1!!",
            tenant_id=t1.id,
            db=db,
        )
        svc.create_tenant_admin(
            username="bob",
            email="bob@t2.example",
            password="PassT2!!",
            tenant_id=t2.id,
            db=db,
        )
        db.commit()

        # T1 password, scoped to T1 — works.
        r1 = svc.login_tenant_admin("bob", "PassT1!!", tenant_id=t1.id, db=db)
        assert r1 is not None
        assert r1["tenant_id"] == t1.id

        # T1 password, scoped to T2 — fails (wrong password for bob@t2).
        db.rollback()
        r2 = svc.login_tenant_admin("bob", "PassT1!!", tenant_id=t2.id, db=db)
        assert r2 is None

    def test_2fa_enabled_returns_challenge(self, db: Session, svc: UnifiedAuthService):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        admin = svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        admin.totp_enabled = True
        admin.totp_secret = "encrypted-secret-blob"
        db.commit()

        result = svc.login_tenant_admin(
            username="bob", password="Secret123!", tenant_id=tenant.id, db=db
        )
        db.commit()

        assert result is not None
        assert result.get("requires_2fa") is True
        assert "temp_token" in result
        assert "access_token" not in result  # Full token is withheld until 2FA is completed.

        # Temp token has a 2fa_challenge type and short expiry.
        payload = jwt.decode(result["temp_token"], JWT_SECRET, algorithms=["HS256"])
        assert payload["type"] == "2fa_challenge"
        assert payload["admin_id"] == admin.id

    def test_locked_tenant_admin_cannot_log_in(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        admin = svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        admin.is_locked = True
        db.commit()

        assert (
            svc.login_tenant_admin("bob", "Secret123!", tenant_id=tenant.id, db=db)
            is None
        )

    def test_unknown_tenant_admin_returns_none(
        self, db: Session, svc: UnifiedAuthService
    ):
        assert (
            svc.login_tenant_admin("nobody", "pw", tenant_id="never-existed", db=db)
            is None
        )

    def test_trial_status_surfaces_in_response(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        tenant.is_trial = True
        tenant.trial_ends_at = datetime.now(timezone.utc) + timedelta(days=7)
        _mk_tenant_admin_role(db, tenant.id)
        svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        result = svc.login_tenant_admin(
            "bob", "Secret123!", tenant_id=tenant.id, db=db
        )
        db.commit()

        assert result["is_trial"] is True
        assert result["trial_ends_at"] is not None

    def test_successful_login_stamps_tenant_last_activity(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        before = tenant.last_active_at
        svc.login_tenant_admin("bob", "Secret123!", tenant_id=tenant.id, db=db)
        db.commit()

        db.refresh(tenant)
        assert tenant.last_active_at is not None
        if before is not None:
            assert tenant.last_active_at >= before


# =============================================================================
# verify_token
# =============================================================================


def _tenant_sign_in(db: Session, svc: UnifiedAuthService) -> str:
    """A tenant administrator, signed in: the token of its session."""
    tenant = _mk_tenant(db)
    _mk_tenant_admin_role(db, tenant.id)
    svc.create_tenant_admin(
        username="bob", email="bob@t1.example", password="Secret123!", tenant_id=tenant.id, db=db
    )
    db.commit()
    token = svc.login_tenant_admin("bob", "Secret123!", tenant_id=tenant.id, db=db)["access_token"]
    db.commit()
    return token


def _verified(sign_in):
    """`sign_in`, and the session has answered once."""
    def live(db: Session, svc: UnifiedAuthService) -> str:
        token = sign_in(db, svc)
        assert svc.verify_token(token, db=db) is not None
        return token
    return live


class TestVerifyToken:
    # What the tests below sign in with, and where its session row is. Here
    # the credential the Community Edition has, a tenant administrator; the
    # Enterprise half runs the same tests with the system administrator.
    SESSION_MODEL = TenantAdminSession
    _signed_in = staticmethod(_tenant_sign_in)
    _live_session = staticmethod(_verified(_tenant_sign_in))

    def test_verify_tenant_admin_token_roundtrip(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()
        r = svc.login_tenant_admin("bob", "Secret123!", tenant_id=tenant.id, db=db)
        db.commit()

        out = svc.verify_token(r["access_token"], db=db)
        assert out is not None
        admin, session, admin_type = out
        assert admin.tenant_id == tenant.id
        assert admin_type == AdminType.TENANT_ADMIN_TYPE

    # --- a token says what it is, or it is refused ------------------------------
    #
    # `verify_token` read `admin_type` with a default of *system admin*: a
    # token signed with JWT_SECRET that did not carry the claim was treated as
    # the most privileged principal and then looked up by its `jti`. The core
    # signs portal sessions with the same secret. Nothing that issuer mints
    # could reach a live admin session — but "unknown means most privileged"
    # is the wrong direction, and these pin the right one: the claim must be
    # present and known, and the token must be an admin token (`typ`).

    def _resign(self, token: str, **changes) -> str:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        for key, value in changes.items():
            if value is None:
                payload.pop(key, None)
            else:
                payload[key] = value
        return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


    def test_token_signed_with_other_secret_rejected(
        self, db: Session, svc: UnifiedAuthService
    ):
        # Minting with a different secret must not validate against ours.
        payload = {
            "user_id": "fake",
            "username": "attacker",
            "admin_type": AdminType.SYSTEM_ADMIN_TYPE.value,
            "permissions": [],
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "jti": "nope",
        }
        fake_token = jwt.encode(payload, "different-secret", algorithm="HS256")
        assert svc.verify_token(fake_token, db=db) is None


    def test_tenant_token_without_tenant_id_rejected(
        self, db: Session, svc: UnifiedAuthService
    ):
        # Well-formed JWT claiming tenant admin type but with no tenant_id —
        # the service must not honor it (defensive against malformed tokens).
        payload = {
            "user_id": "x",
            "username": "x",
            "admin_type": AdminType.TENANT_ADMIN_TYPE.value,
            "permissions": [],
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "jti": "jti-x",
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
        assert svc.verify_token(token, db=db) is None

    def test_unknown_admin_type_rejected(self, db: Session, svc: UnifiedAuthService):
        payload = {
            "user_id": "x",
            "username": "x",
            "admin_type": "mystery",
            "permissions": [],
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "jti": "jti-x",
        }
        token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
        assert svc.verify_token(token, db=db) is None

    @pytest.mark.parametrize("changes", [
        {"admin_type": None},                      # the claim is missing
        {"admin_type": "superuser"},               # a type nobody defined
        {"admin_type": ""},
        {"typ": "portal"},                         # another issuer's token type
        {"typ": None},                             # no token type at all
        {"typ": "portal", "admin_type": None},     # what a portal session looks like
    ])
    def test_a_token_that_does_not_say_it_is_an_admin_token_is_refused(
        self, db: Session, svc: UnifiedAuthService, changes
    ):
        # Same signature key, same live session id, same user id: only the
        # claims that say what the token is are changed.
        token = self._resign(self._live_session(db, svc), **changes)
        assert svc.verify_token(token, db=db) is None

    @pytest.mark.parametrize("changes", [
        {"admin_type": None},
        {"typ": "portal"},
        {"admin_type": "superuser"},
    ])
    def test_such_a_token_cannot_log_an_administrator_out(
        self, db: Session, svc: UnifiedAuthService, changes
    ):
        good = self._live_session(db, svc)
        assert svc.logout(self._resign(good, **changes), db=db) is False
        db.commit()
        assert svc.verify_token(good, db=db) is not None, "the session is still active"

    def test_tampered_token_is_rejected(self, db: Session, svc: UnifiedAuthService):
        r = {"access_token": self._signed_in(db, svc)}

        # Flip a character in the signature section — decode must fail.
        parts = r["access_token"].split(".")
        tampered = ".".join([parts[0], parts[1], parts[2][:-3] + "aaa"])
        assert svc.verify_token(tampered, db=db) is None

    def test_deactivated_session_rejects_token(
        self, db: Session, svc: UnifiedAuthService
    ):
        r = {"access_token": self._signed_in(db, svc)}

        payload = jwt.decode(r["access_token"], JWT_SECRET, algorithms=["HS256"])
        session = db.query(self.SESSION_MODEL).filter_by(id=payload["jti"]).one()
        session.is_active = False
        db.commit()

        # Signed + unexpired, but no active session → treated as invalid.
        assert svc.verify_token(r["access_token"], db=db) is None

    def test_expired_session_auto_deactivates(
        self, db: Session, svc: UnifiedAuthService
    ):
        r = {"access_token": self._signed_in(db, svc)}

        # Make the session itself expired server-side while JWT is still nominally valid.
        payload = jwt.decode(r["access_token"], JWT_SECRET, algorithms=["HS256"])
        session = db.query(self.SESSION_MODEL).filter_by(id=payload["jti"]).one()
        session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()

        assert svc.verify_token(r["access_token"], db=db) is None
        db.commit()

        db.refresh(session)
        assert session.is_active is False
        assert session.deactivation_reason == "expired"


# =============================================================================
# logout
# =============================================================================


class TestLogout:
    SESSION_MODEL = TenantAdminSession
    _signed_in = staticmethod(_tenant_sign_in)
    _live_session = staticmethod(_verified(_tenant_sign_in))

    def test_logout_works_for_tenant_admin(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()
        r = svc.login_tenant_admin("bob", "Secret123!", tenant_id=tenant.id, db=db)
        db.commit()

        assert svc.logout(r["access_token"], db=db) is True
        db.commit()

        payload = jwt.decode(r["access_token"], JWT_SECRET, algorithms=["HS256"])
        session = db.query(TenantAdminSession).filter_by(id=payload["jti"]).one()
        assert session.is_active is False

    def test_logout_ignores_exp_so_expired_tokens_still_deactivate(
        self, db: Session, svc: UnifiedAuthService
    ):
        # Users must be able to log out of an expired tab without the
        # server rejecting their logout call and leaving the session row
        # lying around as "active but unreachable".
        r = {"access_token": self._signed_in(db, svc)}

        # Mint a replacement JWT with exp in the past, same jti as the real session.
        payload = jwt.decode(r["access_token"], JWT_SECRET, algorithms=["HS256"])
        expired_payload = dict(payload)
        expired_payload["exp"] = datetime.now(timezone.utc) - timedelta(hours=1)
        expired_token = jwt.encode(expired_payload, JWT_SECRET, algorithm="HS256")

        assert svc.logout(expired_token, db=db) is True


# =============================================================================
# unlock_admin
# =============================================================================


class TestUnlockAdmin:

    def test_unlock_tenant_admin_requires_tenant_id(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        admin = svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        admin.is_locked = True
        db.commit()

        # Missing tenant_id → refuses (prevents cross-tenant unlock).
        assert (
            svc.unlock_admin(
                admin.id,
                admin_type=AdminType.TENANT_ADMIN_TYPE,
                tenant_id=None,
                db=db,
            )
            is False
        )

        # With tenant_id → works.
        ok = svc.unlock_admin(
            admin.id,
            admin_type=AdminType.TENANT_ADMIN_TYPE,
            tenant_id=tenant.id,
            db=db,
        )
        assert ok is True
        db.commit()
        db.refresh(admin)
        assert admin.is_locked is False

    def test_unlock_unknown_admin_returns_false(
        self, db: Session, svc: UnifiedAuthService
    ):
        assert (
            svc.unlock_admin(
                "does-not-exist",
                admin_type=AdminType.SYSTEM_ADMIN_TYPE,
                tenant_id=None,
                db=db,
            )
            is False
        )


# =============================================================================
# create_admin_jwt — used by 2FA completion
# =============================================================================


class TestCreateAdminJWT:
    def test_mints_jwt_and_persists_session(
        self, db: Session, svc: UnifiedAuthService
    ):
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        admin = svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()

        token = svc.create_admin_jwt(
            admin=admin, tenant_id=tenant.id, db=db, ip_address="1.2.3.4"
        )
        db.commit()

        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        assert payload["tenant_id"] == tenant.id
        assert payload["admin_type"] == AdminType.TENANT_ADMIN_TYPE.value

        session = db.query(TenantAdminSession).filter_by(id=payload["jti"]).one()
        assert session.is_active is True
        assert session.ip_address == "1.2.3.4"

    def test_without_db_mints_token_but_skips_session(
        self, db: Session, svc: UnifiedAuthService
    ):
        # Documented footgun — callers who pass db=None are responsible
        # for creating their own session row. We just verify the contract
        # holds: a token is returned and no session is persisted.
        tenant = _mk_tenant(db)
        _mk_tenant_admin_role(db, tenant.id)
        admin = svc.create_tenant_admin(
            username="bob",
            email="bob@t1.example",
            password="Secret123!",
            tenant_id=tenant.id,
            db=db,
        )
        db.commit()
        before = db.query(TenantAdminSession).count()

        token = svc.create_admin_jwt(admin=admin, tenant_id=tenant.id, db=None)
        assert isinstance(token, str)
        assert db.query(TenantAdminSession).count() == before

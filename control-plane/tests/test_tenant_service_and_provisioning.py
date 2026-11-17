# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for:
  - TenantContext — per-request tenant context (contextvar-based)
  - TenantService — validate_tenant, check_quota, check_feature_access,
    get_or_create_default_tenant
  - TenantProvisioner small methods — _seed_roles (RBAC seeding),
    _create_tenant_admin (admin + password hash), _create_default_resource_group
    (Default system group), get_default_tenant_roles (role catalog)

These exercise the tenant isolation + quota + provisioning plumbing
that every signup and every admin-scoped operation depends on.

Run: pytest tests/test_tenant_service_and_provisioning.py -v
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.constants.groups import ResourceGroupName
from app.constants.tenant import TenantDefaultRole
from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.admin import (
    TenantAdmin,
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
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant
from app.services.tenant.tenant_provisioning import TenantProvisioner
from app.services.tenant.tenant_service import TenantContext, TenantService


REQUIRED_TABLES = [
    Tenant.__table__,
    TenantRole.__table__,
    TenantRolePermission.__table__,
    TenantAdmin.__table__,
    # TenantAdmin.idp_server_id FKs into idp_servers.
    IdPServer.__table__,
    EndUser.__table__,
    ResourceGroup.__table__,
    # ResourceGroup.end_user_groups selectin needs these.
    EndUserGroup.__table__,
    end_user_group_members,
    resource_group_end_user_groups,
]


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _mk_tenant(
    db: Session,
    tenant_id: str = "t1",
    is_default: bool = False,
    is_active: bool = True,
    suspended: bool = False,
    max_admin_users: int | None = None,
    is_trial: bool = False,
    trial_ends_at=None,
    features: dict | None = None,
) -> Tenant:
    t = Tenant(
        id=tenant_id,
        name=tenant_id,
        display_name=tenant_id,
        organization_name=tenant_id,
        primary_contact_email=f"admin@{tenant_id}.local",
        is_active=is_active,
        is_default=is_default,
        suspended=suspended,
        is_trial=is_trial,
        trial_ends_at=trial_ends_at,
        billing_plan="self_hosted",
        max_admin_users=max_admin_users,
        features=features,
    )
    db.add(t)
    db.flush()
    return t


# =============================================================================
# TenantContext (contextvar-based per-request state)
# =============================================================================


class TestTenantContext:
    def test_set_get_round_trip(self):
        TenantContext.clear()
        assert TenantContext.get_tenant_id() is None

        TenantContext.set_tenant_id("t-alpha")
        assert TenantContext.get_tenant_id() == "t-alpha"

        TenantContext.clear()
        assert TenantContext.get_tenant_id() is None

    def test_require_raises_when_not_set(self):
        TenantContext.clear()
        with pytest.raises(HTTPException) as exc:
            TenantContext.require_tenant_id()
        assert exc.value.status_code == 500

    def test_require_returns_when_set(self):
        TenantContext.set_tenant_id("t-beta")
        try:
            assert TenantContext.require_tenant_id() == "t-beta"
        finally:
            TenantContext.clear()


# =============================================================================
# TenantService.validate_tenant
# =============================================================================


class TestValidateTenant:
    def test_returns_active_tenant(self, db: Session):
        t = _mk_tenant(db)
        db.commit()
        got = TenantService.validate_tenant(db, t.id)
        assert got.id == t.id

    def test_raises_404_for_unknown_tenant(self, db: Session):
        with pytest.raises(HTTPException) as exc:
            TenantService.validate_tenant(db, "ghost")
        assert exc.value.status_code == 404

    def test_raises_403_for_inactive_tenant(self, db: Session):
        _mk_tenant(db, is_active=False)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            TenantService.validate_tenant(db, "t1")
        assert exc.value.status_code == 403

    def test_raises_403_for_suspended_tenant(self, db: Session):
        t = _mk_tenant(db, suspended=True)
        t.suspension_reason = "payment"
        db.commit()
        with pytest.raises(HTTPException) as exc:
            TenantService.validate_tenant(db, "t1")
        assert exc.value.status_code == 403
        assert "suspended" in exc.value.detail.lower()

    def test_the_community_edition_knows_no_trial(self, db: Session, monkeypatch):
        # A trial is an Enterprise (multi-tenant) concept. The
        # columns exist (one schema) and no Community code reads them; the
        # expiry check is the Enterprise Edition's, registered at
        # `TENANT_STATUS_CHECKS` (`enterprise/tests/test_tenant_trial_check.py`).
        # This asserted 402 here.
        from app.composition import hooks

        monkeypatch.setattr(hooks.HOOKS, "_specs", {})
        _mk_tenant(
            db,
            is_trial=True,
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.commit()
        assert TenantService.validate_tenant(db, "t1").id == "t1"

    def test_a_registered_status_check_refuses_and_runs_after_the_tenants_own(self, db: Session, monkeypatch):
        from app.composition import hooks

        seen = []

        def check(*, tenant):
            seen.append(tenant.id)
            raise HTTPException(status_code=402, detail="refused by the check")

        registry = hooks.HookRegistry([hooks.HookSpec("a_check", hooks.TENANT_STATUS_CHECKS, check)])
        monkeypatch.setattr(hooks.HOOKS, "_specs", registry._specs)
        _mk_tenant(db)
        _mk_tenant(db, tenant_id="t2", suspended=True)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            TenantService.validate_tenant(db, "t1")
        assert (exc.value.status_code, exc.value.detail) == (402, "refused by the check")
        # A suspended tenant is refused as suspended, before any check.
        with pytest.raises(HTTPException) as exc:
            TenantService.validate_tenant(db, "t2")
        assert exc.value.status_code == 403
        assert seen == ["t1"]

    def test_no_community_source_has_the_trial_message(self):
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[1] / "app"
        found = [
            str(p.relative_to(app_dir))
            for p in app_dir.rglob("*.py")
            if "Trial period has expired" in p.read_text(encoding="utf-8")
            or "is_trial_expired()" in p.read_text(encoding="utf-8")
        ]
        assert found == []


# =============================================================================
# TenantService.check_quota
# =============================================================================


class TestCheckQuota:
    def test_raises_429_when_admin_user_quota_exceeded(self, db: Session):
        # Limit = 1; pre-create 1 admin; quota check must raise on the next.
        t = _mk_tenant(db, max_admin_users=1)
        role = TenantRole(
            id="role-1", tenant_id=t.id, name="Tenant Admin", is_system=True
        )
        db.add(role)
        db.add(
            TenantAdmin(
                id="a-1",
                tenant_id=t.id,
                role_id=role.id,
                username="first",
                email="first@t1",
                password_hash="x",
                is_active=True,
                is_locked=False,
                failed_login_attempts=0,
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as exc:
            TenantService.check_quota(db, t.id, "admin_users")
        assert exc.value.status_code == 429
        # The answer is structured; the sentence is its `message`.
        assert "admin users" in exc.value.detail["message"].lower()
        assert exc.value.detail["error"] == "tenant_quota_reached"

    def test_unlimited_quota_allows_any_count(self, db: Session):
        t = _mk_tenant(db, max_admin_users=None)
        role = TenantRole(
            id="role-1", tenant_id=t.id, name="Tenant Admin", is_system=True
        )
        db.add(role)
        for i in range(5):
            db.add(
                TenantAdmin(
                    id=f"a-{i}",
                    tenant_id=t.id,
                    role_id=role.id,
                    username=f"u{i}",
                    email=f"u{i}@t1",
                    password_hash="x",
                    is_active=True,
                    is_locked=False,
                    failed_login_attempts=0,
                )
            )
        db.commit()

        # Unlimited → no raise.
        TenantService.check_quota(db, t.id, "admin_users")

    def test_raises_404_for_unknown_tenant(self, db: Session):
        with pytest.raises(HTTPException) as exc:
            TenantService.check_quota(db, "ghost", "admin_users")
        assert exc.value.status_code == 404

    def test_unknown_resource_type_is_logged_not_raised(self, db: Session):
        _mk_tenant(db)
        db.commit()
        # Does not raise — unknown resource types are treated as noop so
        # that platform-level code that checks quota for types a tenant
        # plan doesn't recognize doesn't crash.
        TenantService.check_quota(db, "t1", "not-a-real-resource-type")


# =============================================================================
# TenantService.check_feature_access
# =============================================================================


class TestCheckFeatureAccess:
    def test_allows_enabled_feature(self, db: Session):
        _mk_tenant(db, features={"kb_uploader": True})
        db.commit()
        TenantService.check_feature_access(db, "t1", "kb_uploader")

    def test_rejects_disabled_feature(self, db: Session):
        _mk_tenant(db, features={"kb_uploader": False})
        db.commit()
        with pytest.raises(HTTPException):
            TenantService.check_feature_access(db, "t1", "kb_uploader")


# =============================================================================
# TenantService.get_or_create_default_tenant
# =============================================================================


class TestDefaultTenant:
    def test_creates_default_when_absent(self, db: Session):
        before = db.query(Tenant).count()
        t = TenantService.get_or_create_default_tenant(db)
        db.commit()
        after = db.query(Tenant).count()
        assert after == before + 1
        assert t.is_default is True

    def test_returns_existing_default_if_present(self, db: Session):
        existing = _mk_tenant(db, tenant_id="default", is_default=True)
        db.commit()
        got = TenantService.get_or_create_default_tenant(db)
        assert got.id == existing.id


# =============================================================================
# TenantProvisioner — _seed_roles, _create_tenant_admin, _create_default_resource_group
# =============================================================================


class TestProvisionerHelpers:
    def test_get_default_tenant_roles_returns_admin_user_and_readonly(self):
        # Pure config — no DB needed.
        roles = TenantProvisioner(logger=BrutorLogger("test")).get_default_tenant_roles()
        assert TenantDefaultRole.ADMIN_USER.value in roles
        assert TenantDefaultRole.END_USER.value in roles
        assert TenantDefaultRole.READ_ONLY_USER.value in roles
        # Admin role must include a healthy set of permissions.
        assert len(roles[TenantDefaultRole.ADMIN_USER.value]["permissions"]) > 10

    def test_seed_roles_creates_all_default_roles(self, db: Session):
        t = _mk_tenant(db)
        db.commit()
        prov = TenantProvisioner(logger=BrutorLogger("test"))

        role_ids = prov._seed_roles(db, t.id)
        db.commit()

        # All default roles present + persisted.
        assert TenantDefaultRole.ADMIN_USER.value in role_ids
        assert TenantDefaultRole.END_USER.value in role_ids
        persisted = (
            db.query(TenantRole).filter(TenantRole.tenant_id == t.id).all()
        )
        persisted_names = {r.name for r in persisted}
        assert TenantDefaultRole.ADMIN_USER.value in persisted_names
        # Each role got its permissions.
        admin_role = next(
            r for r in persisted if r.name == TenantDefaultRole.ADMIN_USER.value
        )
        assert len(admin_role.permissions) > 0

    def test_seed_roles_is_idempotent(self, db: Session):
        t = _mk_tenant(db)
        db.commit()
        prov = TenantProvisioner(logger=BrutorLogger("test"))

        first = prov._seed_roles(db, t.id)
        db.commit()
        count_after_first = (
            db.query(TenantRole).filter(TenantRole.tenant_id == t.id).count()
        )

        second = prov._seed_roles(db, t.id)
        db.commit()
        count_after_second = (
            db.query(TenantRole).filter(TenantRole.tenant_id == t.id).count()
        )

        assert count_after_first == count_after_second
        assert first == second

    def test_create_tenant_admin_hashes_password(self, db: Session):
        import bcrypt

        t = _mk_tenant(db)
        db.commit()
        prov = TenantProvisioner(logger=BrutorLogger("test"))
        role_ids = prov._seed_roles(db, t.id)
        db.commit()

        admin_id, username = prov._create_tenant_admin(
            db=db,
            tenant_id=t.id,
            role_id=role_ids[TenantDefaultRole.ADMIN_USER.value],
            username="adm",
            password="Secret123!",
            email="adm@t1.local",
            full_name="Admin",
        )
        db.commit()
        admin = db.query(TenantAdmin).filter_by(id=admin_id).one()
        # Password is stored hashed — plaintext must NOT appear.
        assert admin.password_hash != "Secret123!"
        assert bcrypt.checkpw(
            b"Secret123!", admin.password_hash.encode("utf-8")
        )

    def test_create_tenant_admin_is_idempotent_on_username(self, db: Session):
        t = _mk_tenant(db)
        db.commit()
        prov = TenantProvisioner(logger=BrutorLogger("test"))
        role_ids = prov._seed_roles(db, t.id)
        db.commit()

        a1_id, _ = prov._create_tenant_admin(
            db=db,
            tenant_id=t.id,
            role_id=role_ids[TenantDefaultRole.ADMIN_USER.value],
            username="adm",
            password="Secret123!",
            email="adm@t1.local",
            full_name="Admin",
        )
        db.commit()
        a2_id, _ = prov._create_tenant_admin(
            db=db,
            tenant_id=t.id,
            role_id=role_ids[TenantDefaultRole.ADMIN_USER.value],
            username="adm",
            password="Different!",
            email="adm@t1.local",
            full_name="Admin",
        )
        db.commit()

        # Same admin returned — no duplicate row created.
        assert a1_id == a2_id
        count = (
            db.query(TenantAdmin)
            .filter(TenantAdmin.tenant_id == t.id, TenantAdmin.username == "adm")
            .count()
        )
        assert count == 1

    def test_create_default_resource_group(self, db: Session):
        t = _mk_tenant(db)
        db.commit()
        prov = TenantProvisioner(logger=BrutorLogger("test"))

        group_id = prov._create_default_resource_group(db, t.id)
        db.commit()

        group = db.query(ResourceGroup).filter_by(id=group_id).one()
        assert group.name == ResourceGroupName.DEFAULT.value
        assert group.is_system is True
        assert group.tenant_id == t.id

    def test_create_default_resource_group_is_idempotent(self, db: Session):
        t = _mk_tenant(db)
        db.commit()
        prov = TenantProvisioner(logger=BrutorLogger("test"))

        g1 = prov._create_default_resource_group(db, t.id)
        db.commit()
        g2 = prov._create_default_resource_group(db, t.id)
        db.commit()

        # Same group reused; only one Default exists.
        assert g1 == g2
        count = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.tenant_id == t.id,
                ResourceGroup.name == ResourceGroupName.DEFAULT.value,
            )
            .count()
        )
        assert count == 1

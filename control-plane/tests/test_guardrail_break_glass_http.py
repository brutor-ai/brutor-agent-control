# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Guardrail break-glass — the operator escape hatch.

Break-glass produces the same runtime outcome as a hardcoded fail-open:
traffic passes unchecked. The only things that make it acceptable are the
three properties these tests defend — it is **attributed**, it **requires a
reason**, and it **expires on its own**.

Lose any one of those and this stops being an incident action and becomes an
unreviewed permanent policy change, which is exactly the drift that made every
competing gateway fail open by default.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

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
from app.database.models.guardrail_break_glass import (
    DEFAULT_BREAK_GLASS_MINUTES,
    MAX_BREAK_GLASS_HOURS,
    GuardrailBreakGlass,
)
from app.database.models.guardrail_config import (
    GuardrailConfig,
    resource_group_guardrails,
)
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant
from app.services.guardrails.guardrails_service import GuardrailsService

JWT_SECRET = os.environ["JWT_SECRET"]
BG = "/v1/admin/guardrails/break-glass"

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
    GuardrailBreakGlass.__table__,
]


def _admin_role(db: Session, tenant_id: str) -> TenantRole:
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


class _H:
    def __init__(self, client, factory, auth):
        self.client = client
        self.factory = factory
        self.auth = auth

    def session(self) -> Session:
        return self.factory()

    def token(self, username="adm1", tenant="t1") -> dict:
        r = self.client.post(
            "/v1/admin/users/tenant/login",
            json={"username": username, "password": "Secret123!", "tenant_id": tenant},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def h():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine, tables=REQUIRED_TABLES)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        s = factory()
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
        create_guardrails_router(
            GuardrailsService(logger=logger), logger, encryption_service=None
        ),
        prefix="/v1",
    )
    app.dependency_overrides[get_db] = override_get_db

    auth = UnifiedAuthService(jwt_secret=JWT_SECRET)
    setup = factory()
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
        _admin_role(setup, "t1")
        setup.commit()
        auth.create_tenant_admin(
            username="adm1",
            email="adm1@t1.example",
            password="Secret123!",
            tenant_id="t1",
            db=setup,
            role_id="role-t1-admin",
        )
        setup.commit()
    finally:
        setup.close()

    yield _H(TestClient(app), factory, auth)
    app.dependency_overrides.clear()
    engine.dispose()


REASON = "Presidio is returning 503s across the fleet — incident INC-4471"


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_starts_inactive(h):
    """Enforcement on is the resting state. If this ever defaults to active,
    every tenant ships unguarded."""
    r = h.client.get(BG, headers=h.token())
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    assert r.json()["current"] is None


def test_activate_then_read_back(h):
    r = h.client.post(BG, headers=h.token(), json={"reason": REASON})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_active"] is True
    assert body["reason"] == REASON
    assert body["activated_by"] == "adm1"

    state = h.client.get(BG, headers=h.token()).json()
    assert state["active"] is True
    assert state["current"]["id"] == body["id"]


def test_deactivate_restores_enforcement(h):
    h.client.post(BG, headers=h.token(), json={"reason": REASON})
    r = h.client.delete(BG, headers=h.token())
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    assert h.client.get(BG, headers=h.token()).json()["active"] is False


def test_deactivate_is_idempotent(h):
    """'Enforcement is on' is the desired end state either way, and erroring
    mid-incident helps nobody."""
    r = h.client.delete(BG, headers=h.token())
    assert r.status_code == 200
    assert r.json()["active"] is False


# ---------------------------------------------------------------------------
# It must carry a reason
# ---------------------------------------------------------------------------


def test_a_reason_is_required(h):
    r = h.client.post(BG, headers=h.token(), json={})
    assert r.status_code == 422


@pytest.mark.parametrize("reason", ["", "   ", "oops"])
def test_a_trivial_reason_is_refused(h, reason):
    """An unexplained break-glass is indistinguishable from a
    misconfiguration when someone reviews it three months later."""
    r = h.client.post(BG, headers=h.token(), json={"reason": reason})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# It must expire — the property that separates this from a config change
# ---------------------------------------------------------------------------


def test_it_always_has_an_expiry(h):
    r = h.client.post(BG, headers=h.token(), json={"reason": REASON})
    assert r.json()["expires_at"] is not None


def _as_utc(iso: str) -> datetime:
    """SQLite hands back naive datetimes; Postgres (timezone=True) does not.
    Normalize so these tests assert on the window, not on the backend."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def test_default_window_is_short(h):
    r = h.client.post(BG, headers=h.token(), json={"reason": REASON})
    expires = _as_utc(r.json()["expires_at"])
    delta_min = (expires - datetime.now(timezone.utc)).total_seconds() / 60
    assert DEFAULT_BREAK_GLASS_MINUTES - 2 <= delta_min <= DEFAULT_BREAK_GLASS_MINUTES + 2


def test_an_over_long_window_is_clamped_not_rejected(h):
    """An operator mid-incident typing 1440 should get a valid capped window
    and a note, not a 422 while the pager is going off."""
    r = h.client.post(
        BG, headers=h.token(), json={"reason": REASON, "duration_minutes": 60 * 24 * 7}
    )
    assert r.status_code == 201, r.text
    expires = _as_utc(r.json()["expires_at"])
    hours = (expires - datetime.now(timezone.utc)).total_seconds() / 3600
    assert hours <= MAX_BREAK_GLASS_HOURS + 0.1


def test_an_expired_window_reads_as_inactive_without_a_sweeper(h):
    """Expiry is evaluated at read time on purpose: a background job that
    fails to run must never be the reason a tenant stays unguarded."""
    h.client.post(BG, headers=h.token(), json={"reason": REASON})
    s = h.session()
    try:
        row = s.query(GuardrailBreakGlass).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.commit()
    finally:
        s.close()
    assert h.client.get(BG, headers=h.token()).json()["active"] is False


def test_an_expired_window_does_not_block_a_new_one(h):
    h.client.post(BG, headers=h.token(), json={"reason": REASON})
    s = h.session()
    try:
        row = s.query(GuardrailBreakGlass).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        s.commit()
    finally:
        s.close()
    r = h.client.post(BG, headers=h.token(), json={"reason": "second incident"})
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Attribution and history
# ---------------------------------------------------------------------------


def test_activating_twice_is_refused_so_the_log_shows_two_decisions(h):
    """Renewing must be an explicit deactivate-then-activate, or one window
    silently grows and the audit trail loses when the choice was re-made."""
    h.client.post(BG, headers=h.token(), json={"reason": REASON})
    r = h.client.post(
        BG, headers=h.token(), json={"reason": "second attempt during the same incident"}
    )
    assert r.status_code == 409


def test_deactivation_records_who_ended_it(h):
    h.client.post(BG, headers=h.token(), json={"reason": REASON})
    r = h.client.delete(BG, headers=h.token())
    assert r.json()["current"]["deactivated_by"] == "adm1"
    assert r.json()["current"]["deactivated_at"] is not None


def test_history_survives_deactivation(h):
    """The window ending must not erase that it happened — the assurance
    report has to be able to name the period when checks did not run."""
    h.client.post(BG, headers=h.token(), json={"reason": REASON})
    h.client.delete(BG, headers=h.token())
    state = h.client.get(BG, headers=h.token()).json()
    assert state["active"] is False
    assert len(state["history"]) == 1
    assert state["history"][0]["reason"] == REASON


def test_remaining_seconds_counts_down(h):
    r = h.client.post(BG, headers=h.token(), json={"reason": REASON})
    assert r.json()["remaining_seconds"] > 0


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_break_glass_is_per_tenant(h):
    """One tenant's incident must never degrade another tenant's checks."""
    s = h.session()
    try:
        s.add(
            Tenant(
                id="t2",
                name="t2",
                display_name="Tenant Two",
                primary_contact_email="admin@t2.example",
                is_active=True,
                is_default=False,
                is_trial=False,
                suspended=False,
                billing_plan="self_hosted",
            )
        )
        s.flush()
        _admin_role(s, "t2")
        s.commit()
        h.auth.create_tenant_admin(
            username="adm2",
            email="adm2@t2.example",
            password="Secret123!",
            tenant_id="t2",
            db=s,
            role_id="role-t2-admin",
        )
        s.commit()
    finally:
        s.close()

    h.client.post(BG, headers=h.token(), json={"reason": REASON})
    assert h.client.get(BG, headers=h.token()).json()["active"] is True
    other = h.client.get(BG, headers=h.token("adm2", "t2")).json()
    assert other["active"] is False

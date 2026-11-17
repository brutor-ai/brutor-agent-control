# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What a refused request leaves in the database.

A request that refuses (a 4xx or 5xx raised out of the handler) is rolled
back. Whatever it had flushed is gone — unless the code committed it on
purpose. Whatever ANOTHER session committed meanwhile stays: the audit trail
writes each record through a session of its own.

Most of the suite cannot see the difference: its database is one in-memory
SQLite connection shared by every session, on which the audit trail's commit
also commits what the request had flushed (`tests/refusal_write_detector.py`
watches for exactly that and fails the test in which it happens). Here the
cases run on a database with a connection per session — a SQLite file, or
Postgres with BRUTOR_PG_TESTS=1 — as a server has.

  refuse, and it MUST persist                      held by
  ------------------------------------------------ -----------------------------
  a wrong password is counted (sign-in)            lockout.count_failure commits
  a wrong current password is counted              lockout.count_failure commits
  a wrong second-step code is counted              lockout.count_failure commits
  an expired session is marked ended               the verifier commits
  the audit record of a refusal                    the audit trail's own session

  refuse, and it must NOT persist
  ------------------------------------------------
  what a handler flushed before it refused (the probe route below)
  an assurance check edited and enabled in one request, refused at the cap:
  neither the edit nor its new version

The system administrator's sign-in is held by
`enterprise/tests/test_system_sign_in_lockout.py` on the same kind of database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin.assurance_checks import create_assurance_checks_router
from app.api.v1.endpoints.admin.mfa import create_2fa_login_router, create_me_router, create_mfa_router
from app.core.admin_user.dependencies import get_audit_logger, get_current_admin
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin, TenantAdminAuditLog, TenantAdminSession, TenantRole
from app.database.models.assurance_check import AssuranceCheck, AssuranceCheckVersion
from app.database.models.tenant import Tenant
from app.services.assurance_checks import core_client
from tests import refusal_write_detector
from tests.admin_endpoint_harness import PASSWORD, build_admin_harness, throwaway_pg_database
from tests.test_mfa_policy_enforcement import give_2fa

LOGIN = "/v1/admin/users/tenant/login"
LOGIN_2FA = "/v1/admin/users/login/2fa"
CHANGE = "/v1/admin/users/me/change-password"
ME = "/v1/admin/users/me"
WRONG = "Wrong-password-1!"
PROBE = "/v1/f442-probe"


def _probe_router() -> APIRouter:
    """A handler of the shape that once hid the sign-in lockout's lost
    count: flush a change, let the audit
    trail record the refusal, refuse."""
    router = APIRouter()

    @router.post("/f442-probe/{name}")
    def flush_audit_refuse(
        name: str,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit=Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        db.add(TenantRole(id=f"role-{name}", tenant_id=admin.tenant_id, name=name, description="probe", is_system=False))
        db.flush()
        audit.log(action="f442.probe", db=db, admin=admin, admin_type=admin_type, success=False, resource_id=name)
        raise HTTPException(status_code=409, detail="refused after a flush")

    return router


def _routers():
    return [
        create_me_router(),
        create_mfa_router(),
        create_2fa_login_router(),
        create_assurance_checks_router(BrutorLogger("refusal-semantics")),
        _probe_router(),
    ]


@pytest.fixture()
def h(tmp_path):
    """A connection per session. (`timeout`: see `tests/test_sign_in_lockout.py`.)"""
    for url in throwaway_pg_database("refusal_semantics"):
        harness = build_admin_harness(
            _routers(), logger_name="refusal-semantics",
            database_url=url or f"sqlite:///{tmp_path / 'refusal.db'}?timeout=0.05",
        )
        yield harness
        harness.close()


@pytest.fixture()
def shared():
    """The suite's usual database: ONE connection for every session."""
    harness = build_admin_harness(_routers(), logger_name="refusal-semantics")
    yield harness
    harness.close()


def _admin(h, username="admin_t1"):
    s = h.Factory()
    try:
        return s.query(TenantAdmin).filter(TenantAdmin.username == username).one().failed_login_attempts
    finally:
        s.close()


def _role_exists(h, name) -> bool:
    s = h.Factory()
    try:
        return s.get(TenantRole, f"role-{name}") is not None
    finally:
        s.close()


def _audited(h, action, resource_id=None) -> int:
    s = h.Factory()
    try:
        q = s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.action == action)
        if resource_id is not None:
            q = q.filter(TenantAdminAuditLog.resource_id == resource_id)
        return q.count()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The premise, both ways
# ---------------------------------------------------------------------------


def test_a_refusal_loses_what_the_handler_flushed_and_keeps_its_audit_record(h):
    headers = h.headers("t1")
    r = h.client.post(f"{PROBE}/lost", headers=headers)
    assert r.status_code == 409
    assert _role_exists(h, "lost") is False
    # The audit trail's session is a second connection. Postgres locks rows
    # and lets it write; SQLite locks the whole file for the request's open
    # transaction, so there the record cannot be written and is dropped.
    if h.engine.dialect.name == "postgresql":
        assert _audited(h, "f442.probe", "lost") == 1


@pytest.mark.refusal_keeps_write(reason="shows what the shared connection does; the real semantics are the test above")
def test_the_shared_connection_keeps_it_and_the_detector_says_so(shared):
    """Why the routine suite needs the detector: the same request, on the
    suite's usual database, keeps the flushed row."""
    headers = shared.headers("t1")
    refusal_write_detector.EVENTS.clear()
    r = shared.client.post(f"{PROBE}/kept", headers=headers)
    assert r.status_code == 409
    assert _role_exists(shared, "kept") is True, "the shared connection no longer keeps it: the detector may go"
    events = list(refusal_write_detector.EVENTS)
    assert [(e.request, e.tables) for e in events] == [(f"POST {PROBE}/kept", ("tenant_roles",))]


# ---------------------------------------------------------------------------
# Refuse, and it must persist
# ---------------------------------------------------------------------------


def test_a_wrong_password_is_counted(h):
    for expected in (1, 2):
        r = h.client.post(LOGIN, json={"username": "admin_t1", "password": WRONG, "tenant_id": "t1"})
        assert r.status_code == 401
        assert _admin(h) == expected
    assert _audited(h, "tenant_admin_login_failed") == 2


def test_a_wrong_current_password_is_counted(h):
    headers = h.headers("t1")
    r = h.client.post(CHANGE, json={"current_password": WRONG, "new_password": "Another-Secret-9!"}, headers=headers)
    assert r.status_code == 401
    assert _admin(h) == 1


def test_a_wrong_second_step_code_is_counted(h):
    secret = give_2fa(h, "admin_t1")
    r = h.client.post(LOGIN, json={"username": "admin_t1", "password": PASSWORD, "tenant_id": "t1"})
    assert r.status_code == 200 and r.json().get("requires_2fa") is True, r.text
    token = r.json()["temp_token"]
    r = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": "000000"})
    assert r.status_code == 401
    assert _admin(h) == 1
    # ... and the right code afterwards starts from a clean count.
    r = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    assert _admin(h) == 0


def test_an_expired_session_is_marked_ended(h):
    """The verifier once marked an expired session ended with a
    flush and answered None; the request was then refused (401) and rolled
    back, so the row stayed `is_active` for good."""
    headers = h.headers("t1")
    assert h.client.get(ME, headers=headers).status_code == 200

    def expire(s):
        for row in s.query(TenantAdminSession).all():
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    h.seed(expire)
    from app.services import auth_read_model

    auth_read_model.clear_all()
    assert h.client.get(ME, headers=headers).status_code == 401
    s = h.Factory()
    try:
        rows = [(r.is_active, r.deactivation_reason) for r in s.query(TenantAdminSession).all()]
    finally:
        s.close()
    assert rows == [(False, "expired")]


# ---------------------------------------------------------------------------
# Refuse, and it must not persist
# ---------------------------------------------------------------------------


def test_an_assurance_check_refused_at_the_cap_keeps_neither_the_edit_nor_a_version(h, monkeypatch):
    monkeypatch.setattr(
        core_client, "compile_expression",
        lambda *a, **k: {"ok": True, "errors": [], "sample_value": False, "context_version": 2},
    )
    headers = h.headers("t1")

    def cap(s):
        s.query(Tenant).filter(Tenant.id == "t1").one().settings = {"assurance_checks": {"max_enabled": 1}}
    h.seed(cap)

    def create(name):
        r = h.client.post(
            "/v1/admin/assurance-checks", headers=headers,
            json={"name": name, "tier": "deterministic", "evaluator": {"expression": "run.action_count > 0"}},
        )
        assert r.status_code in (200, 201), r.text
        return r.json()

    first = create(f"on-{uuid.uuid4().hex[:6]}")
    r = h.client.patch(f"/v1/admin/assurance-checks/{first['id']}", headers=headers, json={"enabled": True})
    assert r.status_code == 200, r.text
    second = create(f"second-{uuid.uuid4().hex[:6]}")

    def state():
        s = h.Factory()
        try:
            row = s.get(AssuranceCheck, second["id"])
            versions = s.query(AssuranceCheckVersion).filter(AssuranceCheckVersion.check_id == second["id"]).count()
            return row.version, dict(row.evaluator), row.enabled, versions
        finally:
            s.close()

    before = state()
    r = h.client.patch(
        f"/v1/admin/assurance-checks/{second['id']}", headers=headers,
        json={"evaluator": {"expression": "run.error_count > 5"}, "enabled": True},
    )
    assert r.status_code == 422, r.text
    assert "cap" in r.json()["detail"]
    assert state() == before

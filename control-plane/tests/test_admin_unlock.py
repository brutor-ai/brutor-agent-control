# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unlocking an administrator, and what a lock does to the sessions the account
already has.

Before this, nothing unlocked an account: `UnifiedAuthService.unlock_admin`
had no caller, while the 423 of `change-password` says "contact an
administrator", and a lock without an end (`is_locked` with no
`locked_until`) never expires.

The rule, held here through the real routes and the real command:

  * `POST /v1/admin/users/tenant/{admin_id}/unlock`: an administrator with
    `admin_user:update` unlocks an administrator of its own tenant. The lock,
    its end and the count are cleared; the action is in the audit trail.
    Unknown id: 404. Another tenant's administrator: 403, nothing changed.
    Without the permission: 403.
  * `<entry> admin-unlock USERNAME [--tenant ID]`: the operator's way, for an
    installation whose only administrator is locked. It says what it did and
    prints no secret; it is recorded in the tenant's audit trail.
  * Sessions. A deactivated administrator's sessions are not ended: every
    request with them is refused (401) for as long as the account is
    deactivated. A LOCKED administrator's open sessions keep working: anyone
    who can reach the sign-in form can lock an account with five wrong
    guesses, and that must not throw a signed-in administrator out. The lock
    stops new sign-ins (password and second step) and nothing else.

The system administrator's half is `enterprise/tests/test_system_admin_unlock.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from app.api.v1.endpoints.admin.mfa import create_me_router
from app.core.admin_user import lockout
from app.core.admin_user.permissions import Permission
from app.database.models.admin import TenantAdmin, TenantAdminAuditLog
from tests.admin_endpoint_harness import PASSWORD, build_admin_harness, throwaway_pg_database

LOGIN = "/v1/admin/users/tenant/login"
ME = "/v1/admin/users/me"
WRONG = "Wrong-password-1!"


@pytest.fixture()
def h(tmp_path):
    """A database with a connection per session (see
    `tests/test_sign_in_lockout.py`): a file, or Postgres with BRUTOR_PG_TESTS=1."""
    for url in throwaway_pg_database("admin_unlock"):
        harness = build_admin_harness(
            [create_me_router()], logger_name="admin-unlock",
            database_url=url or f"sqlite:///{tmp_path / 'unlock.db'}?timeout=0.05",
        )
        yield harness
        harness.close()


@pytest.fixture()
def ha():
    """For what reads the audit record of a request that SUCCEEDED: the
    suite's usual database (or Postgres with BRUTOR_PG_TESTS=1). In a SQLite
    file the audit trail's own connection cannot write while the request's
    transaction holds the file, and the record is dropped; Postgres locks
    rows, and the shared in-memory connection has nothing to wait for."""
    for url in throwaway_pg_database("admin_unlock_audit"):
        harness = build_admin_harness([create_me_router()], logger_name="admin-unlock", database_url=url)
        yield harness
        harness.close()


def _admin(h, username):
    s = h.Factory()
    try:
        row = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
        return row.id, row.failed_login_attempts, row.is_locked, row.locked_until
    finally:
        s.close()


def _set(h, username, **values):
    def fn(s):
        row = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
        for k, v in values.items():
            setattr(row, k, v)
    h.seed(fn)


def _lock(h, username, tenant):
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        r = h.client.post(LOGIN, json={"username": username, "password": WRONG, "tenant_id": tenant})
        assert r.status_code == 401
    assert _admin(h, username)[2] is True


def _sign_in(h, username, tenant):
    return h.client.post(LOGIN, json={"username": username, "password": PASSWORD, "tenant_id": tenant})


def _audit(h, action):
    s = h.Factory()
    try:
        return [
            (r.tenant_id, r.admin_id, r.resource_id, r.success, dict(r.details or {}))
            for r in s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.action == action).all()
        ]
    finally:
        s.close()


def _second_admin(h, tenant="t1"):
    """A second administrator of `tenant`, signed in; returns (username, headers)."""
    headers = h.limited_headers(tenant, [Permission.ADMIN_USER_READ], username=f"second_{tenant}")
    return f"second_{tenant}", headers


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def test_an_administrator_unlocks_another_of_its_tenant(h):
    username, _ = _second_admin(h)
    _lock(h, username, "t1")
    target_id = _admin(h, username)[0]
    assert _sign_in(h, username, "t1").status_code == 401

    r = h.client.post(f"/v1/admin/users/tenant/{target_id}/unlock", headers=h.headers("t1"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["id"], body["username"], body["is_locked"]) == (target_id, username, False)
    assert _admin(h, username)[1:] == (0, False, None)
    assert _sign_in(h, username, "t1").status_code == 200


def test_a_lock_without_an_end_is_lifted_too(h):
    username, _ = _second_admin(h)
    _set(h, username, is_locked=True, locked_until=None, failed_login_attempts=5)
    assert _sign_in(h, username, "t1").status_code == 401
    target_id = _admin(h, username)[0]
    assert h.client.post(f"/v1/admin/users/tenant/{target_id}/unlock", headers=h.headers("t1")).status_code == 200
    assert _admin(h, username)[1:] == (0, False, None)
    assert _sign_in(h, username, "t1").status_code == 200


def test_the_unlock_is_in_the_audit_trail(ha):
    h = ha
    username, _ = _second_admin(h)
    _lock(h, username, "t1")
    target_id = _admin(h, username)[0]
    actor_id = _admin(h, "admin_t1")[0]
    assert h.client.post(f"/v1/admin/users/tenant/{target_id}/unlock", headers=h.headers("t1")).status_code == 200
    rows = _audit(h, "tenant_admin_unlocked")
    assert len(rows) == 1
    tenant_id, admin_id, resource_id, success, details = rows[0]
    assert (tenant_id, admin_id, resource_id, success) == ("t1", actor_id, target_id, True)
    assert details["unlocked_username"] == username
    assert details["was_locked"] is True
    assert details["failed_login_attempts"] == lockout.MAX_FAILED_ATTEMPTS


def test_unlocking_an_account_that_is_not_locked_changes_nothing_and_says_so(ha):
    h = ha
    username, _ = _second_admin(h)
    target_id = _admin(h, username)[0]
    r = h.client.post(f"/v1/admin/users/tenant/{target_id}/unlock", headers=h.headers("t1"))
    assert r.status_code == 200
    assert r.json()["is_locked"] is False
    assert [row[4]["was_locked"] for row in _audit(h, "tenant_admin_unlocked")] == [False]


def test_an_unknown_administrator_is_404(h):
    r = h.client.post("/v1/admin/users/tenant/no-such-id/unlock", headers=h.headers("t1"))
    assert r.status_code == 404
    assert _audit(h, "tenant_admin_unlocked") == []


def test_another_tenants_administrator_is_refused_and_stays_locked(h):
    _lock(h, "admin_t2", "t2")
    target_id, count, locked, until = _admin(h, "admin_t2")
    r = h.client.post(f"/v1/admin/users/tenant/{target_id}/unlock", headers=h.headers("t1"))
    assert r.status_code == 403
    assert _admin(h, "admin_t2") == (target_id, count, locked, until)
    assert _audit(h, "tenant_admin_unlocked") == []


def test_without_the_permission_it_is_refused(h):
    username, headers = _second_admin(h)  # holds admin_user:read only
    _lock(h, "admin_t1", "t1")
    target_id = _admin(h, "admin_t1")[0]
    r = h.client.post(f"/v1/admin/users/tenant/{target_id}/unlock", headers=headers)
    assert r.status_code == 403
    assert _admin(h, "admin_t1")[2] is True


def test_anonymous_is_refused(h):
    target_id = _admin(h, "admin_t1")[0]
    assert h.client.post(f"/v1/admin/users/tenant/{target_id}/unlock").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Sessions of a locked account
# ---------------------------------------------------------------------------


def test_a_deactivated_accounts_session_is_refused_and_answers_again_when_reactivated(h):
    """What the rule for a lock is modelled on."""
    username, headers = _second_admin(h)
    assert h.client.get(ME, headers=headers).status_code == 200
    _set(h, username, is_active=False)
    assert h.client.get(ME, headers=headers).status_code == 401
    _set(h, username, is_active=True)
    assert h.client.get(ME, headers=headers).status_code == 200


def test_a_signed_in_administrator_keeps_working_while_wrong_sign_ins_lock_the_account(h):
    """Five wrong guesses from anyone who can reach the sign-in form lock the
    account. They must not throw the signed-in administrator out: the lock
    stops new sign-ins, the open session goes on answering. Deactivation, an
    administrator's deliberate act, refuses the same session."""
    username, headers = _second_admin(h)
    assert h.client.get(ME, headers=headers).status_code == 200
    _lock(h, username, "t1")
    assert _sign_in(h, username, "t1").status_code == 401  # no new sign-in
    assert h.client.get(ME, headers=headers).status_code == 200  # the open session answers
    assert h.client.get("/v1/admin/users/tenant", headers=headers).status_code == 200  # an ordinary route too
    _set(h, username, is_active=False)
    assert h.client.get(ME, headers=headers).status_code == 401


def test_a_lock_without_an_end_does_not_end_the_open_session_either(h):
    username, headers = _second_admin(h)
    _set(h, username, is_locked=True, locked_until=None)
    assert _sign_in(h, username, "t1").status_code == 401
    assert h.client.get(ME, headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# The operator command
# ---------------------------------------------------------------------------


def _run(h, monkeypatch, *args):
    from app.services.admin_unlock import cli

    monkeypatch.setattr(cli, "_engine", lambda: h.engine)
    return CliRunner().invoke(cli.admin_unlock, list(args))


def test_the_command_unlocks_the_only_administrator(h, monkeypatch):
    _lock(h, "admin_t1", "t1")
    result = _run(h, monkeypatch, "admin_t1", "--tenant", "t1")
    assert result.exit_code == 0, result.output
    assert "admin_t1" in result.output and "t1" in result.output
    assert "UNLOCKED" in result.output
    assert _admin(h, "admin_t1")[1:] == (0, False, None)
    assert _sign_in(h, "admin_t1", "t1").status_code == 200


def test_the_command_lifts_a_lock_without_an_end(h, monkeypatch):
    _set(h, "admin_t1", is_locked=True, locked_until=None, failed_login_attempts=3)
    assert _run(h, monkeypatch, "admin_t1", "--tenant", "t1").exit_code == 0
    assert _admin(h, "admin_t1")[1:] == (0, False, None)


def test_the_command_is_recorded_in_the_tenants_audit_trail(h, monkeypatch):
    _lock(h, "admin_t1", "t1")
    target_id = _admin(h, "admin_t1")[0]
    assert _run(h, monkeypatch, "admin_t1", "--tenant", "t1").exit_code == 0
    rows = _audit(h, "tenant_admin_unlocked")
    assert len(rows) == 1
    tenant_id, admin_id, resource_id, success, details = rows[0]
    assert (tenant_id, admin_id, resource_id, success) == ("t1", None, target_id, True)
    assert details["unlocked_username"] == "admin_t1" and details["was_locked"] is True
    assert details["operator"]


def test_the_command_prints_no_secret(h, monkeypatch):
    _lock(h, "admin_t1", "t1")
    s = h.Factory()
    try:
        row = s.query(TenantAdmin).filter(TenantAdmin.username == "admin_t1").one()
        secrets = [row.password_hash, PASSWORD]
    finally:
        s.close()
    result = _run(h, monkeypatch, "admin_t1", "--tenant", "t1")
    for secret in secrets:
        assert secret not in result.output
    assert "postgresql://" not in result.output or ":throwaway@" not in result.output


def test_the_command_says_when_the_account_was_not_locked(h, monkeypatch):
    result = _run(h, monkeypatch, "admin_t1", "--tenant", "t1")
    assert result.exit_code == 0
    assert "was not locked" in result.output
    assert "UNLOCKED" not in result.output
    assert _audit(h, "tenant_admin_unlocked") == []


def test_the_command_refuses_an_unknown_administrator(h, monkeypatch):
    result = _run(h, monkeypatch, "nobody-of-that-name", "--tenant", "t1")
    assert result.exit_code == 2
    assert "No administrator" in result.output


def test_the_command_does_not_reach_into_another_tenant(h, monkeypatch):
    _lock(h, "admin_t2", "t2")
    result = _run(h, monkeypatch, "admin_t2", "--tenant", "t1")
    assert result.exit_code == 2
    assert _admin(h, "admin_t2")[2] is True


def test_the_tenant_defaults_to_the_installations_own():
    from app.services.admin_unlock import cli

    option = next(p for p in cli.admin_unlock.params if p.name == "tenant")
    assert option.default == "default"


def test_the_entry_point_knows_the_command(monkeypatch):
    """`<entry> admin-unlock ...` reaches the command and nothing else."""
    import sys

    from app import cli as entry
    from app.services.admin_unlock import cli

    seen = []
    monkeypatch.setattr(cli, "admin_unlock", lambda: seen.append(list(sys.argv)))
    monkeypatch.setattr(sys, "argv", ["main.py", "admin-unlock", "admin", "--tenant", "default"])
    entry.dispatch()
    assert seen == [["main.py", "admin", "--tenant", "default"]]


# ---------------------------------------------------------------------------
# The Community Edition knows no system administrator: the list
# of system administrators and `admin-unlock --system` are the Enterprise
# Edition's (`enterprise/tests/test_system_admin_list_and_cli_unlock.py`).
# ---------------------------------------------------------------------------


def test_the_community_command_has_no_system_option(h, monkeypatch):
    _lock(h, "admin_t1", "t1")
    result = _run(h, monkeypatch, "--system", "admin_t1")
    assert result.exit_code == 2
    assert "No such option" in result.output and "--system" in result.output
    assert _admin(h, "admin_t1")[2] is True


def test_the_community_command_names_no_system_administrator():
    from pathlib import Path

    from app.services.admin_unlock import cli

    assert [p.name for p in cli.admin_unlock.params] == ["username", "tenant"]
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "SystemAdmin" not in source and "--system" not in source
    assert "system" not in (Path(cli.__file__).parents[2] / "cli.py").read_text(encoding="utf-8").lower()


def test_the_community_application_lists_no_system_administrators():
    from tests.app_factory import create_community_app

    paths = create_community_app().openapi()["paths"]
    assert "/v1/admin/users/tenant" in paths
    assert [p for p in paths if p.startswith("/v1/admin/users/system")] == []

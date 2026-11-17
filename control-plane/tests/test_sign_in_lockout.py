# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Account lockout on the password step of a sign-in (security).

`login_tenant_admin` counted a wrong password with `failed_login_attempts += 1`
and a flush, then returned None; the route answered 401 by raising, and the
request's session was rolled back. The count never reached the database: the
column stayed 0 and the lock never engaged, for any number of guesses.

The rule, held here through the real route (`POST /v1/admin/users/tenant/login`;
the system administrator's route is held by the same cases in
`enterprise/tests/test_system_sign_in_lockout.py`):

  * a wrong password is counted, and the count survives the refusal;
  * the fifth consecutive wrong password locks the account for
    `LOCKOUT_MINUTES`; a locked account is refused BEFORE its password is
    looked at, so the right password is refused too;
  * the lock ends by itself when `locked_until` has passed (the next sign-in
    releases it), or when an administrator unlocks the account; a lock without
    an end (`locked_until` NULL) is an administrator's and does not expire;
  * a successful sign-in resets the count;
  * unknown user, wrong password, locked account and deactivated account
    answer the same status and the same body, and each costs exactly one
    bcrypt verification (no cheaper path that tells them apart by time);
  * a wrong CURRENT password on `change-password` counts on the same counter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import pytest

from app.api.v1.endpoints.admin.mfa import create_me_router
from app.core.admin_user import lockout
from app.database.models.admin import TenantAdmin
from tests.admin_endpoint_harness import PASSWORD, build_admin_harness, throwaway_pg_database

LOGIN = "/v1/admin/users/tenant/login"
USER = "admin_t1"
WRONG = "Wrong-password-1!"


@pytest.fixture()
def h(tmp_path):
    """NOT the harness's default database. That one is a single in-memory
    SQLite connection shared by every session, so the audit trail's own
    commit (a failed sign-in is audited) also commits whatever the request's
    session had flushed — which is how a lost failure count once passed every test: there, the
    count survived the rollback. A file database (or Postgres, with
    BRUTOR_PG_TESTS=1) gives each session its own connection, as a server
    has.

    (`timeout`: SQLite locks the whole file for a writer, so the audit row of
    a SUCCESSFUL sign-in, written on a second connection while the request's
    transaction is open, cannot be written there and is dropped after the
    wait. Nothing here reads it; the wait is kept short. Postgres locks rows.)"""
    for url in throwaway_pg_database("sign_in_lockout"):
        harness = build_admin_harness(
            [create_me_router()], logger_name="sign-in-lockout",
            database_url=url or f"sqlite:///{tmp_path / 'lockout.db'}?timeout=0.05",
        )
        yield harness
        harness.close()


def _sign_in(h, password=PASSWORD, username=USER, tenant="t1"):
    return h.client.post(LOGIN, json={"username": username, "password": password, "tenant_id": tenant})


def _row(h, username=USER):
    s = h.Factory()
    try:
        row = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
        return row.failed_login_attempts, row.is_locked, row.locked_until
    finally:
        s.close()


def _set(h, username=USER, **values):
    def fn(s):
        row = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
        for k, v in values.items():
            setattr(row, k, v)
    h.seed(fn)


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def test_the_design_values():
    assert lockout.MAX_FAILED_ATTEMPTS == 5
    assert lockout.LOCKOUT_MINUTES == 15


def test_a_wrong_password_is_counted_and_the_count_survives_the_refusal(h):
    for expected in (1, 2, 3):
        assert _sign_in(h, WRONG).status_code == 401
        assert _row(h)[0] == expected
    assert _row(h)[1] is False


def test_the_fifth_wrong_password_locks_the_account(h):
    before = datetime.now(timezone.utc)
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        assert _sign_in(h, WRONG).status_code == 401
    count, locked, until = _row(h)
    assert (count, locked) == (lockout.MAX_FAILED_ATTEMPTS, True)
    span = _aware(until) - before
    assert timedelta(minutes=lockout.LOCKOUT_MINUTES - 1) < span <= timedelta(minutes=lockout.LOCKOUT_MINUTES, seconds=30)


def test_four_wrong_passwords_do_not_lock(h):
    for _ in range(lockout.MAX_FAILED_ATTEMPTS - 1):
        assert _sign_in(h, WRONG).status_code == 401
    assert _row(h)[1] is False
    assert _sign_in(h).status_code == 200


def test_a_locked_account_is_refused_with_the_right_password(h):
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        _sign_in(h, WRONG)
    wrong = _sign_in(h, WRONG)
    right = _sign_in(h)
    assert right.status_code == 401
    assert (right.status_code, right.json()) == (wrong.status_code, wrong.json())
    assert "access_token" not in right.text
    # Refused attempts on a locked account neither extend the lock nor count.
    assert _row(h)[0] == lockout.MAX_FAILED_ATTEMPTS


def test_the_lock_is_looked_at_before_the_password(h, monkeypatch):
    """A locked account's stored hash is never compared with what was sent:
    the one verification a refusal costs is against the placeholder hash."""
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        _sign_in(h, WRONG)
    s = h.Factory()
    stored = s.query(TenantAdmin).filter(TenantAdmin.username == USER).one().password_hash.encode()
    s.close()
    seen = []
    real = bcrypt.checkpw
    monkeypatch.setattr(bcrypt, "checkpw", lambda pw, hashed: seen.append(hashed) or real(pw, hashed))
    assert _sign_in(h).status_code == 401
    assert len(seen) == 1 and stored not in seen


def test_a_successful_sign_in_resets_the_count(h):
    for _ in range(3):
        _sign_in(h, WRONG)
    assert _row(h)[0] == 3
    assert _sign_in(h).status_code == 200
    assert _row(h) == (0, False, None)
    # so three more wrong passwords do not reach the threshold
    for _ in range(3):
        _sign_in(h, WRONG)
    assert _row(h)[:2] == (3, False)
    assert _sign_in(h).status_code == 200


def test_the_lock_expires(h):
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        _sign_in(h, WRONG)
    # one second before the end: still refused
    _set(h, locked_until=datetime.now(timezone.utc) + timedelta(seconds=60))
    assert _sign_in(h).status_code == 401
    # the end has passed: the right password signs in and the slate is clean
    _set(h, locked_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert _sign_in(h).status_code == 200
    assert _row(h) == (0, False, None)


def test_an_expired_lock_starts_a_new_count(h):
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        _sign_in(h, WRONG)
    _set(h, locked_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert _sign_in(h, WRONG).status_code == 401
    assert _row(h)[:2] == (1, False)


def test_a_lock_without_an_end_does_not_expire(h):
    """`is_locked` with no `locked_until` is an administrator's lock."""
    _set(h, is_locked=True, locked_until=None)
    assert _sign_in(h).status_code == 401
    assert _row(h)[1] is True


def test_unlocking_ends_the_lock(h):
    from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
    from tests.admin_endpoint_harness import JWT_SECRET

    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        _sign_in(h, WRONG)

    def unlock(s):
        row = s.query(TenantAdmin).filter(TenantAdmin.username == USER).one()
        assert UnifiedAuthService(jwt_secret=JWT_SECRET).unlock_admin(row.id, "tenant", tenant_id="t1", db=s)
    h.seed(unlock)
    assert _row(h) == (0, False, None)
    assert _sign_in(h).status_code == 200


def test_every_refusal_answers_the_same(h):
    """Unknown user, wrong password, locked, deactivated: one status, one
    body. Nothing in the answer says which it was, or that the user exists."""
    h.seed(lambda s: None)
    answers = {
        "unknown user": _sign_in(h, WRONG, username="nobody-of-that-name"),
        "unknown tenant": _sign_in(h, WRONG, tenant="no-such-tenant"),
        "wrong password": _sign_in(h, WRONG),
    }
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        _sign_in(h, WRONG)
    answers["locked, wrong password"] = _sign_in(h, WRONG)
    answers["locked, right password"] = _sign_in(h)
    _set(h, "admin_t2", is_active=False)
    answers["deactivated"] = _sign_in(h, username="admin_t2", tenant="t2")
    seen = {(r.status_code, r.text) for r in answers.values()}
    assert seen == {(401, '{"detail":"Invalid credentials"}')}, {k: (r.status_code, r.text) for k, r in answers.items()}
    headers = {tuple(sorted(k.lower() for k in r.headers)) for r in answers.values()}
    assert len(headers) == 1, headers


@pytest.mark.parametrize("case", ["unknown user", "wrong password", "locked", "deactivated", "right password"])
def test_every_sign_in_costs_one_password_verification(h, monkeypatch, case):
    """The time a refusal takes is one bcrypt verification whatever the
    reason, the same as an accepted password: an unknown or locked account is
    verified against a placeholder hash of the same cost."""
    if case == "locked":
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            _sign_in(h, WRONG)
    if case == "deactivated":
        _set(h, is_active=False)
    calls = []
    real = bcrypt.checkpw

    def spy(password, hashed):
        calls.append(bcrypt.gensalt and hashed[:7])
        return real(password, hashed)

    monkeypatch.setattr(bcrypt, "checkpw", spy)
    if case == "unknown user":
        r = _sign_in(h, WRONG, username="nobody-of-that-name")
    elif case == "right password":
        r = _sign_in(h)
    else:
        r = _sign_in(h, WRONG)
    assert r.status_code == (200 if case == "right password" else 401)
    assert len(calls) == 1, calls
    # the placeholder costs what a stored hash costs
    s = h.Factory()
    stored_cost = s.query(TenantAdmin).filter(TenantAdmin.username == USER).one().password_hash.encode()[:7]
    s.close()
    assert calls == [stored_cost]


def test_one_accounts_failures_do_not_touch_another(h):
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        _sign_in(h, WRONG)
    assert _row(h, "admin_t2")[:2] == (0, False)
    assert _sign_in(h, username="admin_t2", tenant="t2").status_code == 200


def test_an_unknown_user_creates_no_row_and_no_count(h):
    for _ in range(lockout.MAX_FAILED_ATTEMPTS + 1):
        assert _sign_in(h, WRONG, username="nobody-of-that-name").status_code == 401
    s = h.Factory()
    try:
        assert s.query(TenantAdmin).filter(TenantAdmin.username == "nobody-of-that-name").count() == 0
        assert {r.failed_login_attempts for r in s.query(TenantAdmin).all()} == {0}
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The other route that verifies a password: change-password (signed in)
# ---------------------------------------------------------------------------

CHANGE = "/v1/admin/users/me/change-password"


def test_a_wrong_current_password_counts_and_locks(h):
    headers = h.headers("t1")
    body = {"current_password": WRONG, "new_password": "Another-Secret-9!"}
    for expected in range(1, lockout.MAX_FAILED_ATTEMPTS):
        r = h.client.post(CHANGE, json=body, headers=headers)
        assert r.status_code == 401, r.text
        assert _row(h)[:2] == (expected, False)
    r = h.client.post(CHANGE, json=body, headers=headers)
    assert r.status_code == 423, r.text
    assert _row(h)[:2] == (lockout.MAX_FAILED_ATTEMPTS, True)
    # locked: the right current password changes nothing either
    r = h.client.post(
        CHANGE, json={"current_password": PASSWORD, "new_password": "Another-Secret-9!"}, headers=headers
    )
    assert r.status_code == 423, r.text
    _set(h, is_locked=False, failed_login_attempts=0, locked_until=None)
    assert _sign_in(h).status_code == 200  # the password is still the old one


def test_a_right_current_password_resets_the_count(h):
    headers = h.headers("t1")
    for _ in range(2):
        h.client.post(CHANGE, json={"current_password": WRONG, "new_password": "Another-Secret-9!"}, headers=headers)
    assert _row(h)[0] == 2
    r = h.client.post(
        CHANGE, json={"current_password": PASSWORD, "new_password": "Another-Secret-9!"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert _row(h) == (0, False, None)


# ---------------------------------------------------------------------------
# The listing shows the lock as it is now
# ---------------------------------------------------------------------------


def test_to_dict_reports_an_expired_lock_as_not_locked():
    row = TenantAdmin(
        id="a", tenant_id="t1", role_id="r", username="u", email="u@x.invalid", password_hash="x",
        is_active=True, is_locked=True, failed_login_attempts=5,
        locked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert row.to_dict()["is_locked"] is False
    row.locked_until = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert row.to_dict()["is_locked"] is True
    row.locked_until = None
    assert row.to_dict()["is_locked"] is True

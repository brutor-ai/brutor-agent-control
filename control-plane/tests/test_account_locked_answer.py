# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
What a 423 says, and that the OpenAPI document declares it.

Every 423 carried one text, "Account is locked after too many failed
attempts. Contact an administrator." — also for a lock that ends by itself
after `LOCKOUT_MINUTES`, and for a deactivated account; and no operation
declared the status in the OpenAPI document.

The answer now says which case it is, in words and in a field a client can
key on:

    {"detail": "<text>", "reason": "<reason>", "locked_until": "<ISO 8601>" | null,
     "lockout_minutes": 15}

    reason             when                                   the text says
    timed_lock         `is_locked` with a `locked_until`      when it ends, that it lasts
                                                              15 minutes, that an administrator
                                                              can lift it earlier
    lock_without_end   `is_locked`, no `locked_until`         an administrator must unlock it
    deactivated        `is_active` false                      an administrator must reactivate it

The routes that answer 423: the second step of a sign-in
(`/admin/users/login/2fa`), the forced enrolment (`.../enroll/setup`,
`.../enroll/verify`) and `/admin/users/me/change-password`. Each declares it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pyotp
import pytest

from app.core import account_lock
from app.core.admin_user import lockout
from app.database.models.admin import TenantAdmin
from tests.admin_endpoint_harness import PASSWORD
from tests.test_mfa_policy_enforcement import (
    ENROLL_SETUP,
    ENROLL_VERIFY,
    LOGIN_2FA,
    bearer,
    give_2fa,
    harness_with,
    password_login,
    session_token,
)

CHANGE = "/v1/admin/users/me/change-password"
OLD_TEXT = "Contact an administrator"


@pytest.fixture()
def h():
    yield from harness_with()


def _set(h, **values):
    def fn(s):
        s.query(TenantAdmin).filter(TenantAdmin.username == "admin_t1").update(values)
    h.seed(fn)


def _second_step(h):
    """A 2FA sign-in stopped after the password: (temp token, secret)."""
    secret = give_2fa(h, "admin_t1")
    r = password_login(h)
    assert r.status_code == 200 and r.json().get("requires_2fa") is True, r.text
    return r.json()["temp_token"], secret


def _parse(text):
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def test_the_reasons_are_these_three():
    assert account_lock.REASONS == ("timed_lock", "lock_without_end", "deactivated")


def test_a_timed_lock_says_when_it_ends(h):
    token, secret = _second_step(h)
    until = datetime.now(timezone.utc) + timedelta(minutes=9)
    _set(h, is_locked=True, locked_until=until, failed_login_attempts=5)
    r = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 423, r.text
    body = r.json()
    assert set(body) == {"detail", "reason", "locked_until", "lockout_minutes"}
    assert body["reason"] == "timed_lock"
    assert abs(_parse(body["locked_until"]) - until) < timedelta(seconds=1)
    assert body["lockout_minutes"] == lockout.LOCKOUT_MINUTES
    assert isinstance(body["detail"], str)
    assert f"{lockout.LOCKOUT_MINUTES} minutes" in body["detail"]
    assert body["locked_until"] in body["detail"]
    assert "administrator can" in body["detail"]
    assert OLD_TEXT not in body["detail"]


def test_a_lock_without_an_end_says_an_administrator_must_unlock_it(h):
    token, secret = _second_step(h)
    _set(h, is_locked=True, locked_until=None)
    r = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 423, r.text
    body = r.json()
    assert (body["reason"], body["locked_until"]) == ("lock_without_end", None)
    assert "administrator must unlock" in body["detail"]
    assert "minutes" not in body["detail"]


def test_a_deactivated_account_says_so(h):
    token, secret = _second_step(h)
    _set(h, is_active=False)
    r = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 423, r.text
    body = r.json()
    assert (body["reason"], body["locked_until"]) == ("deactivated", None)
    assert "deactivated" in body["detail"]
    assert "locked" not in body["detail"]


def test_deactivated_wins_over_a_lock(h):
    token, secret = _second_step(h)
    _set(h, is_active=False, is_locked=True, locked_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    r = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.json()["reason"] == "deactivated"


def test_the_wrong_code_that_locks_answers_a_timed_lock(h):
    token, _secret = _second_step(h)
    before = datetime.now(timezone.utc)
    last = None
    for _ in range(5):
        last = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": "000000"})
    assert last.status_code == 423, last.text
    body = last.json()
    assert body["reason"] == "timed_lock"
    span = _parse(body["locked_until"]) - before
    assert timedelta(minutes=lockout.LOCKOUT_MINUTES - 1) < span <= timedelta(minutes=lockout.LOCKOUT_MINUTES, seconds=30)


def test_the_wrong_current_password_that_locks_answers_a_timed_lock(h):
    headers = bearer(session_token(h))
    body = {"current_password": "Wrong-password-1!", "new_password": "Another-Secret-9!"}
    last = None
    for _ in range(lockout.MAX_FAILED_ATTEMPTS):
        last = h.client.post(CHANGE, json=body, headers=headers)
    assert last.status_code == 423, last.text
    answer = last.json()
    assert answer["reason"] == "timed_lock" and answer["locked_until"]
    assert OLD_TEXT not in answer["detail"]
    assert PASSWORD not in last.text


def test_the_application_answers_through_the_same_handler():
    """The harness installs it as the application factory does."""
    from tests.app_factory import create_app

    app = create_app()
    assert app.exception_handlers.get(account_lock.AccountLocked) is account_lock.account_locked_handler


# ---------------------------------------------------------------------------
# The OpenAPI document declares it
# ---------------------------------------------------------------------------

ANSWER_423 = [
    ("post", LOGIN_2FA),
    ("post", ENROLL_SETUP),
    ("post", ENROLL_VERIFY),
    ("post", CHANGE),
]


@pytest.fixture(scope="module")
def document():
    for harness in harness_with():
        yield harness.app.openapi()


@pytest.mark.parametrize("method,path", ANSWER_423)
def test_the_operations_that_answer_423_declare_it(document, method, path):
    responses = document["paths"][path][method]["responses"]
    assert "423" in responses, sorted(responses)
    schema = responses["423"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/AccountLockedResponse"}


def test_the_declared_body_is_the_one_that_is_sent(document):
    schema = document["components"]["schemas"]["AccountLockedResponse"]
    assert set(schema["properties"]) == {"detail", "reason", "locked_until", "lockout_minutes"}
    assert schema["properties"]["reason"]["enum"] == list(account_lock.REASONS)
    assert set(schema["required"]) == {"detail", "reason"}


def test_change_password_declares_its_other_refusals(document):
    responses = document["paths"][CHANGE]["post"]["responses"]
    assert {"200", "400", "401", "422", "423"} <= set(responses)


def test_the_unlock_route_declares_what_it_answers(document):
    responses = document["paths"]["/v1/admin/users/tenant/{admin_id}/unlock"]["post"]["responses"]
    assert set(responses) == {"200", "401", "403", "404", "422"}
    assert responses["200"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/AdminResponse"}

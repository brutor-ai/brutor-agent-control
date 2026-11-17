# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`tenants.mfa_policy` is saved, read back and enforced.

The column existed and the Admin Console drew a selector for it, but no update
model declared it, `Tenant.to_dict()` did not return it and nothing read it at
sign-in: a security control that looked set and did nothing. This file is the
contract of the wired control:

* the policy is one of `optional`, `required_admins`, `required_all`; read
  through one helper (NULL / empty is `optional`, anything unrecognised is
  `required_all`);
* a tenant administrator can save it on its own tenant, but a value other
  than `optional` only once its own account has 2FA (409 otherwise). A system
  administrator sets it by id without that condition;
* when the policy requires 2FA for administrators, a password sign-in of an
  administrator without it issues NO session: it answers an enrolment
  challenge whose token (`type: 2fa_enrollment`, ten minutes) is accepted by
  `POST /login/2fa/enroll/setup` and `/verify` and by nothing else;
* 2FA cannot be disabled while the policy requires it;
* tightening the policy ends the sessions of the people it now excludes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pyotp
import pytest

from app.api.v1.endpoints.admin.mfa import (
    create_2fa_login_router,
    create_me_router,
    create_mfa_router,
)
from app.api.v1.endpoints.admin.tenant_settings import create_tenant_settings_router
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.core.logging_system import BrutorLogger
from app.database.models.admin import (
    SystemAdminSession,
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
)
from app.database.models.end_user import EndUser
from app.database.models.tenant import Tenant
from app.services.mfa.totp_service import seal_totp_secret
from tests.admin_endpoint_harness import JWT_SECRET, PASSWORD, build_admin_harness

LOGIN = "/v1/admin/users/tenant/login"
SYS_LOGIN = "/v1/admin/users/system/login"
LOGIN_2FA = "/v1/admin/users/login/2fa"
ENROLL_SETUP = "/v1/admin/users/login/2fa/enroll/setup"
ENROLL_VERIFY = "/v1/admin/users/login/2fa/enroll/verify"
CURRENT = "/v1/admin/tenants/current"
ME = "/v1/admin/users/me"
DISABLE = "/v1/admin/users/me/2fa/disable"

POLICIES = ("optional", "required_admins", "required_all")
REQUIRING = ("required_admins", "required_all")

# What a sign-in that issued a session answers (`login_tenant_admin`).
SESSION_KEYS = {
    "access_token", "token_type", "expires_in", "admin_type", "tenant_id", "tenant_display_name",
    "is_trial", "trial_ends_at", "suspended", "suspension_reason", "password_change_required", "admin",
}
CHALLENGE_KEYS = {
    "requires_2fa_enrollment", "temp_token", "admin_type", "tenant_id", "tenant_display_name", "admin",
}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def forged(token_type: str, admin_id: str, *, tenant_id="t1", admin_type="tenant", minutes: int = 10,
           secret: str = JWT_SECRET) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"type": token_type, "admin_id": admin_id, "tenant_id": tenant_id, "admin_type": admin_type,
         "iat": now, "exp": now + timedelta(minutes=minutes)},
        secret, algorithm="HS256",
    )


# ---------------------------------------------------------------------------
# The helper: one reading of a stored value.
# ---------------------------------------------------------------------------


class TestReadingAStoredPolicy:
    @pytest.mark.parametrize("stored", [None, "", "   "])
    def test_nothing_stored_is_optional(self, stored):
        from app.services.mfa.policy import effective_mfa_policy

        assert effective_mfa_policy(stored) == "optional"

    @pytest.mark.parametrize("stored", POLICIES)
    def test_a_known_value_is_itself(self, stored):
        from app.services.mfa.policy import effective_mfa_policy

        assert effective_mfa_policy(stored) == stored

    @pytest.mark.parametrize("stored", ["required", "REQUIRED_ADMINS", "off", "none", "disabled", "0", "optional "])
    def test_an_unrecognised_value_is_the_most_restrictive(self, stored):
        from app.services.mfa.policy import effective_mfa_policy

        assert effective_mfa_policy(stored) == "required_all"

    def test_who_each_policy_covers(self):
        from app.services.mfa.policy import requires_2fa_for_admins, requires_2fa_for_end_users

        assert [requires_2fa_for_admins(p) for p in POLICIES] == [False, True, True]
        assert [requires_2fa_for_end_users(p) for p in POLICIES] == [False, False, True]
        # Unrecognised: everyone.
        assert requires_2fa_for_admins("bogus") and requires_2fa_for_end_users("bogus")

    def test_stricter_is_an_order(self):
        from app.services.mfa.policy import is_stricter

        assert is_stricter("required_admins", than="optional")
        assert is_stricter("required_all", than="optional")
        assert is_stricter("required_all", than="required_admins")
        for p in POLICIES:
            assert not is_stricter(p, than=p)
        assert not is_stricter("optional", than="required_admins")
        assert not is_stricter("required_admins", than="required_all")
        # Nothing is stricter than an unrecognised value: it already means everyone.
        assert not is_stricter("required_all", than="bogus")

    def test_the_compliance_resolver_reads_through_the_helper(self, monkeypatch):
        """`authz.human_identity_verified` judged an unrecognised policy as
        `optional` (it compared the raw string with the two known values)."""
        from app.services.compliance.resolvers import oversight

        rows = [("ar-1", "approved", "admin-1")]
        monkeypatch.setattr(oversight, "_approvals", lambda ctx: rows)
        monkeypatch.setattr(oversight, "_principal", lambda ctx, who: ("user", False))

        def outcome(stored):
            db = SimpleNamespace(get=lambda model, key: SimpleNamespace(mfa_policy=stored))
            return oversight.human_identity_verified(SimpleNamespace(db=db, tenant_id="t1"), None)

        bogus = outcome("bogus")
        assert bogus.facts["mfa_policy"] == "required_all"
        assert (bogus.n, bogus.m, bogus.facts["missing_required_2fa"]) == (0, 1, 1)
        assert outcome(None).facts["mfa_policy"] == "optional"
        assert outcome("").facts["mfa_policy"] == "optional"
        assert outcome("required_admins").facts["missing_required_2fa"] == 0  # an end user


# ---------------------------------------------------------------------------
# Rule 1 — it saves and loads.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule 2 — no self lock-out by surprise.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule 3 — sign-in.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule 4 — disabling.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule 5 — tightening ends sessions.
# ---------------------------------------------------------------------------


def harness_with(extra_routers=()):
    """The file's harness: tenant settings, the signed-in administrator's own
    routes and the two-factor routes; `extra_routers` are mounted after the
    tenant-settings router (the Enterprise half adds tenant management)."""
    log = BrutorLogger("test-mfa-policy")
    harness = build_admin_harness(
        [
            create_tenant_settings_router(log),
            *extra_routers,
            create_me_router(),
            create_mfa_router(),
            create_2fa_login_router(),
        ],
        logger_name="test-mfa-policy",
    )
    yield harness
    harness.close()


@pytest.fixture()
def h():
    yield from harness_with()


def set_policy(h, tenant_id: str, value) -> None:
    """Write the column directly: the state the test starts from."""
    def _w(s):
        s.query(Tenant).filter(Tenant.id == tenant_id).update({"mfa_policy": value})
    h.seed(_w)


def stored_policy(h, tenant_id: str):
    s = h.Factory()
    try:
        return s.get(Tenant, tenant_id).mfa_policy
    finally:
        s.close()


def admin_row(h, username: str) -> SimpleNamespace:
    s = h.Factory()
    try:
        a = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
        return SimpleNamespace(
            id=a.id, totp_enabled=a.totp_enabled, totp_secret=a.totp_secret, backup_codes=a.backup_codes,
            failed_login_attempts=a.failed_login_attempts, is_locked=a.is_locked, last_login=a.last_login,
        )
    finally:
        s.close()


def give_2fa(h, username: str) -> str:
    """Enrol `username` in 2FA directly; returns the TOTP secret."""
    secret = pyotp.random_base32()

    def _w(s):
        a = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
        a.totp_secret = seal_totp_secret(secret)
        a.totp_enabled = True
        a.totp_verified_at = datetime.now(timezone.utc)
    h.seed(_w)
    return secret


def add_admin(h, tenant_id: str, username: str) -> None:
    def _w(s):
        UnifiedAuthService(jwt_secret=JWT_SECRET).create_tenant_admin(
            username=username, email=f"{username}@{tenant_id}.example", password=PASSWORD,
            tenant_id=tenant_id, db=s, role_id=f"role-{tenant_id}-admin",
        )
    h.seed(_w)


def add_end_user(h, tenant_id: str, username: str, totp: bool = False) -> int:
    out = {}

    def _w(s):
        u = EndUser(
            tenant_id=tenant_id, username=username, email=f"{username}@{tenant_id}.example",
            password_hash="x", totp_enabled=totp, totp_secret=("sealed" if totp else None),
        )
        s.add(u)
        s.flush()
        out["id"] = u.id
    h.seed(_w)
    return out["id"]


def end_user_cutoff(h, user_id: int):
    s = h.Factory()
    try:
        return s.get(EndUser, user_id).sessions_valid_after
    finally:
        s.close()


def password_login(h, username: str = "admin_t1", tenant_id="t1"):
    body = {"username": username, "password": PASSWORD}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    return h.client.post(LOGIN, json=body)


def session_token(h, username: str = "admin_t1", tenant_id: str = "t1") -> str:
    """Sign in to a session: password, then the code when 2FA is enabled."""
    r = password_login(h, username, tenant_id)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def session_token_2fa(h, secret: str, username: str = "admin_t1", tenant_id: str = "t1") -> str:
    r = password_login(h, username, tenant_id)
    assert r.status_code == 200 and r.json().get("requires_2fa") is True, r.text
    r = h.client.post(LOGIN_2FA, json={"temp_token": r.json()["temp_token"], "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def enrolment_challenge(h, username: str = "admin_t1", tenant_id="t1") -> dict:
    r = password_login(h, username, tenant_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("requires_2fa_enrollment") is True, body
    return body


def active_sessions(h, tenant_id: str = "t1") -> int:
    s = h.Factory()
    try:
        return (
            s.query(TenantAdminSession)
            .filter(TenantAdminSession.tenant_id == tenant_id, TenantAdminSession.is_active.is_(True))
            .count()
        )
    finally:
        s.close()


class TestATenantAdminNeedsItsOwn2FAFirst:
    @pytest.mark.parametrize("value", REQUIRING)
    def test_without_2fa_a_requiring_policy_is_refused(self, h, value):
        r = h.client.patch(CURRENT, json={"mfa_policy": value}, headers=h.headers("t1"))
        assert r.status_code == 409, r.text
        assert "enable two-factor authentication on your own account first" in r.json()["detail"].lower()
        assert stored_policy(h, "t1") == "optional"

    def test_a_setup_that_was_never_verified_is_not_2fa(self, h):
        def _w(s):
            a = s.query(TenantAdmin).filter(TenantAdmin.username == "admin_t1").one()
            a.totp_secret = seal_totp_secret(pyotp.random_base32())
            a.totp_enabled = False
        h.seed(_w)
        r = h.client.patch(CURRENT, json={"mfa_policy": "required_admins"}, headers=h.headers("t1"))
        assert r.status_code == 409, r.text
        assert stored_policy(h, "t1") == "optional"

    def test_the_refusal_writes_nothing_else_of_the_request(self, h):
        r = h.client.patch(
            CURRENT, json={"display_name": "Smuggled", "mfa_policy": "required_all"}, headers=h.headers("t1")
        )
        assert r.status_code == 409, r.text
        s = h.Factory()
        try:
            assert s.get(Tenant, "t1").display_name != "Smuggled"
        finally:
            s.close()

    def test_optional_needs_no_2fa(self, h):
        r = h.client.patch(CURRENT, json={"mfa_policy": "optional"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text

    def test_relaxing_needs_no_2fa(self, h):
        """Reachable: a system administrator required it by id, then this
        administrator's session is one issued before that."""
        token = session_token(h)
        set_policy(h, "t1", "required_all")
        r = h.client.patch(CURRENT, json={"mfa_policy": "optional"}, headers=bearer(token))
        assert r.status_code == 200, r.text
        assert stored_policy(h, "t1") == "optional"


class TestEnrolmentAtSignIn:
    @pytest.fixture()
    def challenge(self, h):
        set_policy(h, "t1", "required_admins")
        return enrolment_challenge(h)

    def test_setup_answers_what_the_signed_in_setup_answers(self, h, challenge):
        r = h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"secret", "provisioning_uri", "qr_code"}
        assert body["provisioning_uri"].startswith("otpauth://totp/")
        row = admin_row(h, "admin_t1")
        # Pending: stored sealed, not enabled, and still no session.
        assert row.totp_secret and body["secret"] not in row.totp_secret
        assert not row.totp_enabled
        assert active_sessions(h) == 0
        # The other tenant's admin, signed in, gets the same shape.
        r2 = h.client.post("/v1/admin/users/me/2fa/setup", headers=h.headers("t2"))
        assert set(r2.json()) == set(body)

    def test_setup_alone_does_not_satisfy_the_policy(self, h, challenge):
        h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]})
        again = password_login(h).json()
        assert again.get("requires_2fa_enrollment") is True and "access_token" not in again

    def test_verify_enables_2fa_and_signs_in(self, h, challenge):
        secret = h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]}).json()["secret"]
        r = h.client.post(
            ENROLL_VERIFY, json={"temp_token": challenge["temp_token"], "code": pyotp.TOTP(secret).now()}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # The full response of a sign-in, plus the backup codes, once.
        assert set(body) == SESSION_KEYS | {"backup_codes"}
        assert len(body["backup_codes"]) == 10 and len(set(body["backup_codes"])) == 10
        assert body["token_type"] == "bearer" and body["admin_type"] == "tenant"
        assert body["tenant_id"] == "t1" and body["tenant_display_name"] == "T1"
        assert body["admin"]["username"] == "admin_t1" and body["admin"]["totp_enabled"] is True

        row = admin_row(h, "admin_t1")
        assert row.totp_enabled and row.totp_secret
        assert len(row.backup_codes) == 10 and not set(row.backup_codes) & set(body["backup_codes"])
        assert row.failed_login_attempts == 0 and row.last_login is not None
        assert active_sessions(h) == 1

        # A real session: the token is an admin token and the routes take it.
        claims = jwt.decode(body["access_token"], JWT_SECRET, algorithms=["HS256"])
        assert claims["typ"] == "admin" and claims["tenant_id"] == "t1" and claims["jti"]
        me = h.client.get(ME, headers=bearer(body["access_token"]))
        assert me.status_code == 200 and me.json()["username"] == "admin_t1"
        assert h.client.get(CURRENT, headers=bearer(body["access_token"])).json()["mfa_policy"] == "required_admins"

        # From now on: password, then a code.
        nxt = password_login(h).json()
        assert nxt.get("requires_2fa") is True and "requires_2fa_enrollment" not in nxt
        r = h.client.post(LOGIN_2FA, json={"temp_token": nxt["temp_token"], "code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200, r.text
        # A backup code works as the second factor.
        nxt = password_login(h).json()
        r = h.client.post(LOGIN_2FA, json={"temp_token": nxt["temp_token"], "code": body["backup_codes"][0]})
        assert r.status_code == 200, r.text

    def test_the_session_is_the_same_as_a_sign_in_without_the_policy(self, h, challenge):
        normal = password_login(h, "admin_t2", "t2").json()
        secret = h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]}).json()["secret"]
        enrolled = h.client.post(
            ENROLL_VERIFY, json={"temp_token": challenge["temp_token"], "code": pyotp.TOTP(secret).now()}
        ).json()
        assert set(enrolled) - {"backup_codes"} == set(normal)
        a = jwt.decode(normal["access_token"], JWT_SECRET, algorithms=["HS256"])
        b = jwt.decode(enrolled["access_token"], JWT_SECRET, algorithms=["HS256"])
        assert set(a) == set(b)
        assert a["permissions"] and sorted(a["permissions"]) == sorted(b["permissions"])
        assert b["exp"] - b["iat"] == a["exp"] - a["iat"] == enrolled["expires_in"]

    def test_verify_before_setup_is_refused(self, h, challenge):
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": challenge["temp_token"], "code": "123456"})
        assert r.status_code == 400, r.text
        assert active_sessions(h) == 0 and not admin_row(h, "admin_t1").totp_enabled

    def test_a_wrong_code_enables_nothing_and_issues_nothing(self, h, challenge):
        h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]})
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": challenge["temp_token"], "code": "000000"})
        assert r.status_code == 401, r.text
        assert "access_token" not in r.text and "backup_codes" not in r.text
        row = admin_row(h, "admin_t1")
        assert not row.totp_enabled and row.backup_codes is None
        assert row.failed_login_attempts == 1
        assert active_sessions(h) == 0

    def test_the_token_cannot_be_replayed_after_it_was_used(self, h, challenge):
        token = challenge["temp_token"]
        secret = h.client.post(ENROLL_SETUP, json={"temp_token": token}).json()["secret"]
        code = pyotp.TOTP(secret).now()
        assert h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": code}).status_code == 200
        sealed = admin_row(h, "admin_t1").totp_secret
        # Within its ten minutes, the same token: no second session, no new secret.
        assert h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": code}).status_code == 409
        assert h.client.post(ENROLL_SETUP, json={"temp_token": token}).status_code == 409
        assert active_sessions(h) == 1
        assert admin_row(h, "admin_t1").totp_secret == sealed

    def test_an_admin_who_already_has_2fa_is_refused(self, h, challenge):
        """The token is for enrolling a first factor. Used on an account that
        has one, `setup` would replace its secret without the old code."""
        give_2fa(h, "admin_t1")
        sealed = admin_row(h, "admin_t1").totp_secret
        r = h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]})
        assert r.status_code == 409, r.text
        # Refused as a spent sign-in token, before the setup logic is reached.
        assert "sign in again" in r.json()["detail"].lower()
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": challenge["temp_token"], "code": "123456"})
        assert r.status_code == 409, r.text
        assert "sign in again" in r.json()["detail"].lower()
        assert admin_row(h, "admin_t1").totp_secret == sealed
        assert active_sessions(h) == 0

    def test_wrong_codes_lock_the_account_like_the_2fa_login(self, h, challenge):
        token = challenge["temp_token"]
        secret = h.client.post(ENROLL_SETUP, json={"temp_token": token}).json()["secret"]
        for attempt in range(1, 5):
            r = h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": "000000"})
            assert r.status_code == 401, f"attempt {attempt}: {r.status_code}"
            # Signing in again with the password hands out a new token, not a clean counter.
            if attempt < 4:
                token = enrolment_challenge(h)["temp_token"]
            assert admin_row(h, "admin_t1").failed_login_attempts == attempt
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": "000000"})
        assert r.status_code == 423, r.text
        row = admin_row(h, "admin_t1")
        assert row.is_locked and row.failed_login_attempts == 5 and not row.totp_enabled
        # Locked: the right code is refused, a new secret is refused, the password is refused.
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": pyotp.TOTP(secret).now()})
        assert r.status_code == 423, r.text
        assert h.client.post(ENROLL_SETUP, json={"temp_token": token}).status_code == 423
        assert password_login(h).status_code == 401
        assert active_sessions(h) == 0 and not admin_row(h, "admin_t1").totp_enabled

    def test_a_correct_code_clears_earlier_failures(self, h, challenge):
        token = challenge["temp_token"]
        secret = h.client.post(ENROLL_SETUP, json={"temp_token": token}).json()["secret"]
        for _ in range(2):
            h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": "000000"})
        assert admin_row(h, "admin_t1").failed_login_attempts == 2
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200, r.text
        assert admin_row(h, "admin_t1").failed_login_attempts == 0

    @pytest.mark.parametrize("state", [{"is_locked": True}, {"is_active": False}])
    def test_a_locked_or_deactivated_admin_cannot_enrol(self, h, challenge, state):
        def _w(s):
            s.query(TenantAdmin).filter(TenantAdmin.username == "admin_t1").update(state)
        h.seed(_w)
        r = h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]})
        assert r.status_code == 423, r.text
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": challenge["temp_token"], "code": "123456"})
        assert r.status_code == 423, r.text
        assert admin_row(h, "admin_t1").totp_secret is None and active_sessions(h) == 0

    def test_a_backup_code_shaped_input_is_not_a_way_round(self, h, challenge):
        h.client.post(ENROLL_SETUP, json={"temp_token": challenge["temp_token"]})
        r = h.client.post(ENROLL_VERIFY, json={"temp_token": challenge["temp_token"], "code": "ABCD-EFGH"})
        assert r.status_code in (401, 422), r.text
        assert not admin_row(h, "admin_t1").totp_enabled and active_sessions(h) == 0

    def test_enrolment_is_audited_without_secrets(self, h, challenge):
        token = challenge["temp_token"]
        setup = h.client.post(ENROLL_SETUP, json={"temp_token": token}).json()
        code = pyotp.TOTP(setup["secret"]).now()
        body = h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": code}).json()
        s = h.Factory()
        try:
            rows = s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.tenant_id == "t1").all()
            actions = [r.action for r in rows]
            blob = json.dumps([[r.action, r.details] for r in rows], default=str)
        finally:
            s.close()
        assert "tenant_admin_login_2fa_enrollment_required" in actions
        assert "mfa_2fa_setup" in actions and "mfa_2fa_enable" in actions
        assert "admin_login_2fa_enrollment" in actions
        for secret_material in (setup["secret"], code, token, body["access_token"], *body["backup_codes"]):
            assert secret_material not in blob


class TestThePolicyIsSavedAndReadBack:
    def test_a_new_tenant_reads_optional(self, h):
        r = h.client.get(CURRENT, headers=h.headers("t1"))
        assert r.status_code == 200 and r.json()["mfa_policy"] == "optional"

    @pytest.mark.parametrize("value", REQUIRING)
    def test_a_tenant_admin_saves_it_and_reads_it_back(self, h, value):
        secret = give_2fa(h, "admin_t1")
        token = session_token_2fa(h, secret)
        r = h.client.patch(CURRENT, json={"mfa_policy": value}, headers=bearer(token))
        assert r.status_code == 200, r.text
        assert r.json()["mfa_policy"] == value
        assert stored_policy(h, "t1") == value
        assert stored_policy(h, "t2") == "optional"
        r = h.client.get(CURRENT, headers=bearer(token))
        assert r.json()["mfa_policy"] == value
        # And back.
        r = h.client.patch(CURRENT, json={"mfa_policy": "optional"}, headers=bearer(token))
        assert r.status_code == 200 and r.json()["mfa_policy"] == "optional"
        assert stored_policy(h, "t1") == "optional"

    def test_the_change_is_in_the_audit_log(self, h):
        secret = give_2fa(h, "admin_t1")
        token = session_token_2fa(h, secret)
        r = h.client.patch(CURRENT, json={"mfa_policy": "required_admins"}, headers=bearer(token))
        assert r.status_code == 200, r.text
        s = h.Factory()
        try:
            rows = s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.action == "tenant_update").all()
            details = [r.details if isinstance(r.details, dict) else json.loads(r.details) for r in rows]
        finally:
            s.close()
        changes = [d["changes"]["mfa_policy"] for d in details if "mfa_policy" in d.get("changes", {})]
        assert changes == [{"old": "optional", "new": "required_admins"}]

    # The Community half: the installation's own route. The Enterprise half
    # holds the same test with tenant management's by-id route beside it.
    @pytest.mark.parametrize("value", ["required", "REQUIRED_ALL", "", "off", 1, True, ["required_all"]])
    def test_a_value_outside_the_three_is_refused(self, h, value):
        r = h.client.patch(CURRENT, json={"mfa_policy": value}, headers=h.headers("t1"))
        assert r.status_code == 422, r.text
        assert stored_policy(h, "t1") == "optional"


class TestSignInWhenThePolicyRequires2FA:
    @pytest.mark.parametrize("stored", [*REQUIRING, "bogus"])
    @pytest.mark.parametrize("with_tenant_id", [True, False])
    def test_no_session_an_enrolment_challenge_instead(self, h, stored, with_tenant_id):
        """`with_tenant_id=False` is the form without a tenant id: the same
        route and service method, the tenant resolved from the username."""
        set_policy(h, "t1", stored)
        r = password_login(h, "admin_t1", "t1" if with_tenant_id else None)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == CHALLENGE_KEYS
        assert body["requires_2fa_enrollment"] is True
        assert body["admin_type"] == "tenant"
        assert body["tenant_id"] == "t1"
        assert body["tenant_display_name"] == "T1"
        assert body["admin"] == {"username": "admin_t1", "email": "admin@t1.example"}
        assert active_sessions(h) == 0
        claims = jwt.decode(body["temp_token"], JWT_SECRET, algorithms=["HS256"])
        assert claims["type"] == "2fa_enrollment"
        assert "typ" not in claims and "jti" not in claims and "permissions" not in claims
        assert claims["admin_id"] == admin_row(h, "admin_t1").id
        assert claims["tenant_id"] == "t1"
        assert claims["exp"] - claims["iat"] == 600
        # A sign-in that did not happen is not recorded as one.
        assert admin_row(h, "admin_t1").last_login is None

    def test_optional_signs_in_as_before(self, h):
        r = password_login(h)
        assert r.status_code == 200 and set(r.json()) == SESSION_KEYS
        assert active_sessions(h) == 1

    def test_another_tenants_policy_does_not_apply(self, h):
        set_policy(h, "t2", "required_all")
        r = password_login(h, "admin_t1", "t1")
        assert set(r.json()) == SESSION_KEYS

    @pytest.mark.parametrize("stored", REQUIRING)
    def test_an_admin_with_2fa_gets_the_usual_code_challenge(self, h, stored):
        secret = give_2fa(h, "admin_t1")
        set_policy(h, "t1", stored)
        r = password_login(h)
        body = r.json()
        assert body.get("requires_2fa") is True and "requires_2fa_enrollment" not in body
        assert jwt.decode(body["temp_token"], JWT_SECRET, algorithms=["HS256"])["type"] == "2fa_challenge"
        r = h.client.post(LOGIN_2FA, json={"temp_token": body["temp_token"], "code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200 and r.json()["token"]

    def test_a_wrong_password_is_still_just_refused(self, h):
        set_policy(h, "t1", "required_all")
        r = h.client.post(LOGIN, json={"username": "admin_t1", "password": "Wrong-password-1!", "tenant_id": "t1"})
        assert r.status_code == 401
        assert "temp_token" not in r.text

    def test_the_password_step_does_not_clear_the_failure_counter(self, h):
        """Wrong enrolment codes count on `failed_login_attempts`; if the
        password step cleared it, signing in again would undo the lock-out."""
        set_policy(h, "t1", "required_admins")

        def _w(s):
            s.query(TenantAdmin).filter(TenantAdmin.username == "admin_t1").update({"failed_login_attempts": 3})
        h.seed(_w)
        enrolment_challenge(h)
        assert admin_row(h, "admin_t1").failed_login_attempts == 3


class TestTheEnrolmentTokenIsNotASession:
    @pytest.fixture()
    def token(self, h):
        set_policy(h, "t1", "required_admins")
        return enrolment_challenge(h)["temp_token"]

    def test_verify_token_refuses_it(self, h, token):
        s = h.Factory()
        try:
            assert UnifiedAuthService(jwt_secret=JWT_SECRET).verify_token(token, s) is None
        finally:
            s.close()

    def test_the_2fa_login_refuses_it(self, h, token):
        r = h.client.post(LOGIN_2FA, json={"temp_token": token, "code": "123456"})
        assert r.status_code == 401, r.text
        assert active_sessions(h) == 0

    def test_logout_with_it_ends_nothing(self, h, token):
        other = session_token(h, "admin_t2", "t2")
        h.client.post("/v1/admin/users/logout", headers=bearer(token))
        assert h.client.get(ME, headers=bearer(other)).status_code == 200

    # The Community routes; the Enterprise half adds tenant management's.
    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("get", ME, None),
            ("get", CURRENT, None),
            ("patch", CURRENT, {"mfa_policy": "optional"}),
            ("get", "/v1/admin/tenants/current/theme", None),
            ("get", "/v1/admin/users/me/2fa/status", None),
            ("post", "/v1/admin/users/me/2fa/setup", None),
            ("post", "/v1/admin/users/me/2fa/verify", {"code": "123456"}),
            ("post", DISABLE, {"code": "123456"}),
            ("post", "/v1/admin/users/me/change-password", {"current_password": PASSWORD, "new_password": "Other123!"}),
            ("get", "/v1/admin/users/tenant/sessions", None),
            ("post", "/v1/admin/users/logout", None),
        ],
    )
    def test_no_authenticated_route_takes_it(self, h, token, method, path, body):
        kw = {"json": body} if body is not None else {}
        r = getattr(h.client, method)(path, headers=bearer(token), **kw)
        assert r.status_code == 401, f"{method} {path}: {r.status_code} {r.text}"
        assert stored_policy(h, "t1") == "required_admins"
        assert admin_row(h, "admin_t1").totp_secret is None


class TestTheEnrolEndpointsTakeOnlyAnEnrolmentToken:
    @pytest.fixture()
    def admin_id(self, h):
        set_policy(h, "t1", "required_admins")
        return admin_row(h, "admin_t1").id

    def _both(self, h, token):
        return (
            h.client.post(ENROLL_SETUP, json={"temp_token": token}),
            h.client.post(ENROLL_VERIFY, json={"temp_token": token, "code": "123456"}),
        )

    def _untouched(self, h):
        row = admin_row(h, "admin_t1")
        assert row.totp_secret is None and not row.totp_enabled
        assert row.failed_login_attempts == 0
        assert active_sessions(h) == 0

    def test_a_2fa_challenge_token_is_refused(self, h, admin_id):
        for r in self._both(h, forged("2fa_challenge", admin_id, minutes=5)):
            assert r.status_code == 401, r.text
        self._untouched(h)

    def test_the_real_2fa_challenge_of_an_enrolled_admin_is_refused(self, h, admin_id):
        add_admin(h, "t1", "second")
        give_2fa(h, "second")
        challenge = password_login(h, "second").json()
        assert challenge["requires_2fa"] is True
        for r in self._both(h, challenge["temp_token"]):
            assert r.status_code == 401, r.text

    def test_a_session_token_is_refused(self, h, admin_id):
        set_policy(h, "t1", "optional")
        session = session_token(h)
        for r in self._both(h, session):
            assert r.status_code == 401, r.text
        assert admin_row(h, "admin_t1").totp_secret is None

    def test_a_token_with_no_type_is_refused(self, h, admin_id):
        now = datetime.now(timezone.utc)
        token = jwt.encode({"admin_id": admin_id, "tenant_id": "t1", "admin_type": "tenant",
                            "iat": now, "exp": now + timedelta(minutes=5)}, JWT_SECRET, algorithm="HS256")
        for r in self._both(h, token):
            assert r.status_code == 401, r.text
        self._untouched(h)

    def test_an_expired_token_is_refused(self, h, admin_id):
        for r in self._both(h, forged("2fa_enrollment", admin_id, minutes=-1)):
            assert r.status_code == 401, r.text
        self._untouched(h)

    @pytest.mark.parametrize("token", ["garbage", "a.b.c", "", "eyJhbGciOiJub25lIn0.e30."])
    def test_garbage_is_refused(self, h, admin_id, token):
        for r in self._both(h, token):
            assert r.status_code == 401, r.text
        self._untouched(h)

    def test_a_token_signed_with_another_key_is_refused(self, h, admin_id):
        for r in self._both(h, forged("2fa_enrollment", admin_id, secret="not-the-secret")):
            assert r.status_code == 401, r.text
        self._untouched(h)

    def test_an_unsigned_token_is_refused(self, h, admin_id):
        now = datetime.now(timezone.utc)
        token = jwt.encode({"type": "2fa_enrollment", "admin_id": admin_id, "tenant_id": "t1",
                            "admin_type": "tenant", "exp": now + timedelta(minutes=5)}, None, algorithm="none")
        for r in self._both(h, token):
            assert r.status_code == 401, r.text
        self._untouched(h)

    def test_a_token_naming_the_admin_in_another_tenant_is_refused(self, h, admin_id):
        for r in self._both(h, forged("2fa_enrollment", admin_id, tenant_id="t2")):
            assert r.status_code == 401, r.text
        self._untouched(h)

    def test_a_token_naming_nobody_is_refused(self, h, admin_id):
        for r in self._both(h, forged("2fa_enrollment", "no-such-admin")):
            assert r.status_code == 401, r.text

    def test_a_missing_token_is_a_validation_error_not_a_pass(self, h, admin_id):
        assert h.client.post(ENROLL_SETUP, json={}).status_code == 422
        assert h.client.post(ENROLL_VERIFY, json={"code": "123456"}).status_code == 422
        self._untouched(h)


class TestDisablingWhileThePolicyRequires2FA:
    @pytest.mark.parametrize("stored", [*REQUIRING, "bogus"])
    def test_a_tenant_admin_cannot_disable(self, h, stored):
        secret = give_2fa(h, "admin_t1")
        token = session_token_2fa(h, secret)
        set_policy(h, "t1", stored)
        r = h.client.post(DISABLE, json={"code": pyotp.TOTP(secret).now()}, headers=bearer(token))
        assert r.status_code == 409, r.text
        assert "required" in r.json()["detail"].lower()
        row = admin_row(h, "admin_t1")
        assert row.totp_enabled and row.totp_secret

    def test_optional_still_lets_them(self, h):
        secret = give_2fa(h, "admin_t1")
        token = session_token_2fa(h, secret)
        r = h.client.post(DISABLE, json={"code": pyotp.TOTP(secret).now()}, headers=bearer(token))
        assert r.status_code == 200, r.text
        assert not admin_row(h, "admin_t1").totp_enabled

    def test_another_tenants_policy_does_not_apply(self, h):
        secret = give_2fa(h, "admin_t1")
        token = session_token_2fa(h, secret)
        set_policy(h, "t2", "required_all")
        r = h.client.post(DISABLE, json={"code": pyotp.TOTP(secret).now()}, headers=bearer(token))
        assert r.status_code == 200, r.text


class TestTighteningEndsSessions:
    @pytest.fixture()
    def scene(self, h):
        """t1: `admin_t1` (2FA, signed in), `plain` (no 2FA, signed in twice),
        two end users (one with 2FA). t2: an admin signed in, an end user."""
        add_admin(h, "t1", "plain")
        plain = [session_token(h, "plain"), session_token(h, "plain")]
        other = session_token(h, "admin_t2", "t2")
        secret = give_2fa(h, "admin_t1")
        owner = session_token_2fa(h, secret)
        return SimpleNamespace(
            owner=owner, plain=plain, other=other,
            user=add_end_user(h, "t1", "u-plain"),
            user_2fa=add_end_user(h, "t1", "u-2fa", totp=True),
            user_t2=add_end_user(h, "t2", "u-other"),
        )

    def _alive(self, h, token) -> bool:
        return h.client.get(ME, headers=bearer(token)).status_code == 200

    def _reasons(self, h, username):
        s = h.Factory()
        try:
            a = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
            rows = s.query(TenantAdminSession).filter(TenantAdminSession.admin_id == a.id).all()
            return sorted((r.is_active, r.deactivation_reason) for r in rows)
        finally:
            s.close()

    def test_required_admins_ends_the_sessions_of_admins_without_2fa(self, h, scene):
        assert all(self._alive(h, t) for t in (*scene.plain, scene.owner, scene.other))
        r = h.client.patch(CURRENT, json={"mfa_policy": "required_admins"}, headers=bearer(scene.owner))
        assert r.status_code == 200, r.text
        assert not any(self._alive(h, t) for t in scene.plain)
        assert self._reasons(h, "plain") == [(False, "mfa_policy_tightened")] * 2
        # Whoever has 2FA stays signed in; another tenant is not touched.
        assert self._alive(h, scene.owner) and self._alive(h, scene.other)
        # End users are not covered by `required_admins`.
        assert end_user_cutoff(h, scene.user) is None
        assert end_user_cutoff(h, scene.user_2fa) is None
        assert end_user_cutoff(h, scene.user_t2) is None
        # And the next sign-in of the admin without 2FA is an enrolment.
        assert password_login(h, "plain").json().get("requires_2fa_enrollment") is True

    def test_required_all_also_ends_the_portal_sessions_of_users_without_2fa(self, h, scene):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        r = h.client.patch(CURRENT, json={"mfa_policy": "required_all"}, headers=bearer(scene.owner))
        assert r.status_code == 200, r.text
        assert not any(self._alive(h, t) for t in scene.plain)
        assert self._alive(h, scene.owner) and self._alive(h, scene.other)
        cutoff = end_user_cutoff(h, scene.user)
        assert cutoff is not None
        if cutoff.tzinfo is None:  # SQLite drops the zone; the value is UTC
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        assert before <= cutoff <= datetime.now(timezone.utc)
        assert end_user_cutoff(h, scene.user_2fa) is None
        assert end_user_cutoff(h, scene.user_t2) is None

    def test_from_required_admins_to_required_all_ends_the_portal_sessions(self, h, scene):
        h.client.patch(CURRENT, json={"mfa_policy": "required_admins"}, headers=bearer(scene.owner))
        assert end_user_cutoff(h, scene.user) is None
        r = h.client.patch(CURRENT, json={"mfa_policy": "required_all"}, headers=bearer(scene.owner))
        assert r.status_code == 200, r.text
        assert end_user_cutoff(h, scene.user) is not None
        assert end_user_cutoff(h, scene.user_2fa) is None

    def test_an_update_that_does_not_name_the_policy_ends_nothing(self, h, scene):
        r = h.client.patch(CURRENT, json={"display_name": "Renamed"}, headers=bearer(scene.owner))
        assert r.status_code == 200, r.text
        assert all(self._alive(h, t) for t in (*scene.plain, scene.owner))

    def test_the_audit_record_says_how_many_were_ended(self, h, scene):
        h.client.patch(CURRENT, json={"mfa_policy": "required_all"}, headers=bearer(scene.owner))
        s = h.Factory()
        try:
            rows = s.query(TenantAdminAuditLog).filter(TenantAdminAuditLog.action == "tenant_update").all()
            details = [r.details if isinstance(r.details, dict) else json.loads(r.details) for r in rows]
        finally:
            s.close()
        ended = [d["mfa_sessions_ended"] for d in details if "mfa_sessions_ended" in d]
        assert ended == [{"admin_sessions": 2, "end_users": 1}]

    # The Community half; the Enterprise half adds the by-id refusal.
    def test_a_refused_change_ends_nothing(self, h, scene):
        r = h.client.patch(CURRENT, json={"mfa_policy": "required_all"}, headers=bearer(scene.plain[0]))
        assert r.status_code == 409, r.text
        assert all(self._alive(h, t) for t in (*scene.plain, scene.owner))
        assert end_user_cutoff(h, scene.user) is None

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The second step of a sign-in belongs to the principal that began it.

`POST /admin/users/login/2fa` read the challenge token's `admin_type`, took the
system administrator's branch for `system` and the tenant administrator's for
everything else, a missing claim included. It now asks the principal registry:
the registered principal the challenge names looks up its own administrator
and issues its own session (`PrincipalSpec.find_admin`, `complete_sign_in`),
and a challenge for a type that is not registered is refused with 401. It is
never completed as some other principal's.

The same for the routes of the signed-in administrator (`/me/2fa/*`,
`/me/change-password`): the row is the calling principal's own.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pyotp
import pytest
from fastapi import HTTPException

from app.composition.principals import HUMAN_SESSION, PRINCIPALS, PrincipalSpec
from app.database.models.admin import SystemAdmin, SystemAdminSession, TenantAdmin, TenantAdminSession
from tests.principal_matrix import community_principals
from app.composition.principals import PRINCIPALS
from tests.test_admin_self_service import JWT_SECRET, _enrol_2fa, harness  # noqa: F401

APP = Path(__file__).resolve().parent.parent / "app"


def _reissue(token, **claims):
    """The token's claims, changed, signed with the real secret. `None` drops a claim."""
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    for name, value in claims.items():
        if value is None:
            payload.pop(name, None)
        else:
            payload[name] = value
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _challenge(harness, kind):  # noqa: F811
    if kind == "system":
        r = harness.client.post("/v1/admin/users/system/login", json={"username": "sysadmin", "password": "Secret123!"})
    else:
        r = harness.client.post(
            "/v1/admin/users/tenant/login", json={"username": "tadmin", "password": "Secret123!", "tenant_id": "t1"}
        )
    assert r.status_code == 200 and r.json().get("requires_2fa") is True, r.text
    return r.json()["temp_token"]


def _state(harness):  # noqa: F811
    """What a refused step must leave alone."""
    s = harness.new_session()
    try:
        return (
            s.query(SystemAdminSession).count(),
            s.query(TenantAdminSession).count(),
            [(a.failed_login_attempts, a.is_locked, a.last_login) for a in s.query(SystemAdmin).all()],
            [(a.failed_login_attempts, a.is_locked, a.last_login) for a in s.query(TenantAdmin).all()],
        )
    finally:
        s.close()


def _ids(harness):  # noqa: F811
    s = harness.new_session()
    try:
        return s.query(SystemAdmin).one().id, s.query(TenantAdmin).one().id
    finally:
        s.close()


def _complete(harness, token, secret):  # noqa: F811
    return harness.client.post(
        "/v1/admin/users/login/2fa", json={"temp_token": token, "code": pyotp.TOTP(secret).now()}
    )


class registered:
    def __init__(self, *specs):
        self.specs = specs

    def __enter__(self):
        self.saved = dict(PRINCIPALS._specs)
        PRINCIPALS.register(self.specs)

    def __exit__(self, *exc):
        PRINCIPALS._specs = self.saved
        return False


def _robot(**kw):
    def refuse(*_a, **_k):
        raise HTTPException(status_code=403)

    return PrincipalSpec(
        name="robot", credential_class=HUMAN_SESSION, verify=lambda *_a: None,
        resolve_tenant=refuse, find_session=lambda *_a: None, **kw,
    )


# =============================================================================
# The composition that runs today
# =============================================================================

class TestTheChallengeNamesItsPrincipal:
    @pytest.mark.parametrize("kind", sorted(PRINCIPALS.names()))  # every registered principal
    def test_each_principal_completes_its_own(self, harness, kind):  # noqa: F811
        secret = _enrol_2fa(harness, kind)
        before = _state(harness)
        r = _complete(harness, _challenge(harness, kind), secret)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {
            "token", "admin", "tenant_id", "tenant_display_name", "is_trial", "trial_ends_at", "suspended",
        }
        claims = jwt.decode(body["token"], JWT_SECRET, algorithms=["HS256"])
        assert claims["typ"] == "admin" and claims["admin_type"] == kind
        after = _state(harness)
        if kind == "system":
            assert body["tenant_id"] is None and body["tenant_display_name"] is None
            assert "tenant_id" not in claims
            assert (after[0], after[1]) == (before[0] + 1, before[1])
        else:
            assert body["tenant_id"] == "t1" and body["tenant_display_name"] == "Tenant One"
            assert claims["tenant_id"] == "t1"
            assert (after[0], after[1]) == (before[0], before[1] + 1)
        # The session is real: the token is accepted.
        me = harness.client.get("/v1/admin/users/me/2fa/status", headers=harness.bearer(body["token"]))
        assert me.status_code == 200 and me.json()["enabled"] is True

    @pytest.mark.parametrize("kind", sorted(PRINCIPALS.names()))  # every registered principal
    @pytest.mark.parametrize("admin_type", ["robot", None, "System", "TENANT", " tenant", "tenant ", 7, "tenant_admin"])
    def test_a_type_nobody_registered_is_refused(self, harness, kind, admin_type):  # noqa: F811
        """With the right code, of a real administrator of either kind: the
        challenge is no registered principal's, so nobody completes it."""
        secret = _enrol_2fa(harness, kind)
        token = _reissue(_challenge(harness, kind), admin_type=admin_type)
        before = _state(harness)
        r = _complete(harness, token, secret)
        assert r.status_code == 401, r.text
        assert "token" not in r.json()
        assert _state(harness) == before


    def test_a_registered_principal_that_completes_no_sign_in_is_refused(self, harness):  # noqa: F811
        """Registered, but with no administrator rows and no sign-in of its
        own: it is not handed the tenant administrator's."""
        secret = _enrol_2fa(harness, "tenant")
        token = _reissue(_challenge(harness, "tenant"), admin_type="robot")
        before = _state(harness)
        with registered(_robot()):
            assert _complete(harness, token, secret).status_code == 401
        with registered(_robot(find_admin=PRINCIPALS.get("tenant").find_admin)):
            # It finds a row (borrowed here), and still has no sign-in to complete.
            assert _complete(harness, token, secret).status_code == 401
        assert _state(harness) == before


# =============================================================================
# The Community composition
# =============================================================================

class TestTheCommunityComposition:

    def test_a_tenant_administrator_completes_its_sign_in(self, harness):  # noqa: F811
        secret = _enrol_2fa(harness, "tenant")
        token = _challenge(harness, "tenant")
        with community_principals():
            r = _complete(harness, token, secret)
            assert r.status_code == 200, r.text
            assert r.json()["tenant_id"] == "t1"
            status = harness.client.get("/v1/admin/users/me/2fa/status", headers=harness.bearer(r.json()["token"]))
            assert status.status_code == 200


# =============================================================================
# Community modules name nothing of the system administrator
# =============================================================================

SYSTEM_NAMES = {
    "SystemAdmin", "SystemAdminSession", "SystemAdminAuditLog", "system_principal",
    "SYSTEM_ADMIN_TYPE",
    "login_system_admin", "create_system_admin",
}

COMMUNITY_MODULES = (
    "api/v1/endpoints/admin/mfa.py",
    "core/admin_user/services/unified_auth_service.py",
    "core/admin_user/services/audit_logger.py",
    "core/admin_user/tenant_principal.py",
)


def _names(source: str):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.update(node.name.split("."))
            names.add(node.asname or "")
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Constant) and node.value == "system":
            names.add('"system"')
    return names


@pytest.mark.parametrize("module", COMMUNITY_MODULES)
def test_the_module_names_nothing_of_the_system_administrator(module):
    found = _names((APP / module).read_text()) & (SYSTEM_NAMES | {'"system"'})
    assert not found, sorted(found)



# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
One password rule, on every path that sets a password.

The rule is stated once (`app/core/password_rule.py`). An administrator's
password is set when the account is created, when another administrator
updates it, when its owner changes it, and when the installation creates
its first accounts from the configured passwords; a Portal user's when an
administrator creates the user. Each of those refuses a password the rule
refuses, and the stored password is the one before.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.password_rule import MIN_PASSWORD_LENGTH, PASSWORD_RULE, PasswordRefused, check_password
from app.database.models.admin import TenantAdmin
from tests.admin_endpoint_harness import PASSWORD, build_admin_harness

SHORT = "Ab1!" + "x" * (MIN_PASSWORD_LENGTH - 5)
GOOD = "Ab1!" + "x" * (MIN_PASSWORD_LENGTH - 4)


def test_the_rule():
    assert len(SHORT) == MIN_PASSWORD_LENGTH - 1
    assert check_password(GOOD) == GOOD
    for refused in (SHORT, "", " " * (MIN_PASSWORD_LENGTH + 2), None, 12345678):
        with pytest.raises(PasswordRefused) as raised:
            check_password(refused)
        assert PASSWORD_RULE in str(raised.value)
    assert str(MIN_PASSWORD_LENGTH) in PASSWORD_RULE


# The stock password of earlier releases, assembled here only: no shipped
# file holds it (`tests/test_initial_passwords.py` scans for it).
FORMER_STOCK = "Admin" + "123!"


def test_the_former_stock_password_is_refused():
    with pytest.raises(PasswordRefused, match="publicly known"):
        check_password(FORMER_STOCK, "BRUTOR_SYSTEM_ADMIN_PASSWORD")


@pytest.fixture(scope="module")
def h():
    from app.api.v1.endpoints.admin.mfa import create_me_router

    harness = build_admin_harness([create_me_router()])
    yield harness
    harness.close()


def _admin(h, tenant: str = "t1") -> str:
    name = f"u{uuid.uuid4().hex[:10]}"
    r = h.client.post(
        "/v1/admin/users/tenant",
        json={"tenant_id": tenant, "username": name, "email": f"{name}@x.example", "password": PASSWORD},
        headers=h.headers(tenant),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _hash(h, admin_id: str) -> str:
    s = h.Factory()
    try:
        return s.get(TenantAdmin, admin_id).password_hash
    finally:
        s.close()


def test_creating_an_administrator(h):
    name = f"u{uuid.uuid4().hex[:10]}"
    r = h.client.post(
        "/v1/admin/users/tenant",
        json={"tenant_id": "t1", "username": name, "email": f"{name}@x.example", "password": SHORT},
        headers=h.headers(),
    )
    assert r.status_code == 422, r.text
    assert PASSWORD_RULE in r.text


def test_updating_an_administrator(h):
    admin_id = _admin(h)
    before = _hash(h, admin_id)

    r = h.client.patch(f"/v1/admin/users/tenant/{admin_id}", json={"password": SHORT}, headers=h.headers())

    assert r.status_code == 422, r.text
    assert PASSWORD_RULE in r.text
    assert _hash(h, admin_id) == before

    r = h.client.patch(f"/v1/admin/users/tenant/{admin_id}", json={"password": GOOD}, headers=h.headers())
    assert r.status_code == 200, r.text
    assert _hash(h, admin_id) != before


def test_changing_ones_own(h):
    r = h.client.post(
        "/v1/admin/users/me/change-password",
        json={"current_password": PASSWORD, "new_password": SHORT},
        headers=h.headers("t2"),
    )
    assert r.status_code == 422, r.text
    assert PASSWORD_RULE in r.text


def test_the_configured_password_of_a_first_account(monkeypatch):
    from app.core.initial_credentials import initial_password
    from app.env_vars import BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD

    monkeypatch.setenv(BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD.name, SHORT)
    with pytest.raises(PasswordRefused) as raised:
        initial_password(BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD, "the first administrator")
    assert BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD.name in str(raised.value)
    assert SHORT not in str(raised.value)

    monkeypatch.setenv(BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD.name, GOOD)
    assert initial_password(BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD, "the first administrator") == GOOD


def test_no_stored_password_is_made_from_a_refused_one():
    """Whatever route or seed step asks: the places a password becomes a
    stored hash refuse too."""
    from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
    from app.database.models.admin import SystemAdmin

    for setter in (TenantAdmin().set_password, SystemAdmin().set_password, UnifiedAuthService(jwt_secret="x" * 40)._hash_password):
        with pytest.raises(PasswordRefused):
            setter(SHORT)


def test_a_portal_user_created_by_an_administrator():
    from pydantic import ValidationError

    from app.api.v1.models.admin.end_user import CreateEndUserRequest

    fields = {name: "x@x.example" if "email" in name else "someone" for name, f in CreateEndUserRequest.model_fields.items() if f.is_required()}
    with pytest.raises(ValidationError) as raised:
        CreateEndUserRequest(**{**fields, "password": SHORT})
    assert PASSWORD_RULE in str(raised.value)
    CreateEndUserRequest(**{**fields, "password": GOOD})


def test_the_rule_is_stated_once():
    """No model states a minimum length for a password of its own."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in [*root.glob("app/**/*.py"), *root.glob("enterprise/brutor_enterprise/**/*.py")]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"password\w*\s*:\s*[^=]*=\s*Field\([^)]*min_length\s*=\s*([2-9]|\d\d)", line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert offenders == []


def test_the_api_description_states_the_rule_of_every_password_it_sets():
    """A client generated from the description refuses a short or blank
    password before it is sent, by the same rule the server holds."""
    import re

    from app.core.password_rule import MIN_PASSWORD_LENGTH, NewPassword, check_password
    from pydantic import TypeAdapter
    from tests.app_factory import create_app

    schema = TypeAdapter(NewPassword).json_schema()
    assert schema["minLength"] == MIN_PASSWORD_LENGTH == 8
    assert f"at least {MIN_PASSWORD_LENGTH} characters" in schema["description"]
    assert "only spaces" in schema["description"]
    # The pattern says "not blank" the way the server does.
    for password, accepted in (("        ", False), ("\t\n        ", False), ("a       ", True), ("pass word", True)):
        assert bool(re.search(schema["pattern"], password)) is accepted, repr(password)
        try:
            check_password(password)
            assert accepted
        except ValueError:
            assert not accepted

    schemas = create_app().openapi()["components"]["schemas"]
    setters = {
        (name, field)
        for name, body in schemas.items()
        for field, spec in body.get("properties", {}).items()
        for option in [spec, *spec.get("anyOf", [])]
        if option.get("minLength") == MIN_PASSWORD_LENGTH and "password" in field
    }
    assert {
        ("TenantAdminCreate", "password"),
        ("EndUserCreate", "password"),
    } <= setters or len(setters) >= 4, sorted(setters)
    for name, field in setters:
        spec = schemas[name]["properties"][field]
        options = [spec, *spec.get("anyOf", [])]
        assert any("pattern" in option for option in options), (name, field)

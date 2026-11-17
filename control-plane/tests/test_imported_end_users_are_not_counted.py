# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
End users created by traffic import are not counted against the end-user
quota.

A traffic import finds the people in a vendor's usage report and creates an
end user for each, so that usage has somebody to belong to. They are marked
(`end_users.source = 'import'`), hold no password anybody can sign in with,
and were counted like everybody else: one import of a large organisation
filled the tenant's quota and every creation of a real user was refused.
The count the quota check makes leaves them out.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.constants.tenant import TenantResourceTypes
from app.database.models.end_user import SOURCE_IMPORT, EndUser
from app.database.models.tenant import Tenant
from app.services.tenant.tenant_service import TenantService
from tests.admin_endpoint_harness import build_admin_harness


@pytest.fixture()
def h():
    harness = build_admin_harness([], logger_name="imported-end-users")
    harness.seed(lambda s: setattr(s.get(Tenant, "t1"), "max_end_users", 2))
    yield harness
    harness.close()


def _user(name: str, source: str, tenant: str = "t1") -> EndUser:
    return EndUser(
        tenant_id=tenant, username=name, email=f"{name}@example.com", display_name=name,
        password_hash="!no-login", is_active=True, source=source,
    )


def _check(h) -> None:
    with h.Factory() as s:
        TenantService.check_quota(s, "t1", TenantResourceTypes.END_USERS)


def test_the_mark_of_an_imported_user_is_its_source():
    assert SOURCE_IMPORT == "import"
    assert EndUser.__table__.c.source.nullable is False
    assert EndUser.__table__.c.source.default.arg == "manual"


def test_imported_users_leave_the_quota_to_the_users_who_can_sign_in(h):
    h.seed(lambda s: s.add_all([_user(f"imp-{i}", SOURCE_IMPORT) for i in range(5)]))
    # Five rows in a tenant whose quota is two: none of them counts.
    _check(h)
    h.seed(lambda s: s.add(_user("jane", "manual")))
    _check(h)
    # Every other source counts: the second user who can sign in fills it.
    h.seed(lambda s: s.add(_user("bob", "saml")))
    with pytest.raises(HTTPException) as refused:
        _check(h)
    assert refused.value.status_code == 429
    assert refused.value.detail["limit"] == "max_end_users"
    assert (refused.value.detail["current"], refused.value.detail["maximum"]) == (2, 2)
    assert "Current: 2, Maximum: 2" in refused.value.detail["message"]


def test_another_tenants_users_and_the_count_helpers_agree(h):
    h.seed(lambda s: s.add_all([
        _user("a", SOURCE_IMPORT), _user("b", "manual"), _user("c", "scim"),
        _user("d", "manual", tenant="t2"), _user("e", SOURCE_IMPORT, tenant="t2"),
    ]))
    with h.Factory() as s:
        assert EndUser.count_for_quota(s, "t1") == 2
        assert EndUser.count_imported(s, "t1") == 1
        assert (EndUser.count_for_quota(s, "t2"), EndUser.count_imported(s, "t2")) == (1, 1)

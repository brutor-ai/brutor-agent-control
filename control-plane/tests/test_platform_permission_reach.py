# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Who comes to hold `platform:catalog:manage` and `system:admin`

Two questions the principal registry left open.

**An installation that already exists.** `platform:catalog:manage` is the
Tenant Admin role's in a single-tenant installation (the Community
composition) and nobody's role where a system administrator exists. A tenant
made before the permission existed gets it the way every new permission
arrives: `sync_role_permissions`, a step of the install seed that runs at
every start, adds what `get_default_tenant_roles` lists and the role lacks.
So: added at the next start in the Community composition, not added in the
composition with a system administrator, never to another role.

**`system:admin` in a tenant role** answers yes to every permission check, so
in the Community composition it also answers yes to `platform:catalog:manage`.
Only a caller that holds `system:admin` may put it into a role (create and
update refuse anyone else). The same must be asked when a role is
*handed out* — a tenant administrator with `roles:assign` or `admin:create`
could give a role holding `system:admin`, one a system administrator had made
in the tenant, to another account. Handing out such a role now takes holding
the permission, like writing it.
"""

from __future__ import annotations

import uuid

import pytest

from app.composition.principals import PLATFORM_CATALOG_MANAGE
from app.core.admin_user.permissions import Permission
from app.database import sync_role_permissions
from app.database.models.admin import TenantAdmin, TenantRole, TenantRolePermission
from app.database.models.tenant import Tenant
from tests.admin_endpoint_harness import build_admin_harness
from tests.principal_matrix import community_principals

SYSTEM_ADMIN = Permission.SYSTEM_ADMIN.value


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([], logger_name="test-platform-permission-reach")
    yield harness
    harness.close()


def _permissions(h, tenant_id, role_name):
    s = h.Factory()
    try:
        role = s.query(TenantRole).filter_by(tenant_id=tenant_id, name=role_name).one()
        return {p.permission for p in role.permissions}
    finally:
        s.close()


def _old_tenant(h, tenant_id):
    """A tenant as an installation made before the permission existed has it:
    system roles `Tenant Admin` (everything of its day) and `Tenant
    Read-Only`, and a custom role that happens to be called like one."""

    def make(s):
        s.add(Tenant(
            id=tenant_id, name=tenant_id, display_name=tenant_id, organization_name=tenant_id,
            primary_contact_email=f"a@{tenant_id}.example", is_active=True, is_default=False,
            is_trial=False, suspended=False, billing_plan="self_hosted", features={},
        ))
        s.flush()
        for name, is_system, perms in (
            ("Tenant Admin", True, [p.value for p in Permission
                                    if p not in (Permission.SYSTEM_ADMIN, Permission.PLATFORM_CATALOG_MANAGE)]),
            ("Tenant Read-Only", True, [Permission.AI_MODEL_READ.value]),
            ("Catalog Curators", False, [Permission.AI_MODEL_READ.value]),
        ):
            role = TenantRole(id=f"role-{uuid.uuid4().hex[:12]}", tenant_id=tenant_id, name=name,
                              description="", is_system=is_system)
            s.add(role)
            s.flush()
            for value in perms:
                s.add(TenantRolePermission(id=f"perm-{uuid.uuid4().hex[:10]}", role_id=role.id, permission=value))

    h.seed(make)


def _sync(h):
    s = h.Factory()
    try:
        sync_role_permissions(s)
        s.commit()
    finally:
        s.close()


# =============================================================================
# 4a. A tenant that already exists
# =============================================================================

class TestAnExistingTenant:

    def test_in_the_community_composition_the_next_start_gives_it_to_tenant_admin(self, h):
        _old_tenant(h, "old-ce")
        before = {role: _permissions(h, "old-ce", role) for role in ("Tenant Read-Only", "Catalog Curators")}
        with community_principals():
            _sync(h)
            assert PLATFORM_CATALOG_MANAGE in _permissions(h, "old-ce", "Tenant Admin")
            assert SYSTEM_ADMIN not in _permissions(h, "old-ce", "Tenant Admin")
            # Not the read-only role's, and not a custom role's whatever it is called.
            assert PLATFORM_CATALOG_MANAGE not in _permissions(h, "old-ce", "Tenant Read-Only")
            assert _permissions(h, "old-ce", "Catalog Curators") == before["Catalog Curators"]
            # Again: nothing more.
            first = _permissions(h, "old-ce", "Tenant Admin")
            _sync(h)
            assert _permissions(h, "old-ce", "Tenant Admin") == first
            s = h.Factory()
            try:
                role = s.query(TenantRole).filter_by(tenant_id="old-ce", name="Tenant Admin").one()
                held = [p.permission for p in role.permissions]
                assert len(held) == len(set(held))
            finally:
                s.close()


    def test_the_sync_is_a_step_of_the_install_seed_of_both_editions(self):
        from app.composition.seeding import INSTALL_SEED

        steps = {step.name: step for step in INSTALL_SEED}
        assert steps["role_permissions"].feature is None


# =============================================================================
# 4b. `system:admin` in a tenant role
# =============================================================================

@pytest.fixture(scope="module")
def power_role(h):
    """A role holding `system:admin` in t1, made by the system administrator
    through the API, and one administrator who holds it."""
    r = h.client.post(
        "/v1/admin/users/roles?tenant_id=t1", headers=h.sys_headers(),
        json={"name": "PlatformOps", "permissions": [SYSTEM_ADMIN]},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _new_admin(h, username, role_id=None, tenant_id="t1"):
    from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
    from tests.admin_endpoint_harness import JWT_SECRET, PASSWORD

    out = {}

    def make(s):
        role = role_id or s.query(TenantRole).filter_by(tenant_id=tenant_id, name="Tenant Admin").one().id
        out["id"] = UnifiedAuthService(jwt_secret=JWT_SECRET).create_tenant_admin(
            username=username, email=f"{username}@{tenant_id}.example", password=PASSWORD,
            tenant_id=tenant_id, db=s, role_id=role,
        ).id

    h.seed(make)
    return out["id"]


def _role_of(h, admin_id):
    s = h.Factory()
    try:
        return s.query(TenantAdmin).filter_by(id=admin_id).one().role_id
    finally:
        s.close()


def _login(h, username, tenant_id="t1"):
    from tests.admin_endpoint_harness import PASSWORD

    r = h.client.post("/v1/admin/users/tenant/login",
                      json={"username": username, "password": PASSWORD, "tenant_id": tenant_id})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class composition:
    """Both compositions, by name."""

    def __init__(self, name):
        self.ctx = community_principals() if name == "community" else None

    def __enter__(self):
        if self.ctx:
            self.ctx.__enter__()

    def __exit__(self, *exc):
        if self.ctx:
            self.ctx.__exit__(*exc)
        return False


COMPOSITIONS = ["community", "both"]


@pytest.mark.parametrize("name", COMPOSITIONS)
class TestATenantAdministratorCannotReachSystemAdmin:
    def test_not_by_creating_a_role(self, h, name):
        headers = h.headers("t1")
        with composition(name):
            for permissions in ([SYSTEM_ADMIN], [Permission.AI_MODEL_READ.value, SYSTEM_ADMIN]):
                r = h.client.post("/v1/admin/users/roles", headers=headers,
                                  json={"name": f"esc-{uuid.uuid4().hex[:6]}", "permissions": permissions})
                assert r.status_code == 403, r.text
            # What is not the permission's exact name is no permission at all.
            for almost in ("SYSTEM:ADMIN", " system:admin", "system:admin ", "system:*", "*"):
                r = h.client.post("/v1/admin/users/roles", headers=headers,
                                  json={"name": f"esc-{uuid.uuid4().hex[:6]}", "permissions": [almost]})
                assert r.status_code == 400, (almost, r.text)
        s = h.Factory()
        try:
            assert s.query(TenantRole).filter(TenantRole.name.like("esc-%")).count() == 0
        finally:
            s.close()

    def test_not_by_updating_a_role(self, h, name):
        headers = h.headers("t1")
        with composition(name):
            r = h.client.post("/v1/admin/users/roles", headers=headers,
                              json={"name": f"plain-{uuid.uuid4().hex[:6]}", "permissions": [Permission.AI_MODEL_READ.value]})
            assert r.status_code == 200, r.text
            role_id = r.json()["id"]
            r = h.client.patch(f"/v1/admin/users/roles/{role_id}", headers=headers,
                               json={"permissions": [Permission.AI_MODEL_READ.value, SYSTEM_ADMIN]})
            assert r.status_code == 403, r.text
        s = h.Factory()
        try:
            assert {p.permission for p in s.query(TenantRole).filter_by(id=role_id).one().permissions} == {
                Permission.AI_MODEL_READ.value
            }
        finally:
            s.close()


    def test_any_other_role_is_assigned_and_created_with_as_before(self, h, name):
        target = _new_admin(h, f"plain_peer_{name}")
        headers = h.headers("t1")
        s = h.Factory()
        try:
            admin_role = s.query(TenantRole).filter_by(tenant_id="t1", name="Tenant Admin").one().id
        finally:
            s.close()
        with composition(name):
            r = h.client.post("/v1/admin/users/roles", headers=headers,
                              json={"name": f"reader-{name}", "permissions": [Permission.AI_MODEL_READ.value]})
            assert r.status_code == 200, r.text
            reader = r.json()["id"]
            r = h.client.patch(f"/v1/admin/users/tenant/{target}/role", headers=headers, json={"role_id": reader})
            assert r.status_code == 200, r.text
            r = h.client.patch(f"/v1/admin/users/tenant/{target}/role", headers=headers, json={"role_id": admin_role})
            assert r.status_code == 200, r.text
            r = h.client.post("/v1/admin/users/tenant", headers=headers, json={
                "tenant_id": "t1", "username": f"third_{name}", "email": f"third_{name}@t1.example",
                "password": "Secret123!", "role_id": reader,
            })
            assert r.status_code == 201, r.text



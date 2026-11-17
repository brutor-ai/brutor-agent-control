# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The paths of the admin RBAC router that decide who may touch which admin:
per-admin read/update/delete, sessions, roles, audit reads and photos.

Every refusal asserts the row afterwards, because a route that answers 403 after
it wrote is worse than one that answers 200.
"""

from __future__ import annotations

import pytest

from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.unified_auth_service import UnifiedAuthService
from app.database.models.admin import (
    TenantAdmin,
    TenantAdminAuditLog,
    TenantAdminSession,
    TenantRole,
)
from tests.admin_endpoint_harness import (
    JWT_SECRET,
    PASSWORD,
    build_admin_harness,
    throwaway_pg_database,
)

BASE = "/v1/admin/users"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture(scope="module")
def h():
    for url in throwaway_pg_database("rbac_admins"):
        harness = build_admin_harness([], database_url=url)
        yield harness
        harness.close()


def _new_admin(h, tenant_id: str, username: str) -> str:
    """A second admin to act on, holding the tenant's admin role. Returns its id."""
    s = h.Factory()
    try:
        admin = UnifiedAuthService(jwt_secret=JWT_SECRET).create_tenant_admin(
            username=username,
            email=f"{username}@{tenant_id}.example",
            password=PASSWORD,
            tenant_id=tenant_id,
            db=s,
            role_id=f"role-{tenant_id}-admin",
        )
        s.commit()
    finally:
        s.close()
    return _admin_row(h, username).id


def _admin_row(h, username: str) -> TenantAdmin:
    s = h.Factory()
    try:
        row = s.query(TenantAdmin).filter(TenantAdmin.username == username).one()
        s.expunge(row)
        return row
    finally:
        s.close()


def _audit_actions(h, tenant_id: str) -> list[str]:
    s = h.Factory()
    try:
        return [
            a
            for (a,) in s.query(TenantAdminAuditLog.action)
            .filter(TenantAdminAuditLog.tenant_id == tenant_id)
            .all()
        ]
    finally:
        s.close()


class TestReadAdmin:
    def test_returns_the_admin_with_no_secret(self, h):
        target = _new_admin(h, "t1", "read_target")
        r = h.client.get(f"{BASE}/tenant/{target}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["id"], body["tenant_id"]) == (target, "t1")
        assert "password_hash" not in body

    def test_another_tenants_admin_is_refused(self, h):
        target = _new_admin(h, "t2", "read_target_t2")
        r = h.client.get(f"{BASE}/tenant/{target}", headers=h.headers("t1"))
        assert r.status_code == 403
        assert "t2" not in r.text and "read_target_t2" not in r.text


    def test_unknown_is_404(self, h):
        assert h.client.get(f"{BASE}/tenant/nope", headers=h.headers("t1")).status_code == 404

    def test_without_the_read_permission_is_403(self, h):
        target = _new_admin(h, "t1", "read_target_perm")
        limited = h.limited_headers("t1", [Permission.ROLES_READ])
        assert h.client.get(f"{BASE}/tenant/{target}", headers=limited).status_code == 403

    def test_no_token_is_refused(self, h):
        assert h.client.get(f"{BASE}/tenant/x").status_code in (401, 403)

    def test_a_token_signed_with_the_wrong_secret_is_refused(self, h):
        import jwt as pyjwt

        forged = pyjwt.encode(
            {"sub": "x", "admin_type": "system", "exp": 4102444800}, "wrong-secret", algorithm="HS256"
        )
        r = h.client.get(f"{BASE}/tenant/x", headers={"Authorization": f"Bearer {forged}"})
        assert r.status_code == 401

    def test_an_expired_token_is_refused(self, h):
        import jwt as pyjwt

        admin = _admin_row(h, "admin_t1")
        expired = pyjwt.encode(
            {"sub": admin.id, "admin_id": admin.id, "admin_type": "tenant", "tenant_id": "t1", "exp": 1000},
            JWT_SECRET,
            algorithm="HS256",
        )
        r = h.client.get(f"{BASE}/tenant/x", headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401


class TestUpdateAdmin:
    def test_updates_allowed_fields_and_audits(self, h):
        target = _new_admin(h, "t1", "upd_target")
        r = h.client.patch(
            f"{BASE}/tenant/{target}",
            json={"full_name": "New Name", "is_active": False},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        row = _admin_row(h, "upd_target")
        assert (row.full_name, row.is_active) == ("New Name", False)
        assert "tenant_admin_updated" in _audit_actions(h, "t1")

    def test_fields_outside_the_allow_list_are_refused(self, h):
        target = _new_admin(h, "t1", "upd_allowlist")
        r = h.client.patch(
            f"{BASE}/tenant/{target}",
            json={"tenant_id": "t2", "username": "stolen", "role_id": "role-t2-admin"},
            headers=h.headers("t1"),
        )
        assert r.status_code == 422, r.text
        assert {tuple(e["loc"]) for e in r.json()["detail"]} == {
            ("body", "tenant_id"), ("body", "username"), ("body", "role_id")
        }
        row = _admin_row(h, "upd_allowlist")
        assert (row.tenant_id, row.role_id) == ("t1", "role-t1-admin")

    def test_another_tenants_admin_cannot_be_updated(self, h):
        target = _new_admin(h, "t2", "upd_cross")
        r = h.client.patch(
            f"{BASE}/tenant/{target}", json={"is_active": False}, headers=h.headers("t1")
        )
        assert r.status_code == 403
        assert _admin_row(h, "upd_cross").is_active is True

    def test_setting_another_admins_password_lets_them_log_in_with_it(self, h):
        target = _new_admin(h, "t1", "upd_pw")
        r = h.client.patch(
            f"{BASE}/tenant/{target}", json={"password": "Fresh-Secret-1!"}, headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text
        login = lambda pw: h.client.post(
            f"{BASE}/tenant/login", json={"username": "upd_pw", "password": pw, "tenant_id": "t1"}
        ).status_code
        assert login("Fresh-Secret-1!") == 200
        assert login(PASSWORD) == 401

    def test_own_password_cannot_be_changed_here(self, h):
        before = _admin_row(h, "admin_t1").password_hash
        me = _admin_row(h, "admin_t1").id
        r = h.client.patch(f"{BASE}/tenant/{me}", json={"password": "x" * 12}, headers=h.headers("t1"))
        assert r.status_code == 403
        assert _admin_row(h, "admin_t1").password_hash == before

    def test_roles_cannot_be_changed_here(self, h):
        """Nobody's, the caller's own included: `roles` is not a field of
        this route (the role is assigned on `/tenant/{id}/role`)."""
        me = _admin_row(h, "admin_t1").id
        r = h.client.patch(f"{BASE}/tenant/{me}", json={"roles": ["x"]}, headers=h.headers("t1"))
        assert r.status_code == 422

    def test_unknown_is_404(self, h):
        r = h.client.patch(f"{BASE}/tenant/nope", json={"full_name": "x"}, headers=h.headers("t1"))
        assert r.status_code == 404

    def test_read_only_admin_is_403(self, h):
        target = _new_admin(h, "t1", "upd_perm")
        limited = h.limited_headers("t1", [Permission.ADMIN_USER_READ])
        r = h.client.patch(f"{BASE}/tenant/{target}", json={"full_name": "x"}, headers=limited)
        assert r.status_code == 403
        assert _admin_row(h, "upd_perm").full_name != "x"


class TestDeleteAdmin:
    def test_soft_delete_deactivates_and_keeps_the_row(self, h):
        target = _new_admin(h, "t1", "del_soft")
        r = h.client.delete(f"{BASE}/tenant/{target}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["deletion_type"] == "soft"
        assert _admin_row(h, "del_soft").is_active is False
        assert "tenant_admin_soft_deleted" in _audit_actions(h, "t1")

    def test_deactivated_admin_cannot_log_in(self, h):
        target = _new_admin(h, "t1", "del_login")
        h.client.delete(f"{BASE}/tenant/{target}", headers=h.headers("t1"))
        r = h.client.post(
            f"{BASE}/tenant/login", json={"username": "del_login", "password": PASSWORD, "tenant_id": "t1"}
        )
        assert r.status_code in (401, 403)

    def test_tenant_admin_cannot_hard_delete(self, h):
        target = _new_admin(h, "t1", "del_hard_denied")
        r = h.client.delete(f"{BASE}/tenant/{target}?hard_delete=true", headers=h.headers("t1"))
        assert r.status_code == 403
        assert _admin_row(h, "del_hard_denied").is_active is True


    def test_cannot_delete_yourself(self, h):
        me = _admin_row(h, "admin_t1").id
        r = h.client.delete(f"{BASE}/tenant/{me}", headers=h.headers("t1"))
        assert r.status_code == 403
        assert _admin_row(h, "admin_t1").is_active is True

    def test_another_tenants_admin_cannot_be_deleted(self, h):
        target = _new_admin(h, "t2", "del_cross")
        r = h.client.delete(f"{BASE}/tenant/{target}", headers=h.headers("t1"))
        assert r.status_code == 403
        assert _admin_row(h, "del_cross").is_active is True

    def test_unknown_is_404(self, h):
        assert h.client.delete(f"{BASE}/tenant/nope", headers=h.headers("t1")).status_code == 404

    def test_without_the_delete_permission_is_403(self, h):
        target = _new_admin(h, "t1", "del_perm")
        limited = h.limited_headers("t1", [Permission.ADMIN_USER_UPDATE])
        assert h.client.delete(f"{BASE}/tenant/{target}", headers=limited).status_code == 403
        assert _admin_row(h, "del_perm").is_active is True


class TestSessions:
    def _session_of(self, h, username: str) -> TenantAdminSession:
        admin = _admin_row(h, username)
        s = h.Factory()
        try:
            row = (
                s.query(TenantAdminSession)
                .filter(TenantAdminSession.admin_id == admin.id, TenantAdminSession.is_active == True)  # noqa: E712
                .first()
            )
            assert row is not None
            s.expunge(row)
            return row
        finally:
            s.close()

    def test_tenant_admin_sees_only_its_tenant_and_cannot_widen_it(self, h):
        h.headers("t1"), h.headers("t2")
        # Naming another tenant is refused, not silently answered about its
        # own (the route asks `tenant_scope`; it must not ignore the
        # parameter and answer 200 with t1's sessions).
        r = h.client.get(f"{BASE}/tenant/sessions?tenant_id=t2", headers=h.headers("t1"))
        assert r.status_code == 403, r.text
        assert "sessions" not in r.json()
        for own in ("", "?tenant_id=t1"):
            r = h.client.get(f"{BASE}/tenant/sessions{own}", headers=h.headers("t1"))
            assert r.status_code == 200, r.text
            assert {x["tenant_id"] for x in r.json()["sessions"]} == {"t1"}
        assert r.json()["current_session_id"] is not None
        assert [x for x in r.json()["sessions"] if x["is_current"]]


    def test_another_tenants_session_looks_absent_and_stays_alive(self, h):
        h.headers("t2")
        victim = self._session_of(h, "admin_t2")
        r = h.client.post(f"{BASE}/tenant/sessions/{victim.id}/revoke", headers=h.headers("t1"))
        assert r.status_code == 404
        assert h.client.get(f"{BASE}/me", headers=h.headers("t2")).status_code == 200

    def test_unknown_session_is_404(self, h):
        r = h.client.post(f"{BASE}/tenant/sessions/nope/revoke", headers=h.headers("t1"))
        assert r.status_code == 404

    def _revoker(self, h) -> dict:
        """Who ends the session: here an administrator of the same tenant.
        The Enterprise half runs the test below as the system administrator."""
        return h.headers("t1")

    def test_revoke_kills_the_token_and_a_second_revoke_is_a_no_op(self, h):
        h.limited_headers("t1", [Permission.ROLES_READ], username="sess_victim")
        victim_headers = {"Authorization": f"Bearer {h._tokens['sess_victim']}"}
        assert h.client.get(f"{BASE}/me", headers=victim_headers).status_code == 200
        victim = self._session_of(h, "sess_victim")
        r = h.client.post(f"{BASE}/tenant/sessions/{victim.id}/revoke", headers=self._revoker(h))
        assert r.status_code == 200, r.text
        assert h.client.get(f"{BASE}/me", headers=victim_headers).status_code == 401
        again = h.client.post(f"{BASE}/tenant/sessions/{victim.id}/revoke", headers=self._revoker(h))
        assert again.json()["already_inactive"] is True
        h._tokens.pop("sess_victim")


class TestRoles:
    def _role(self, h, name: str, tenant: str = "t1") -> str:
        r = h.client.post(
            f"{BASE}/roles",
            json={"name": name, "description": "d", "permissions": [Permission.ROLES_READ.value]},
            headers=h.headers(tenant),
        )
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    def _row(self, h, role_id: str):
        s = h.Factory()
        try:
            row = s.query(TenantRole).filter(TenantRole.id == role_id).first()
            return None if row is None else (row.name, row.description, sorted(row.get_permissions()))
        finally:
            s.close()

    def test_update_changes_name_description_and_permissions(self, h):
        rid = self._role(h, "ops-1")
        r = h.client.patch(
            f"{BASE}/roles/{rid}",
            json={"name": "ops-2", "description": "new", "permissions": [Permission.ADMIN_USER_READ.value]},
            headers=h.headers("t1"),
        )
        assert r.status_code == 200, r.text
        assert self._row(h, rid) == ("ops-2", "new", [Permission.ADMIN_USER_READ.value])
        assert "role_updated" in _audit_actions(h, "t1")

    def test_update_to_a_taken_name_is_refused(self, h):
        rid = self._role(h, "taken-a")
        self._role(h, "taken-b")
        r = h.client.patch(f"{BASE}/roles/{rid}", json={"name": "taken-b"}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert self._row(h, rid)[0] == "taken-a"

    def test_update_with_an_unknown_permission_keeps_the_old_set(self, h):
        rid = self._role(h, "bad-perm")
        r = h.client.patch(f"{BASE}/roles/{rid}", json={"permissions": ["nope:nope"]}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert self._row(h, rid)[2] == [Permission.ROLES_READ.value]

    def test_tenant_admin_cannot_grant_system_admin_by_update(self, h):
        rid = self._role(h, "escalate")
        r = h.client.patch(
            f"{BASE}/roles/{rid}", json={"permissions": [Permission.SYSTEM_ADMIN.value]}, headers=h.headers("t1")
        )
        assert r.status_code == 403
        assert Permission.SYSTEM_ADMIN.value not in self._row(h, rid)[2]

    def test_another_tenants_role_looks_absent(self, h):
        rid = self._role(h, "t2-role", tenant="t2")
        assert h.client.patch(f"{BASE}/roles/{rid}", json={"name": "x"}, headers=h.headers("t1")).status_code == 404
        assert h.client.delete(f"{BASE}/roles/{rid}", headers=h.headers("t1")).status_code == 404
        assert self._row(h, rid)[0] == "t2-role"


    def test_a_role_in_use_cannot_be_deleted(self, h):
        rid = self._role(h, "in-use")
        target = _new_admin(h, "t1", "role_holder")
        assert (
            h.client.patch(f"{BASE}/tenant/{target}/role", json={"role_id": rid}, headers=h.headers("t1")).status_code
            == 200
        )
        r = h.client.delete(f"{BASE}/roles/{rid}", headers=h.headers("t1"))
        assert r.status_code == 409
        assert [row["name"] for row in r.json()["detail"]["used_by"]["administrators"]] == ["role_holder"]
        assert self._row(h, rid) is not None

    def test_delete_removes_the_role_and_audits_it(self, h):
        rid = self._role(h, "doomed")
        r = h.client.delete(f"{BASE}/roles/{rid}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert self._row(h, rid) is None
        assert "role_deleted" in _audit_actions(h, "t1")

    def test_assign_requires_the_admin_to_be_in_the_tenant(self, h):
        rid = self._role(h, "assign-cross")
        target = _new_admin(h, "t2", "assign_t2")
        r = h.client.patch(f"{BASE}/tenant/{target}/role", json={"role_id": rid}, headers=h.headers("t1"))
        assert r.status_code == 404
        assert _admin_row(h, "assign_t2").role_id == "role-t2-admin"

    def test_assign_unknown_role_is_404(self, h):
        target = _new_admin(h, "t1", "assign_unknown")
        r = h.client.patch(f"{BASE}/tenant/{target}/role", json={"role_id": "nope"}, headers=h.headers("t1"))
        assert r.status_code == 404
        assert _admin_row(h, "assign_unknown").role_id == "role-t1-admin"

    def test_changing_a_role_changes_what_the_holder_may_do(self, h):
        """Permissions are read from the role at request time."""
        rid = self._role(h, "narrowed")
        target = _new_admin(h, "t1", "role_effect")
        token = h.client.post(
            f"{BASE}/tenant/login", json={"username": "role_effect", "password": PASSWORD, "tenant_id": "t1"}
        ).json()["access_token"]
        mine = {"Authorization": f"Bearer {token}"}
        assert h.client.get(f"{BASE}/tenant/{target}", headers=mine).status_code == 200
        h.client.patch(f"{BASE}/tenant/{target}/role", json={"role_id": rid}, headers=h.headers("t1"))
        assert h.client.get(f"{BASE}/tenant/{target}", headers=mine).status_code == 403


class TestAuditReads:
    def _seed(self, h, tenant_id: str, action: str, success: bool = True) -> None:
        def add(s):
            s.add(
                TenantAdminAuditLog(
                    id=f"log-{tenant_id}-{action}",
                    tenant_id=tenant_id,
                    admin_id=_admin_row(h, f"admin_{tenant_id}").id,
                    action=action,
                    resource_type="thing",
                    resource_id=f"res-{action}",
                    success=success,
                    ip_address="10.0.0.1",
                    request_method="POST",
                    request_path=f"/x/{action}",
                )
            )

        h.seed(add)

    @pytest.fixture(scope="class", autouse=True)
    def rows(self, h):
        self._seed(h, "t1", "zz_alpha")
        self._seed(h, "t1", "zz_beta", success=False)
        self._seed(h, "t2", "zz_secret_t2")

    def test_get_pins_tenant_admin_to_its_tenant(self, h):
        r = h.client.get(f"{BASE}/audit-logs?limit=1000", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert "zz_secret_t2" not in {x["action"] for x in r.json()["logs"]}
        assert "zz_alpha" in {x["action"] for x in r.json()["logs"]}

    def test_get_filters(self, h):
        me = _admin_row(h, "admin_t1").id
        r = h.client.get(
            f"{BASE}/audit-logs?action_filter=zz_&success_only=false&user_filter={me}",
            headers=h.headers("t1"),
        )
        assert [x["action"] for x in r.json()["logs"]] == ["zz_beta"]
        r = h.client.get(f"{BASE}/audit-logs?action_filter=zz_&user_filter=nobody", headers=h.headers("t1"))
        assert r.json()["logs"] == []


    def test_without_audit_permission_is_403(self, h):
        limited = h.limited_headers("t1", [Permission.ROLES_READ])
        assert h.client.get(f"{BASE}/audit-logs", headers=limited).status_code == 403
        for path in ("search", "facets", "suggest"):
            r = h.client.post(f"{BASE}/audit-logs/{path}", json={"field": "action", "prefix": "a"}, headers=limited)
            assert r.status_code == 403, path

    def test_search_cannot_be_widened_by_a_tenant_admin(self, h):
        r = h.client.post(
            f"{BASE}/audit-logs/search", json={"tenant_id": "t2", "q": "zz_"}, headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert {x["action"] for x in r.json()["logs"]} == {"zz_alpha", "zz_beta"}


    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"actions": ["zz_alpha"]}, {"zz_alpha"}),
            ({"success": False, "q": "zz_"}, {"zz_beta"}),
            ({"resource_types": ["nope"]}, set()),
            ({"ip_addresses": ["10.0.0.1"], "q": "zz_"}, {"zz_alpha", "zz_beta"}),
            ({"request_methods": ["DELETE"], "q": "zz_"}, set()),
            ({"admin_ids": ["nobody"], "q": "zz_"}, set()),
            ({"hours_ago": 1, "q": "zz_"}, {"zz_alpha", "zz_beta"}),
            ({"hours_ago": "not-a-number", "q": "zz_"}, {"zz_alpha", "zz_beta"}),
            ({"q": "/x/zz_alpha"}, {"zz_alpha"}),
        ],
    )
    def test_search_filters(self, h, body, expected):
        r = h.client.post(f"{BASE}/audit-logs/search", json=body, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert {x["action"] for x in r.json()["logs"]} == expected

    def test_facets_do_not_leak_another_tenant(self, h):
        r = h.client.post(f"{BASE}/audit-logs/facets", json={"fields": ["action"]}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        values = {x["value"] for x in r.json()["facets"]["action"]}
        assert "zz_alpha" in values and "zz_secret_t2" not in values

    def test_suggest_does_not_leak_another_tenant(self, h):
        r = h.client.post(
            f"{BASE}/audit-logs/suggest", json={"field": "action", "prefix": "zz_"}, headers=h.headers("t1")
        )
        assert r.status_code == 200, r.text
        assert {x["value"] for x in r.json()["suggestions"]} == {"zz_alpha", "zz_beta"}

    def test_suggest_refuses_a_column_outside_the_list(self, h):
        r = h.client.post(
            f"{BASE}/audit-logs/suggest", json={"field": "details", "prefix": "a"}, headers=h.headers("t1")
        )
        assert r.status_code == 422

    def test_suggest_with_no_prefix_is_empty(self, h):
        r = h.client.post(f"{BASE}/audit-logs/suggest", json={"field": "action"}, headers=h.headers("t1"))
        assert r.json() == {"suggestions": []}


class TestPhotos:
    def _upload(self, h, target: str, headers: dict, content_type: str = "image/png", data: bytes = PNG):
        return h.client.post(
            f"{BASE}/tenant/{target}/photo", files={"file": ("a.png", data, content_type)}, headers=headers
        )

    def test_round_trip_serves_only_with_the_capability_token(self, h):
        target = _new_admin(h, "t1", "photo_ok")
        r = self._upload(h, target, h.headers("t1"))
        assert r.status_code == 200, r.text
        token = r.json()["avatar_url"].split("?h=")[1]
        got = h.client.get(f"{BASE}/tenant/{target}/photo?h={token}")
        assert got.status_code == 200 and got.content == PNG
        assert got.headers["content-type"] == "image/png"
        assert h.client.get(f"{BASE}/tenant/{target}/photo").status_code == 404
        assert h.client.get(f"{BASE}/tenant/{target}/photo?h=wrong").status_code == 404
        cached = h.client.get(f"{BASE}/tenant/{target}/photo?h={token}", headers={"If-None-Match": got.headers["etag"]})
        assert cached.status_code == 304

    def test_delete_removes_it(self, h):
        target = _new_admin(h, "t1", "photo_del")
        token = self._upload(h, target, h.headers("t1")).json()["avatar_url"].split("?h=")[1]
        assert h.client.delete(f"{BASE}/tenant/{target}/photo", headers=h.headers("t1")).status_code == 200
        assert _admin_row(h, "photo_del").photo_data is None
        assert h.client.get(f"{BASE}/tenant/{target}/photo?h={token}").status_code == 404

    def test_another_tenant_cannot_touch_it(self, h):
        target = _new_admin(h, "t2", "photo_cross")
        assert self._upload(h, target, h.headers("t1")).status_code == 404
        assert h.client.delete(f"{BASE}/tenant/{target}/photo", headers=h.headers("t1")).status_code == 404
        assert _admin_row(h, "photo_cross").photo_data is None


    @pytest.mark.parametrize("content_type", ["image/svg+xml", "text/html"])
    def test_active_content_is_refused(self, h, content_type):
        target = _new_admin(h, "t1", f"photo_{content_type[:4]}_{content_type[-3:]}")
        assert self._upload(h, target, h.headers("t1"), content_type=content_type).status_code == 400

    def test_too_large_is_refused(self, h):
        target = _new_admin(h, "t1", "photo_big")
        r = self._upload(h, target, h.headers("t1"), data=b"0" * (5 * 1024 * 1024 + 1))
        assert r.status_code == 400
        assert _admin_row(h, "photo_big").photo_data is None

    def test_no_file_is_refused(self, h):
        target = _new_admin(h, "t1", "photo_none")
        r = h.client.post(f"{BASE}/tenant/{target}/photo", data={"other": "x"}, headers=h.headers("t1"))
        assert r.status_code == 400

    def test_an_admin_without_user_permissions_cannot_change_someone_elses_photo(self, h):
        target = _new_admin(h, "t1", "photo_perm")
        limited = h.limited_headers("t1", [Permission.ROLES_READ])
        assert self._upload(h, target, limited).status_code == 403
        assert h.client.delete(f"{BASE}/tenant/{target}/photo", headers=limited).status_code == 403
        assert _admin_row(h, "photo_perm").photo_data is None

    def test_an_admin_without_user_permissions_can_change_its_own_photo(self, h):
        limited = h.limited_headers("t1", [Permission.ROLES_READ], username="photo_self")
        me = _admin_row(h, "photo_self").id
        assert self._upload(h, me, limited).status_code == 200
        assert h.client.delete(f"{BASE}/tenant/{me}/photo", headers=limited).status_code == 200


def test_the_caller_is_told_whether_it_operates_the_installation(h):
    """`/me` says whether the installation has a system administrator and
    whether the caller is the one who operates what every tenant shares
    (the scheduler actions), by the rule those routes are guarded with."""
    from fastapi import HTTPException

    from app.constants.admin import AdminType
    from app.core.admin_user import dependencies
    from app.core.admin_user.dependencies import PRINCIPALS

    r = h.client.get(f"{BASE}/me", headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    body = r.json()
    has_system_admin = PRINCIPALS.has_platform_wide()
    assert body["installation_has_system_admin"] is has_system_admin
    # A tenant administrator operates the installation only where it is the installation.
    assert body["is_installation_operator"] is (not has_system_admin)

    class Caller:
        id = "x"

    try:
        dependencies.require_installation_operator((Caller(), AdminType.TENANT_ADMIN_TYPE))
        let_in = True
    except HTTPException as refused:
        assert refused.status_code == 403
        let_in = False
    assert let_in is body["is_installation_operator"]

    document = h.client.app.openapi()
    answered = document["paths"][f"{BASE}/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    schema = document["components"]["schemas"][answered["$ref"].rsplit("/", 1)[1]]
    assert {"installation_has_system_admin", "is_installation_operator"} <= set(schema["required"])
    # Only `/me` says it: a listed administrator is not "the caller".
    assert "is_installation_operator" not in document["components"]["schemas"]["AdminResponse"]["properties"]

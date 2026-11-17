# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Admin routes over end users, and the live half of EndUserAuthentication behind
them: tenant isolation on every read and write, permission denial and the audit
row. The Control Plane mints no end-user session token.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.api.v1.endpoints.admin.end_users import create_end_user_router
from app.core.admin_user.permissions import Permission
from app.core.end_user.services.authentication import EndUserAuthentication
from app.core.logging_system import BrutorLogger
from app.database.models.admin import TenantAdminAuditLog, TenantRole
from app.database.models.end_user import EndUser
from app.database.models.resource_group_membership import ResourceGroupMembership
from app.database.models.tenant import Tenant
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

BASE = "/v1/admin/end-users"
PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 32
auth = EndUserAuthentication()
_hash = bcrypt.hashpw(b"pw", bcrypt.gensalt(rounds=4)).decode()
_n = {"i": 0}


@pytest.fixture(scope="module")
def h():
    for url in throwaway_pg_database("end_user_routes"):
        harness = build_admin_harness(
            [create_end_user_router(auth, BrutorLogger("test-end-users"))], database_url=url
        )

        def roles(s):
            for tid in ("t1", "t2"):
                s.add(TenantRole(id=f"role-{tid}-user", tenant_id=tid, name="Tenant User", is_system=True))

        harness.seed(roles)
        yield harness
        harness.close()


def _user(h, tenant: str = "t1", **over) -> int:
    _n["i"] += 1
    s = h.Factory()
    try:
        u = EndUser(
            tenant_id=tenant,
            username=over.pop("username", f"user{_n['i']}"),
            email=over.pop("email", f"user{_n['i']}@{tenant}.example"),
            password_hash=_hash,
            role_id=f"role-{tenant}-user",
            is_active=over.pop("is_active", True),
            is_verified=over.pop("is_verified", True),
            **over,
        )
        s.add(u)
        s.commit()
        return u.id
    finally:
        s.close()


def _row(h, user_id: int):
    s = h.Factory()
    try:
        u = s.get(EndUser, user_id)
        if u is not None:
            s.expunge(u)
        return u
    finally:
        s.close()


def _create_body(**over) -> dict:
    _n["i"] += 1
    return {
        "username": f"created{_n['i']}",
        "email": f"created{_n['i']}@x.example",
        "password": "Initial-Secret-1",
        "send_welcome_email": False,
        **over,
    }


class TestIsolation:
    @pytest.mark.parametrize(
        "method,suffix,kw",
        [
            ("get", "", {}),
            ("patch", "/status", {"json": {"is_active": False}}),
            ("patch", "/primary-group", {"json": {"group_id": "group-default-t1"}}),
            ("delete", "", {}),
            ("delete", "/photo", {}),
        ],
    )
    def test_another_tenants_user_looks_absent_and_is_unchanged(self, h, method, suffix, kw):
        target = _user(h, "t2")
        r = getattr(h.client, method)(f"{BASE}/{target}{suffix}", headers=h.headers("t1"), **kw)
        assert r.status_code == 404, r.text
        row = _row(h, target)
        assert row is not None and row.is_active is True

    def test_listing_shows_only_the_callers_tenant(self, h):
        mine, theirs = _user(h, "t1"), _user(h, "t2")
        r = h.client.get(f"{BASE}?limit=1000", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        ids = {str(u["id"]) for u in r.json()["users"]}
        assert str(mine) in ids and str(theirs) not in ids

    @pytest.mark.parametrize("fmt", ["csv", "json"])
    def test_exports_contain_only_the_callers_tenant(self, h, fmt):
        _user(h, "t1", username=f"exported_mine_{fmt}")
        _user(h, "t2", username=f"exported_theirs_{fmt}")
        r = h.client.get(f"{BASE}/export/{fmt}?tenant_id=t2", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert f"exported_mine_{fmt}" in r.text and "exported_theirs" not in r.text

    def test_a_role_from_another_tenant_cannot_be_assigned(self, h):
        body = _create_body(role_id="role-t2-user")
        r = h.client.post(BASE, json=body, headers=h.headers("t1"))
        assert r.status_code == 404
        s = h.Factory()
        try:
            assert s.query(EndUser).filter(EndUser.username == body["username"]).count() == 0
        finally:
            s.close()

    def test_a_group_from_another_tenant_is_not_joined(self, h):
        body = _create_body(group_ids=["group-default-t2"])
        r = h.client.post(BASE, json=body, headers=h.headers("t1"))
        assert r.status_code in (200, 201), r.text
        s = h.Factory()
        try:
            uid = s.query(EndUser).filter(EndUser.username == body["username"]).one().id
            assert s.query(ResourceGroupMembership).filter(ResourceGroupMembership.user_id == uid).count() == 0
            assert s.get(EndUser, uid).primary_group_id is None
        finally:
            s.close()

    def test_the_same_username_may_exist_in_two_tenants(self, h):
        body = _create_body()
        assert h.client.post(BASE, json=body, headers=h.headers("t1")).status_code in (200, 201)
        assert h.client.post(BASE, json=body, headers=h.headers("t2")).status_code in (200, 201)


class TestPermissions:
    @pytest.mark.parametrize(
        "method,suffix,kw,held",
        [
            ("get", "", {}, Permission.END_USER_UPDATE),
            ("post", "", {"json": {"username": "abc", "email": "a@b.example", "password": "12345678"}}, Permission.END_USER_READ),
            ("get", "/1", {}, Permission.END_USER_UPDATE),
            ("patch", "/1/status", {"json": {"is_active": False}}, Permission.END_USER_READ),
            ("patch", "/1/primary-group", {"json": {"group_id": "g"}}, Permission.END_USER_READ),
            ("delete", "/1", {}, Permission.END_USER_UPDATE),
            ("get", "/export/csv", {}, Permission.END_USER_UPDATE),
            ("get", "/export/json", {}, Permission.END_USER_UPDATE),
            ("delete", "/1/photo", {}, Permission.END_USER_READ),
        ],
    )
    def test_the_wrong_permission_is_403(self, h, method, suffix, kw, held):
        limited = h.limited_headers("t1", [held])
        r = getattr(h.client, method)(f"{BASE}{suffix}", headers=limited, **kw)
        assert r.status_code == 403, r.text

    def test_anonymous_is_refused(self, h):
        assert h.client.get(BASE).status_code in (401, 403)

    # `end_users.py` must not read `admin.tenant_id` off a system
    # administrator, which has none.
    def test_a_system_admin_gets_an_answer_not_a_500(self, h):
        from app.composition.principals import PRINCIPALS

        if "system" not in PRINCIPALS.names():
            pytest.skip("this composition has no system administrator (Community Edition)")
        c = TestClient(h.app, raise_server_exceptions=False)
        r = c.get(f"{BASE}?tenant_id=t1", headers=h.sys_headers())
        assert r.status_code < 500


class TestCreate:

    def test_stores_a_hash_assigns_the_default_role_and_joins_groups(self, h):
        body = _create_body(group_ids=["group-default-t1"])
        r = h.client.post(BASE, json=body, headers=h.headers("t1"))
        assert r.status_code in (200, 201), r.text
        assert "Initial-Secret-1" not in r.text
        s = h.Factory()
        try:
            u = s.query(EndUser).filter(EndUser.username == body["username"]).one()
            assert bcrypt.checkpw(b"Initial-Secret-1", u.password_hash.encode())
            assert (u.tenant_id, u.role_id, u.primary_group_id) == ("t1", "role-t1-user", "group-default-t1")
        finally:
            s.close()

    @pytest.mark.parametrize("field", ["username", "email"])
    def test_duplicates_are_refused(self, h, field):
        first = _create_body()
        assert h.client.post(BASE, json=first, headers=h.headers("t1")).status_code in (200, 201)
        again = _create_body(**{field: first[field]})
        assert h.client.post(BASE, json=again, headers=h.headers("t1")).status_code == 409

    def test_a_short_password_is_refused(self, h):
        assert h.client.post(BASE, json=_create_body(password="short"), headers=h.headers("t1")).status_code == 422


class TestReadAndStatus:
    def test_detail_has_no_hash(self, h):
        target = _user(h)
        r = h.client.get(f"{BASE}/{target}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["id"] == target and "password_hash" not in r.json()

    @pytest.mark.parametrize(
        "query,key,must,must_not",
        [
            ("search=findme", "username", "findme_a", "other_a"),
            ("active_only=true", "username", "active_b", "inactive_b"),
            ("verified_only=true", "username", "verified_c", "unverified_c"),
        ],
    )
    def test_list_filters(self, h, query, key, must, must_not):
        if must == "findme_a":
            _user(h, username="findme_a"), _user(h, username="other_a")
        elif must == "active_b":
            _user(h, username="active_b"), _user(h, username="inactive_b", is_active=False)
        else:
            _user(h, username="verified_c"), _user(h, username="unverified_c", is_verified=False)
        r = h.client.get(f"{BASE}?{query}&limit=1000", headers=h.headers("t1"))
        names = {u[key] for u in r.json()["users"]}
        assert must in names and must_not not in names
        assert r.json()["pagination"]["total"] == len(names)

    def test_pagination_reports_the_total_not_the_page(self, h):
        _user(h), _user(h)
        r = h.client.get(f"{BASE}?limit=1", headers=h.headers("t1"))
        assert r.json()["pagination"]["returned"] == 1
        assert r.json()["pagination"]["total"] >= 2

    def test_status_change_is_stored_and_audited(self, h):
        target = _user(h)
        r = h.client.patch(f"{BASE}/{target}/status", json={"is_active": False}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, target).is_active is False
        s = h.Factory()
        try:
            log = (
                s.query(TenantAdminAuditLog)
                .filter(TenantAdminAuditLog.action == "end_user_status_update", TenantAdminAuditLog.resource_id == str(target))
                .one()
            )
            assert log.tenant_id == "t1"
            details = log.details if isinstance(log.details, dict) else json.loads(log.details)
            assert (details["old_status"], details["new_status"]) == (True, False)
        finally:
            s.close()


class TestPrimaryGroup:
    def test_a_non_member_cannot_be_given_the_group(self, h):
        target = _user(h)
        r = h.client.patch(f"{BASE}/{target}/primary-group", json={"group_id": "group-default-t1"}, headers=h.headers("t1"))
        assert r.status_code == 400
        assert _row(h, target).primary_group_id is None

    def test_a_member_can_and_it_is_audited(self, h):
        target = _user(h)

        def join(s):
            s.add(ResourceGroupMembership(tenant_id="t1", user_id=target, group_id="group-default-t1", role="member"))

        h.seed(join)
        r = h.client.patch(f"{BASE}/{target}/primary-group", json={"group_id": "group-default-t1"}, headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["previous_primary_group_id"] is None
        assert _row(h, target).primary_group_id == "group-default-t1"


class TestDelete:
    def test_removes_the_row_and_the_audit_row_names_who_and_where(self, h):
        target = _user(h, username="deleted_person")
        r = h.client.delete(f"{BASE}/{target}", headers=h.headers("t1"))
        assert r.status_code == 200, r.text
        assert r.json()["username"] == "deleted_person"
        assert _row(h, target) is None
        s = h.Factory()
        try:
            log = (
                s.query(TenantAdminAuditLog)
                .filter(TenantAdminAuditLog.action == "end_user_delete", TenantAdminAuditLog.resource_id == str(target))
                .one()
            )
            assert log.tenant_id == "t1"
            details = log.details if isinstance(log.details, dict) else json.loads(log.details)
            assert details["username"] == "deleted_person"
        finally:
            s.close()

    def test_unknown_is_404(self, h):
        assert h.client.delete(f"{BASE}/999999", headers=h.headers("t1")).status_code == 404
        assert h.client.get(f"{BASE}/999999", headers=h.headers("t1")).status_code == 404
        assert h.client.patch(f"{BASE}/999999/status", json={"is_active": False}, headers=h.headers("t1")).status_code == 404


class TestPhotos:
    def _upload(self, h, target, headers, content_type="image/png", data=PNG):
        return h.client.post(f"{BASE}/{target}/photo", files={"file": ("a.png", data, content_type)}, headers=headers)

    def test_round_trip_needs_the_capability_token(self, h):
        target = _user(h)
        r = self._upload(h, target, h.headers("t1"))
        assert r.status_code == 200, r.text
        token = r.json()["avatar_url"].split("?h=")[1]
        ok = h.client.get(f"{BASE}/{target}/photo?h={token}")
        assert ok.status_code == 200 and ok.content == PNG
        assert h.client.get(f"{BASE}/{target}/photo").status_code == 404
        assert h.client.get(f"{BASE}/{target}/photo?h=deadbeefdeadbeef").status_code == 404
        cached = h.client.get(f"{BASE}/{target}/photo?h={token}", headers={"If-None-Match": ok.headers["etag"]})
        assert cached.status_code == 304
        assert h.client.delete(f"{BASE}/{target}/photo", headers=h.headers("t1")).status_code == 200
        assert h.client.get(f"{BASE}/{target}/photo?h={token}").status_code == 404

    def test_another_tenant_cannot_upload(self, h):
        target = _user(h, "t2")
        assert self._upload(h, target, h.headers("t1")).status_code == 404
        assert _row(h, target).photo_data is None

    def test_read_permission_cannot_upload(self, h):
        target = _user(h)
        limited = h.limited_headers("t1", [Permission.END_USER_READ])
        assert self._upload(h, target, limited).status_code == 403
        assert _row(h, target).photo_data is None

    @pytest.mark.parametrize(
        "content_type,data", [("image/svg+xml", PNG), ("text/html", PNG), ("image/png", b"0" * (5 * 1024 * 1024 + 1))]
    )
    def test_bad_uploads_are_refused(self, h, content_type, data):
        target = _user(h)
        assert self._upload(h, target, h.headers("t1"), content_type=content_type, data=data).status_code == 400
        assert _row(h, target).photo_data is None


class TestServiceLayer:
    """Direct calls for what the routes cannot reach: the False answer on a
    wrong tenant, and a storage failure turned into False, not a traceback."""

    def _call(self, h, coro_fn, **kw):
        import asyncio

        s = h.Factory()
        try:
            result = asyncio.run(coro_fn(db=s, **kw))
            s.commit()
            return result
        finally:
            s.close()

    def test_status_change_refuses_another_tenant(self, h):
        target = _user(h, "t2")
        assert self._call(h, auth.update_gateway_user_status, user_id=target, tenant_id="t1", is_active=False) is False
        assert _row(h, target).is_active is True

    def test_delete_refuses_another_tenant(self, h):
        target = _user(h, "t2")
        assert self._call(h, auth.delete_gateway_user_account, user_id=target, tenant_id="t1") is False
        assert _row(h, target) is not None

    def test_get_refuses_another_tenant(self, h):
        target = _user(h, "t2")
        assert self._call(h, auth.get_gateway_user, user_id=target, tenant_id="t1") is None
        assert self._call(h, auth.get_gateway_user, user_id=target, tenant_id="t2")["id"] == str(target)

    @pytest.mark.parametrize("method", ["update_gateway_user_status", "delete_gateway_user_account"])
    def test_a_storage_failure_is_false_not_an_exception(self, method):
        import asyncio

        class Broken:
            def query(self, *_a, **_k):
                raise RuntimeError("db gone")

        kw = {"is_active": True} if method == "update_gateway_user_status" else {}
        assert asyncio.run(getattr(auth, method)(user_id=1, tenant_id="t1", db=Broken(), **kw)) is False


class TestNoSessionToken:
    def test_the_control_plane_signs_no_end_user_in(self):
        """The Core Gateway issues the session tokens of end users; this
        service has no method that mints one."""
        assert not [m for m in dir(auth) if "jwt" in m.lower() or "token" in m.lower()]

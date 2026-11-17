# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`/v1/admin/tenants` is two routers: the installation's
own tenant (`tenant_settings`, Community) and tenant management
(`tenant_management`, Enterprise, feature `multi_tenant`).

The first three classes are the recorder: written against the unsplit
`tenants.py`, where they passed, and re-pointed here. What moved:

* a tenant administrator's `PATCH /{own id}` is `PATCH /current`, and its
  `GET /{own id}/theme` and `/theme-variants` are `GET /current/theme` and
  `/current/theme-variants`. The by-id forms now answer a system administrator
  only (403 for a tenant administrator, where its own id answered 200);
* a field reserved to tenant management was refused with 403 by a list in the
  handler; it is now not in the model of `PATCH /current`, which forbids what
  it does not declare (422). Written nowhere, before and after;
* a column no model declares (`is_active`, `suspended`, ...) and `theme` were
  accepted and dropped (200, nothing written); they are refused too (422);
* the three new `/current` routes refuse a system administrator (403): it has
  no tenant of its own. `GET /current` keeps its placeholder for one.

The rest is the contract of the split: every tenant-management route answers a
system administrator only, the Community composition has none of them, and
`current` is never read as a tenant id.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app.api.v1.endpoints.admin import tenant_settings
from app.api.v1.endpoints.admin.tenant_settings import create_tenant_settings_router
from app.api.v1.models.admin.tenant_settings import TenantSettingsUpdate
from app.composition.features import MULTI_TENANT
from app.composition.routers import ROUTERS, mount_routers
from app.core.admin_user.dependencies import get_current_admin, require_system_admin
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.tenant import Tenant
from tests.admin_endpoint_harness import build_admin_harness

BASE = "/v1/admin/tenants"

NEW_TENANT = {"name": "rec-new", "display_name": "Rec New", "primary_contact_email": "a@b.example"}
REASON = {"reason": "recorded by the split recorder"}
THEME = {"theme": {"primary_color": "#010203"}}

# (method, path, body, anonymous, tenant admin of t1, system admin), in an order
# the system administrator's column can run in.
SETTINGS = [
    ("get", "/current", None, 401, 200, 200),
    ("patch", "/current", {"display_name": "Recorded"}, 401, 200, 403),
    ("get", "/current/theme", None, 401, 200, 403),
    ("get", "/current/theme-variants", None, 401, 200, 403),
]
MANAGEMENT = [
    ("get", "", None, 401, 403, 200),
    ("get", "/t2", None, 401, 403, 200),
    ("get", "/t1", None, 401, 403, 200),
    ("post", "", NEW_TENANT, 401, 403, 201),
    ("patch", "/t1", {"display_name": "Recorded"}, 401, 403, 200),
    ("patch", "/t2", {"display_name": "Recorded"}, 401, 403, 200),
    ("post", "/t2/suspend", REASON, 401, 403, 200),
    ("post", "/t2/unsuspend", None, 401, 403, 200),
    ("post", "/t1/suspend", REASON, 401, 403, 200),
    ("post", "/t1/unsuspend", None, 401, 403, 200),
    ("get", "/t1/stats", None, 401, 403, 200),
    ("get", "/t2/stats", None, 401, 403, 200),
    ("get", "/stats/summary", None, 401, 403, 200),
    ("get", "/t1/theme", None, 401, 403, 200),
    ("get", "/t2/theme", None, 401, 403, 200),
    ("get", "/t1/theme-variants", None, 401, 403, 200),
    ("get", "/t2/theme-variants", None, 401, 403, 200),
    ("patch", "/t1/theme", THEME, 401, 403, 200),
    ("patch", "/t2/theme", THEME, 401, 403, 200),
    ("put", "/t1/theme-variants", {"company_name": "Recorded"}, 401, 403, 200),
    ("put", "/t2/theme-variants", {"company_name": "Recorded"}, 401, 403, 200),
    ("delete", "/t1/theme", None, 401, 403, 200),
    ("delete", "/t2/theme", None, 401, 403, 200),
    ("get", "/t1/theme/preview", None, 401, 403, 200),
    ("get", "/t2/theme/preview", None, 401, 403, 200),
    ("delete", "/doomed?force=true", None, 401, 403, 200),
]
MATRIX = SETTINGS + MANAGEMENT

# What a tenant administrator may change on its own tenant, with a value to send.
OWN_TENANT_FIELDS = {
    "display_name": "Renamed Co",
    "organization_name": "Renamed Organisation",
    "domain": "recorder.example",
    "primary_contact_email": "ops@recorder.example",
    "primary_contact_name": "Rec Order",
    "settings": {"recorded": 1},
    # `optional` is the one value an administrator without 2FA may write
    # (and the stored default); the other two, the 409 and what tightening
    # does are in test_mfa_policy_enforcement.py.
    "mfa_policy": "optional",
    "data_residency": {"allowed_regions": ["eu"], "block_global": True},
    "data_retention": {"store_bodies": "full", "body_days": None, "log_days": None, "run_days": None},
    "default_embedding_model_id": None,
    "default_judge_model_id": None,
    "assurance_capture_interval_hours": 24,
    "default_approval_window_seconds": 600,
    "ai_system_gate_policy": None,
    # Set and cleared together (test_tenant_google_drive_picker.py): alone,
    # each can only be written as it is (clear).
    "portal_google_drive_client_id": None,
    "portal_google_drive_api_key": None,
}
# Reserved to tenant management: in `TenantUpdate`, not in `TenantSettingsUpdate`.
MANAGEMENT_FIELDS = {
    "theme": {"primary_color": "#112233"},
    "max_admin_users": 5,
    "max_end_users": 5,
    "max_end_user_groups": 5,
    "max_llm_models": 5,
    "max_mcp_servers": 5,
    "max_mcp_registries": 5,
    "max_kb_collections": 5,
    "max_agent_skills": 5,
    "max_agent_cards": 5,
    "max_agent_identities": 5,
    "media_storage_quota_bytes": 5_000_000,
    "features": {"mcp": True},
    "is_trial": True,
    "trial_days": 14,
    "billing_plan": "enterprise",
    "billing_email": "billing@recorder.example",
    "billing_metadata": {"recorded": 1},
}
# Columns of the tenant that no update model declares.
# `Tenant` has no `theme` column either: tenant management's PATCH accepts the
# key and writes nothing (found by the recorder; not changed by the split).
NOT_A_COLUMN = {"theme", "trial_days"}
UNDECLARED = {
    "is_active": False,
    "suspended": True,
    "is_default": True,
    "name": "renamed",
    "id": "t9",
}


@pytest.fixture(scope="module")
def h():
    """The Community router alone. The Enterprise half runs the same tests on
    a harness that mounts tenant management beside it."""
    harness = build_admin_harness(
        [create_tenant_settings_router(BrutorLogger("test-tenant-split"))],
        logger_name="test-tenant-split",
    )
    yield harness
    harness.close()


def _row(h, tenant_id: str) -> dict:
    s = h.Factory()
    try:
        t = s.get(Tenant, tenant_id)
        # `updated_at` moves with every accepted update, `last_active_at` with a login.
        skip = ("updated_at", "last_active_at")
        return {c.name: getattr(t, c.name) for c in Tenant.__table__.columns if c.name not in skip}
    finally:
        s.close()


def _call(h, method, path, body, headers=None):
    kw = {"json": body} if body is not None else {}
    if headers:
        kw["headers"] = headers
    return getattr(h.client, method)(f"{BASE}{path}", **kw)


class TestRouteByPrincipal:
    """The settings routes, by principal. The Enterprise half runs the whole
    matrix (tenant management and the system administrator's column)."""

    @pytest.mark.parametrize("method,path,body,anonymous,_t,_s", SETTINGS)
    def test_anonymous(self, h, method, path, body, anonymous, _t, _s):
        before = (_row(h, "t1"), _row(h, "t2"))
        assert _call(h, method, path, body).status_code == anonymous
        assert (_row(h, "t1"), _row(h, "t2")) == before

    @pytest.mark.parametrize("method,path,body,_a,tenant_admin,_s", SETTINGS)
    def test_tenant_admin(self, h, method, path, body, _a, tenant_admin, _s):
        before = (_row(h, "t1"), _row(h, "t2"))
        r = _call(h, method, path, body, h.headers("t1"))
        assert r.status_code == tenant_admin, r.text
        if tenant_admin != 200:
            assert (_row(h, "t1"), _row(h, "t2")) == before
        else:
            assert _row(h, "t2") == before[1]


class TestCurrentIsTheCallersTenant:
    @pytest.mark.parametrize("tenant_id", ["t1", "t2"])
    def test_current_reads_the_callers_tenant(self, h, tenant_id):
        r = _call(h, "get", "/current", None, h.headers(tenant_id))
        assert r.status_code == 200, r.text
        assert r.json()["id"] == tenant_id

    @pytest.mark.parametrize("tenant_id,other", [("t1", "t2"), ("t2", "t1")])
    def test_own_theme_reads_answer_for_the_callers_tenant(self, h, tenant_id, other):
        for path in ("theme", "theme-variants"):
            r = _call(h, "get", f"/current/{path}", None, h.headers(tenant_id))
            assert r.status_code == 200, r.text
            assert r.json()["tenant_id"] == tenant_id

    @pytest.mark.parametrize("tenant_id,other", [("t1", "t2"), ("t2", "t1")])
    def test_an_update_changes_the_callers_tenant_only(self, h, tenant_id, other):
        before = _row(h, other)
        r = _call(h, "patch", "/current", {"primary_contact_name": f"Owner of {tenant_id}"}, h.headers(tenant_id))
        assert r.status_code == 200, r.text
        assert r.json()["id"] == tenant_id
        assert _row(h, tenant_id)["primary_contact_name"] == f"Owner of {tenant_id}"
        assert _row(h, other) == before


class TestFieldsATenantAdminMayChange:

    def test_the_settings_model_forbids_what_it_does_not_declare(self):
        assert TenantSettingsUpdate.model_config.get("extra") == "forbid"


# ---------------------------------------------------------------------------
# The contract of the split.
# ---------------------------------------------------------------------------

    @pytest.mark.parametrize("field", sorted(OWN_TENANT_FIELDS))
    def test_own_tenant_field_is_accepted_and_written(self, h, field):
        value = OWN_TENANT_FIELDS[field]
        other = _row(h, "t2")
        r = _call(h, "patch", "/current", {field: value}, h.headers("t1"))
        assert r.status_code == 200, r.text
        assert _row(h, "t1")[field] == value
        assert _row(h, "t2") == other

    @pytest.mark.parametrize("field", sorted(MANAGEMENT_FIELDS))
    def test_management_field_is_refused_and_not_written(self, h, field):
        before = _row(h, "t1")
        r = _call(h, "patch", "/current", {field: MANAGEMENT_FIELDS[field]}, h.headers("t1"))
        assert r.status_code == 422, r.text
        assert _row(h, "t1") == before

    @pytest.mark.parametrize("field", sorted(MANAGEMENT_FIELDS))
    def test_management_field_beside_an_allowed_one_writes_neither(self, h, field):
        before = _row(h, "t1")
        body = {"display_name": "Smuggled", field: MANAGEMENT_FIELDS[field]}
        r = _call(h, "patch", "/current", body, h.headers("t1"))
        assert r.status_code == 422, r.text
        assert _row(h, "t1") == before

    def test_a_limited_admin_needs_the_permission_for_every_field(self, h):
        limited = h.limited_headers("t1", [Permission.ROLES_READ])
        before = _row(h, "t1")
        for field, value in OWN_TENANT_FIELDS.items():
            r = _call(h, "patch", "/current", {field: value}, limited)
            assert r.status_code == 403, f"{field}: {r.status_code} {r.text}"
        assert _row(h, "t1") == before


def _routes(router):
    return [(method, route) for route in router.routes for method in sorted(route.methods)]


def _dependency_calls(dependant) -> set:
    found = set()
    for sub in dependant.dependencies:
        found.add(sub.call)
        found |= _dependency_calls(sub)
    return found


class TestTheSettingsRouterHasNoManagement:
    ROUTER = create_tenant_settings_router(BrutorLogger("test-tenant-split"))

    def test_its_routes_are_the_four_under_current(self):
        assert sorted((m, r.path_format) for m, r in _routes(self.ROUTER)) == [
            ("GET", "/admin/tenants/current"),
            ("GET", "/admin/tenants/current/theme"),
            ("GET", "/admin/tenants/current/theme-variants"),
            ("PATCH", "/admin/tenants/current"),
        ]

    def test_no_route_takes_a_tenant_id(self):
        """Nothing to compare with the caller's tenant means nothing to get wrong."""
        for _, route in _routes(self.ROUTER):
            assert route.param_convertors == {}, route.path_format
            assert "tenant_id" not in {p.name for p in route.dependant.query_params}, route.path_format

    def test_every_route_authenticates(self):
        for _, route in _routes(self.ROUTER):
            assert get_current_admin in _dependency_calls(route.dependant), route.path_format

    def test_only_reads_are_open_without_a_permission(self):
        for method, route in _routes(self.ROUTER):
            if method != "GET":
                names = {getattr(c, "__qualname__", "") for c in _dependency_calls(route.dependant)}
                assert "require_permission.<locals>.permission_dependency" in names, route.path_format


class TestEditions:
    """The registry files the two routers on the two sides, and a composition
    without the Enterprise routers has no tenant management."""


    @pytest.fixture(scope="class")
    def community(self):
        """The Community composition: the registry without the Enterprise routers."""
        from app.composition.services import build_services
        from app.core.logging_system import setup_logging

        app = FastAPI()
        specs = [spec for spec in ROUTERS if spec.feature is None]
        mount_routers(app, build_services(setup_logging("INFO", use_json=False)), specs)
        return app

    def test_the_community_composition_has_the_settings_routes_only(self, community):
        under = sorted(
            (method, context.path)
            for context in iter_route_contexts(community.router.routes)
            if context.methods and (context.path == BASE or context.path.startswith(BASE + "/"))
            for method in context.methods
        )
        assert under == [
            ("GET", f"{BASE}/current"),
            ("GET", f"{BASE}/current/theme"),
            ("GET", f"{BASE}/current/theme-variants"),
            ("PATCH", f"{BASE}/current"),
        ]
        assert sorted(p for p in community.openapi()["paths"] if p.startswith(BASE)) == [
            f"{BASE}/current", f"{BASE}/current/theme", f"{BASE}/current/theme-variants",
        ]



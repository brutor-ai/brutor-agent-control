# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A caller that is neither a tenant administrator nor the system administrator
reaches no tenant.

Route modules decided their tenant with private copies of "a tenant
administrator is pinned to its tenant; anything else is the system
administrator and gets the tenant it names" — or, in the listings of the
admin-users router, "anything else sees every tenant". A third principal
(`tests/unknown_principal.py`: registered, every permission, no tenant, its
`resolve_tenant` refuses) is the caller those copies were wrong about.

Two parts:

* `CONVERTED`: the routes whose copies now ask the principal
  (`resolve_tenant_id`, `tenant_scope`, `may_act_on_tenant` in
  `app/core/admin_user/dependencies.py`). The third principal gets 403 on
  each, naming a tenant or not.
* the ratchet: every other admin route that does not refuse it, recorded in
  `fixtures/unknown_principal_let_past.json`. The list may only shrink: a new
  route that lets an unknown kind of caller past fails here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.admin_endpoint_harness import JWT_SECRET, build_admin_harness
from tests.principal_matrix import admin_routes, key, sweep
from tests.unknown_principal import robot_admin, robot_headers, robot_registered

RECORDING = Path(__file__).parent / "fixtures" / "unknown_principal_let_past.json"

CONVERTED = [
    "GET /v1/admin/users/audit-logs",
    "GET /v1/admin/users/audit-logs/export",
    "POST /v1/admin/users/audit-logs/facets",
    "POST /v1/admin/users/audit-logs/search",
    "GET /v1/admin/users/roles",
    "GET /v1/admin/users/roles/{role_id}",
    "PATCH /v1/admin/users/roles/{role_id}",
    "DELETE /v1/admin/users/roles/{role_id}",
    "GET /v1/admin/users/tenant/sessions",
    "POST /v1/admin/proxy-logs/cleanup",
    "POST /v1/admin/proxy-logs/export",
    "POST /v1/admin/proxy-logs/facets",
    "POST /v1/admin/proxy-logs/query",
    "GET /v1/admin/proxy-logs/statistics",
    "GET /v1/admin/proxy-logs/thread/{correlation_id}",
    "GET /v1/admin/proxy-logs/traces",
    "GET /v1/admin/proxy-logs/traces/{root_task_id}",
    "GET /v1/admin/proxy-logs/user/{user_id}/activity",
    "GET /v1/admin/agent-registry",
    "GET /v1/admin/evidence/logs",
    "GET /v1/admin/mcp-servers",
    # (the three prompt-cache routes: enterprise/tests, since L4)
    "GET /v1/admin/saved-log-views/{view_id}",
    "PATCH /v1/admin/saved-log-views/{view_id}",
    "DELETE /v1/admin/saved-log-views/{view_id}",
]


@pytest.fixture(scope="module")
def swept():
    from tests.app_factory import create_app

    real = create_app()
    h = build_admin_harness([], logger_name="test-unknown-principal")
    h.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = h.app.dependency_overrides
    routes = admin_routes(real)
    with robot_registered():
        result = sweep(
            h.app, routes,
            {"anonymous": None, "tenant": None, "tenant_other": None,
             "system": lambda: robot_headers(JWT_SECRET), "system_named": lambda: robot_headers(JWT_SECRET)},
        )
    h.close()
    return routes, result


@pytest.mark.parametrize("route", CONVERTED)
def test_a_converted_route_refuses_the_third_principal(swept, route):
    _routes, result = swept
    assert result[route] == {"system": 403, "system_named": 403}


def test_the_recording_is_not_stale(swept):
    """A route that now refuses is taken off the list, so it cannot come back."""
    _routes, result = swept
    recorded = json.loads(RECORDING.read_text())
    refused_now = sorted(r for r in recorded if r in result and not (set(result[r].values()) - {401, 403}))
    assert refused_now == [], f"record again (BRUTOR_RECORD_UNKNOWN_PRINCIPAL=1): {refused_now[:20]}"


# =============================================================================
# The three questions route modules ask
# =============================================================================

class TestTheSharedResolvers:
    tenant = SimpleNamespace(tenant_id="t1")
    system = SimpleNamespace()


    @pytest.mark.parametrize("admin_type", ["robot", None, "System", "TENANT", " tenant", 7])
    def test_tenant_scope_refuses_a_type_nobody_registered(self, admin_type):
        from app.core.admin_user.dependencies import tenant_scope

        for admin in (self.tenant, self.system):
            for named in (None, "t1"):
                with pytest.raises(HTTPException) as e:
                    tenant_scope(admin, admin_type, named)
                assert e.value.status_code == 403

    def test_tenant_scope_refuses_a_tenant_administrator_naming_another_tenant(self):
        from app.core.admin_user.dependencies import tenant_scope

        with pytest.raises(HTTPException) as e:
            tenant_scope(self.tenant, "tenant", "t2")
        assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e:
            tenant_scope(SimpleNamespace(tenant_id=None), "tenant")
        assert e.value.status_code == 403

    def test_tenant_scope_refuses_a_registered_principal_that_acts_on_no_tenant(self):
        from app.core.admin_user.dependencies import tenant_scope

        with robot_registered():
            for named in (None, "t1"):
                with pytest.raises(HTTPException) as e:
                    tenant_scope(robot_admin(), "robot", named)
                assert e.value.status_code == 403


# =============================================================================
# What the sweep's empty body and `nope` ids do not reach
# =============================================================================

@pytest.fixture(scope="module")
def live():
    """The real routes on the harness, with a system administrator's platform
    witness, a tenant witness, a tenant administrator and a session in it."""
    from tests.app_factory import create_app

    real = create_app()
    h = build_admin_harness([], logger_name="test-unknown-principal-live")
    h.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = h.app.dependency_overrides
    h.client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(h.app, raise_server_exceptions=False)
    witness = {"kind": "rfc3161", "url": "https://tsa.example/x", "operator_label": "C7"}
    r = h.client.post("/v1/admin/evidence/witnesses", json=dict(witness, scope="platform"), headers=h.sys_headers())
    assert r.status_code == 201, r.text
    h.platform_witness = r.json()["id"]
    r = h.client.post("/v1/admin/evidence/witnesses", json=dict(witness, scope="tenant", url="https://tsa.example/t"),
                      headers=h.headers("t1"))
    assert r.status_code == 201, r.text
    h.tenant_witness = r.json()["id"]
    h.witness = witness
    r = h.client.get("/v1/admin/users/tenant", headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    body = r.json()
    admins = body if isinstance(body, list) else body.get("admins") or body.get("items")
    h.t1_admin = admins[0]["id"]
    r = h.client.get("/v1/admin/users/tenant/sessions", headers=h.headers("t1"))
    assert r.status_code == 200, r.text
    body = r.json()
    sessions = body if isinstance(body, list) else body.get("sessions") or body.get("items")
    h.t1_session = sessions[0]["id"]
    yield h
    h.close()



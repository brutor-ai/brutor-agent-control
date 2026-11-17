# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Class guard over every admin route the real app mounts: anonymous is refused, and
an admin holding no permission is refused unless the route is on the
reviewed list below. The list is read from `create_app()`, so a route added without
a permission check fails here the day it lands.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.admin_endpoint_harness import build_admin_harness

# An administrator who signed in and holds nothing (every permission guards something).
NO_PERMISSIONS: tuple = ()
# An administrator who holds a permission, but one no route asks for: a route that
# only asks "holds any permission" is refused by the test that uses it.
UNRELATED = SimpleNamespace(value="sweep:unrelated")
PREFIXES = ("/v1/admin/", "/v1/admin/users/")


def _build():
    from tests.app_factory import create_app

    app = create_app()
    routes = [
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith(PREFIXES)
        for method in item
    ]
    return app, routes


REAL_APP, ROUTES = _build()


def _url(path: str) -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", "nope", path)


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([], logger_name="test-auth-sweep")
    # The real routes, on the harness's bare app: no middleware, in-memory DB.
    harness.app.router.routes.extend(REAL_APP.router.routes)
    REAL_APP.dependency_overrides = harness.app.dependency_overrides
    yield harness
    harness.close()


# Answers an anonymous caller, by design.
PUBLIC = {
    # Credentials are what these take.
    ("POST", "/v1/admin/users/login/2fa"),
    # A `2fa_enrollment` token is what these take: the password step hands it
    # out instead of a session when the tenant's policy requires 2FA.
    # `test_mfa_policy_enforcement.py` proves nothing else opens them.
    ("POST", "/v1/admin/users/login/2fa/enroll/setup"),
    ("POST", "/v1/admin/users/login/2fa/enroll/verify"),
    ("POST", "/v1/admin/users/system/login"),
    ("POST", "/v1/admin/users/tenant/login"),
    # Image bytes behind a capability token, or an icon by id.
    ("GET", "/v1/admin/users/tenant/{admin_id}/photo"),
    ("GET", "/v1/admin/end-users/{user_id}/photo"),
    ("GET", "/v1/admin/mcp-registries/instance/servers/{server_id}/icon"),
    ("GET", "/v1/admin/mcp-servers/{server_id}/icon"),
    ("GET", "/v1/admin/providers/{provider_id}/icon"),
    # Static capability metadata about a provider; no tenant data.
    ("GET", "/v1/admin/prompt-cache/models/{provider}/capability"),
    # Liveness.
    ("GET", "/v1/admin/proxy-logs/health"),
}

# Answers any signed-in admin, whatever its role: the caller's own account, or
# reference data with no tenant content. Everything else needs a permission.
ANY_ADMIN = {
    ("GET", "/v1/admin/users/me"),
    ("GET", "/v1/admin/users/me/2fa/status"),
    ("POST", "/v1/admin/users/me/2fa/disable"),
    ("POST", "/v1/admin/users/me/2fa/setup"),
    ("POST", "/v1/admin/users/me/2fa/verify"),
    ("POST", "/v1/admin/users/me/change-password"),
    ("POST", "/v1/admin/users/logout"),
    # Own photo needs nothing; another admin's needs ADMIN_USER_UPDATE (checked
    # inside, see test_admin_rbac_tenant_admins_http.py).
    ("POST", "/v1/admin/users/tenant/{admin_id}/photo"),
    ("DELETE", "/v1/admin/users/tenant/{admin_id}/photo"),
    # The caller's own tenant, for the console's frame, and the theme it draws
    # itself in.
    ("GET", "/v1/admin/tenants/current"),
    ("GET", "/v1/admin/tenants/current/theme"),
    ("GET", "/v1/admin/tenants/current/theme-variants"),
    ("GET", "/v1/admin/deployments/capabilities"),
    # What the licence grants the caller's tenant and how much of it is
    # used (Enterprise): the console draws its frame from it, as from
    # the tenant above. A tenant administrator sees its own tenant only.
    ("GET", "/v1/admin/licence"),
    ("GET", "/v1/admin/mission-control/explainers"),
    ("GET", "/v1/admin/standards"),
    ("GET", "/v1/admin/standards/{slug}/schema"),
    ("GET", "/v1/admin/compliance/obligation-frameworks"),
}


def test_the_sweep_found_the_app():
    assert len(ROUTES) > 400


GUARDED = sorted(set(ROUTES) - PUBLIC)


@pytest.mark.parametrize("method,path", GUARDED)
def test_anonymous_is_refused(h, method, path):
    client = TestClient(h.app, raise_server_exceptions=False)
    r = client.request(method, _url(path), json={})
    assert r.status_code in (401, 403), f"{method} {path}: {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("method,path", sorted(set(ROUTES) - PUBLIC - ANY_ADMIN))
def test_an_admin_without_the_permission_is_refused(h, method, path):
    client = TestClient(h.app, raise_server_exceptions=False)
    r = client.request(method, _url(path), json={}, headers=h.limited_headers("t1", NO_PERMISSIONS, username="holds_nothing"))
    # A body refused before the permission check also reached nothing, but it is
    # not proof of the check, so only the few routes that do it that way pass 422.
    assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("method,path", sorted(set(ROUTES) - PUBLIC - ANY_ADMIN))
def test_an_admin_with_an_unrelated_permission_is_refused(h, method, path):
    client = TestClient(h.app, raise_server_exceptions=False)
    r = client.request(method, _url(path), json={}, headers=h.limited_headers("t1", [UNRELATED], username="holds_unrelated"))
    # A body refused before the permission check also reached nothing, but it is
    # not proof of the check, so only the few routes that do it that way pass 422.
    assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text[:200]}"

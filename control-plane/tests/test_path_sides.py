# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Three surfaces filed on the right side of the edition boundary, with no
path renamed.

* **Provider icons** (`/v1/admin/providers/{id}/icon`) are a Community
  concern and live in the Community providers router. Nothing moves; held
  here so that it stays so.
* **Router cache configuration** (`GET` / `PATCH /v1/admin/llm-router/cache-config`)
  configures the LLM router's exact-match cache, which is part of the
  Enterprise response cache. It is served by a router of its own, tagged
  `response_cache`, on the same paths; the Community routing-groups router
  does not carry it.
* **`/v1/admin/notifications`** is the assurance notification channels
  (Community) alone. The portal inbox's send / sent (Enterprise, `portal`)
  are served under `/v1/admin/user-notifications`.

Two halves: the recorder (what the composition with
everything answers, written on the code before the change, unchanged by it)
and the Community contract.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app.composition.features import PORTAL, RESPONSE_CACHE
from app.composition.routers import ROUTERS
from app.core.admin_user.permissions import Permission
from app.database import get_db
from tests.admin_endpoint_harness import build_admin_harness
from tests.principal_matrix import community_app, community_principals, no_network

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS = ROOT / "app" / "api" / "v1" / "endpoints" / "admin"

ICON_OPS = {
    ("GET", "/v1/admin/providers/{provider_id}/icon"),
    ("POST", "/v1/admin/providers/{provider_id}/icon"),
    ("DELETE", "/v1/admin/providers/{provider_id}/icon"),
}
CACHE_CONFIG_OPS = {
    ("GET", "/v1/admin/llm-router/cache-config"),
    ("PATCH", "/v1/admin/llm-router/cache-config"),
}
PORTAL_INBOX_OPS = {
    ("POST", "/v1/admin/user-notifications/send"),
    ("GET", "/v1/admin/user-notifications/sent"),
}
ASSURANCE_NOTIFICATION_OPS = {
    ("GET", "/v1/admin/notifications/channels"),
    ("POST", "/v1/admin/notifications/channels"),
    ("PATCH", "/v1/admin/notifications/channels/{channel_id}"),
    ("DELETE", "/v1/admin/notifications/channels/{channel_id}"),
    ("POST", "/v1/admin/notifications/channels/{channel_id}/test"),
    ("GET", "/v1/admin/notifications/subscriptions"),
    ("POST", "/v1/admin/notifications/subscriptions"),
    ("PATCH", "/v1/admin/notifications/subscriptions/{subscription_id}"),
    ("DELETE", "/v1/admin/notifications/subscriptions/{subscription_id}"),
    ("GET", "/v1/admin/notifications/deliveries"),
}


def operations(app, prefix=""):
    return {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith(prefix)
        for method in item
    }


def owners(app, ops):
    """`{operation: module of the handler}` for the mounted routes in `ops`."""
    out = {}
    for context in iter_route_contexts(app.router.routes):
        for method in context.methods or ():
            if (method, context.path) in ops:
                out[(method, context.path)] = (context.endpoint or context.original_route.endpoint).__module__
    return out


def mounted_modules(app):
    """The modules of every handler mounted in `app`."""
    return {
        (context.endpoint or context.original_route.endpoint).__module__
        for context in iter_route_contexts(app.router.routes)
    }


@pytest.fixture(scope="module")
def h():
    """The composition that runs today, on the harness's database."""
    harness = build_admin_harness([], logger_name="test-c11")
    from tests.app_factory import create_app

    real = create_app()
    harness.app.router.routes.extend(real.router.routes)
    real.dependency_overrides = harness.app.dependency_overrides
    harness.real = real
    yield harness
    harness.close()


@pytest.fixture(scope="module")
def community(h):
    app = community_app(h.app.dependency_overrides[get_db])
    tenant = h.headers("t1")
    with community_principals():
        yield app, TestClient(app, raise_server_exceptions=False), tenant


# =============================================================================
# Recorder: the composition with everything
# =============================================================================


class TestRecorderFullComposition:


    def test_no_notifications_path_of_one_router_can_take_a_url_of_the_other(self, h):
        """The collision is the prefix only: the first segment after it differs."""
        first = lambda ops: {path.split("/")[4] for _m, path in ops}
        assert first(PORTAL_INBOX_OPS) == {"send", "sent"}
        assert first(ASSURANCE_NOTIFICATION_OPS) == {"channels", "subscriptions", "deliveries"}

    def test_the_icon_is_public_and_a_missing_one_is_404(self, h):
        r = h.client.get("/v1/admin/providers/nope/icon")
        assert r.status_code == 404
        assert r.json()["detail"] == "Provider icon not found"

    def test_icon_writes_need_a_session(self, h):
        assert h.client.delete("/v1/admin/providers/nope/icon").status_code == 401
        assert h.client.post("/v1/admin/providers/nope/icon").status_code == 401


    def test_assurance_channels_answer(self, h):
        r = h.client.get("/v1/admin/notifications/channels", headers=h.headers("t1"))
        assert r.status_code == 200


# =============================================================================
# The sides
# =============================================================================


# =============================================================================
# The Community composition
# =============================================================================


class TestTheCommunityComposition:
    def test_the_icon_endpoints_are_in_it(self, community):
        app, client, _tenant = community
        assert ICON_OPS <= operations(app)
        assert client.get("/v1/admin/providers/nope/icon").status_code == 404
        assert client.get("/v1/admin/providers/nope/icon").json()["detail"] == "Provider icon not found"
        assert client.delete("/v1/admin/providers/nope/icon").status_code == 401

    def test_the_router_cache_config_is_not(self, community):
        app, client, tenant = community
        assert not CACHE_CONFIG_OPS & operations(app)
        assert not [p for _m, p in operations(app) if "cache-config" in p]
        for method in ("GET", "PATCH"):
            r = client.request(method, "/v1/admin/llm-router/cache-config", json={}, headers=tenant)
            assert r.status_code in (404, 405), (method, r.status_code)

    def test_the_rest_of_the_llm_router_is(self, community, h):
        app, client, tenant = community
        full = operations(h.real, "/v1/admin/llm-router")
        assert operations(app, "/v1/admin/llm-router") == full - CACHE_CONFIG_OPS
        assert len(full - CACHE_CONFIG_OPS) >= 10
        with no_network():
            r = client.get("/v1/admin/llm-router/routing-groups", headers=tenant)
        assert r.status_code == 200

    def test_no_router_cache_row_is_written_by_a_community_request(self, community, h):
        """The Enterprise table stays empty in Community (one schema: the
        table exists in both editions, only Enterprise writes it)."""
        from app.database.models.router_cache_config import RouterCacheConfig

        s = h.Factory()
        try:
            before = s.query(RouterCacheConfig).count()
        finally:
            s.close()
        _app, client, tenant = community
        with no_network():
            client.get("/v1/admin/llm-router/cache-config", headers=tenant)
            client.patch("/v1/admin/llm-router/cache-config", json={"ttl_secs": 5}, headers=tenant)
            client.get("/v1/admin/llm-router/status", headers=tenant)
        s = h.Factory()
        try:
            assert s.query(RouterCacheConfig).count() == before
        finally:
            s.close()

    def test_the_notifications_prefix_holds_the_assurance_routes_only(self, community):
        app, client, tenant = community
        assert operations(app, "/v1/admin/notifications") == ASSURANCE_NOTIFICATION_OPS
        r = client.post("/v1/admin/user-notifications/send", json={"title": "x"}, headers=tenant)
        assert r.status_code in (404, 405)
        assert client.get("/v1/admin/user-notifications/sent", headers=tenant).status_code in (404, 405)
        assert client.get("/v1/admin/notifications/channels", headers=tenant).status_code == 200


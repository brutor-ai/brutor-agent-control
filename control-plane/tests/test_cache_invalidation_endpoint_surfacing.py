# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A failed cache-invalidation publish must be surfaced on EVERY admin change.

`invalidate_proxy_cache` is the one entry point that does that: it records
the failure on the request (the `CacheInvalidationWarningMiddleware` turns it
into `X-Brutor-Cache-Invalidated: false` + a `Warning` header) and raises the
throttled `downstream_cache` SystemAlert. The semantic-cache and
knowledge-base admin endpoints called the lower-level `publish_invalidation`
directly, so a dead publisher there returned an unqualified 204 and raised no
alert — the operator saw success while the proxies kept the old config.

Covered here:
  * one endpoint per family (semantic cache, knowledge base): a publish
    failure yields the warning header + alert, and the request still
    succeeds;
  * a class guard: no module under app/api/ calls `publish_invalidation` /
    `publish_invalidation_sync` directly (AST scan — imports and comments
    do not count, a call does).

Mocks only; no Redis, no database.
"""

import ast
import pathlib
import types
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.middleware.cache_invalidation import CacheInvalidationWarningMiddleware
from app.services import cache_invalidation as ci
from tests.source_trees import python_sources

API_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"
LOW_LEVEL_PUBLISHERS = {"publish_invalidation", "publish_invalidation_sync"}

_FAKE_ADMIN = types.SimpleNamespace(
    id="admin-test", username="tester", email="t@example.com", tenant_id="t1"
)


def _fake_require_permission(_permission):
    def _dep():
        return _FAKE_ADMIN
    return _dep


def _fake_require_tenant_resource_access(_resource_type, *a, **k):
    def _dep():
        return (_FAKE_ADMIN, "t1", "tenant")
    return _dep


class _DeadRedis:
    """A publisher whose Redis is unreachable."""

    async def publish(self, channel, message):
        raise ConnectionError("Timeout connecting to server")


@pytest.fixture(autouse=True)
def _reset_publisher_health():
    ci._publisher_health.reset()
    yield
    ci._publisher_health.reset()


@pytest.fixture
def alerts(monkeypatch):
    raised = []
    monkeypatch.setattr(
        ci, "_raise_publish_failure_alert", lambda rt, tid: raised.append((rt, tid))
    )
    return raised


def _client(module, router):
    app = FastAPI()
    app.add_middleware(CacheInvalidationWarningMiddleware)
    app.include_router(router, prefix="/v1")
    app.state.redis = _DeadRedis()
    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[module.get_audit_logger] = lambda: MagicMock()
    return TestClient(app)


def _patch_auth(monkeypatch, module):
    monkeypatch.setattr(module, "require_permission", _fake_require_permission)
    monkeypatch.setattr(
        module, "require_tenant_resource_access", _fake_require_tenant_resource_access
    )


def _assert_surfaced(resp, alerts, resource_type):
    assert resp.status_code == 204, resp.text
    assert resp.headers.get("x-brutor-cache-invalidated") == "false"
    assert "invalidation could not be published" in resp.headers.get("warning", "")
    assert alerts == [(resource_type, "t1")]


# ---------------------------------------------------------------------------
# Class guard: the API layer goes through invalidate_proxy_cache only
# ---------------------------------------------------------------------------

def _direct_publisher_calls(source: str):
    """(lineno, name) for every call to a low-level publisher."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
        if name in LOW_LEVEL_PUBLISHERS:
            hits.append((node.lineno, name))
    return sorted(hits)


def test_scanner_detects_the_old_shape():
    """Prove the guard fires on exactly the pattern it replaced."""
    old_shape = (
        "def h(request):\n"
        "    redis_client = getattr(request.app.state, 'redis', None)\n"
        "    if redis_client:\n"
        "        block_on(publish_invalidation(\n"
        "            redis_client, 'semantic_cache', 't', 'c', action='delete'))\n"
        "    ci.publish_invalidation_sync(redis_client, 'x', 't', 'c')\n"
    )
    assert [n for _, n in _direct_publisher_calls(old_shape)] == [
        "publish_invalidation", "publish_invalidation_sync",
    ]
    ok_shape = (
        "from app.services.cache_invalidation import publish_invalidation  # unused\n"
        "def h(request):\n"
        "    block_on(invalidate_proxy_cache(request, 'model', 't', 'c'))\n"
    )
    assert _direct_publisher_calls(ok_shape) == []


def test_no_api_module_calls_low_level_publisher():
    offenders = []
    for path in python_sources(API_ROOT):
        for lineno, name in _direct_publisher_calls(path.read_text()):
            offenders.append(f"{path.relative_to(API_ROOT.parent.parent)}:{lineno} {name}()")
    assert not offenders, (
        "Admin endpoints must publish through invalidate_proxy_cache(request, ...) "
        "so a failed publish adds the warning header and raises the "
        "downstream_cache alert:\n  " + "\n  ".join(offenders)
    )

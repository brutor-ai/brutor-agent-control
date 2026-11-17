# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Performance regression guard for the resource-groups router.

The control plane runs under a single Uvicorn event loop per worker. A
read-only handler declared ``async def`` that then does synchronous
SQLAlchemy work blocks that loop for the duration of every query — under
admin-UI load (the hierarchy tree / stats / list endpoints are polled
constantly) this serialises all other requests behind it.

FastAPI runs a plain ``def`` handler in its threadpool instead, keeping the
event loop free. So every GET (read-only) handler on this router MUST be a
sync ``def``. This test pins that contract so a future edit can't silently
re-introduce an ``async def`` read handler and re-block the loop.

Write handlers (POST/PUT/PATCH/DELETE) legitimately ``await`` the proxy
cache-invalidation call, so they stay ``async`` — they are not asserted here.
"""

import inspect
from unittest.mock import MagicMock

from fastapi.routing import APIRoute

from app.api.v1.endpoints.admin.resource_groups import create_resource_groups_router


def _get_routes():
    router = create_resource_groups_router(
        usage_limit_enforcer=MagicMock(),
        logger=MagicMock(),
    )
    return [r for r in router.routes if isinstance(r, APIRoute)]


def test_all_get_handlers_are_sync_def():
    """Every read-only (GET) handler must be a sync def so it runs off the
    event loop in the threadpool."""
    offenders = []
    get_count = 0
    for route in _get_routes():
        methods = set(route.methods or set())
        # Pure read endpoints (GET, optionally with the auto-added HEAD).
        if methods <= {"GET", "HEAD"} and "GET" in methods:
            get_count += 1
            if inspect.iscoroutinefunction(route.endpoint):
                offenders.append(f"{sorted(methods)} {route.path} -> {route.endpoint.__name__}")

    assert get_count >= 40, f"expected the full GET surface, only saw {get_count}"
    assert not offenders, (
        "These GET handlers are async def and will block the event loop on "
        "synchronous DB access — flip them to `def`:\n  " + "\n  ".join(offenders)
    )


def test_write_handlers_bridge_cache_invalidation():
    """Write handlers are sync `def` too (the query stays in the threadpool);
    the cache-invalidation publish is genuinely async, so every sync writer
    that invalidates must hand it to ``block_on`` — a bare
    ``invalidate_proxy_cache(...)`` call in a sync handler would create a
    coroutine and never run it."""
    unbridged, writers = [], 0
    for route in _get_routes():
        methods = set(route.methods or set())
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        writers += 1
        assert not inspect.iscoroutinefunction(route.endpoint), (
            f"{sorted(methods)} {route.path} is async def; make it def and "
            "route the await through app.core.sync_bridge.block_on"
        )
        src = inspect.getsource(route.endpoint)
        calls = src.count("invalidate_proxy_cache(")
        bridged = src.count("block_on(invalidate_proxy_cache(")
        if calls != bridged:
            unbridged.append(f"{sorted(methods)} {route.path}")
    assert writers >= 25, f"expected the full write surface, only saw {writers}"
    assert not unbridged, (
        "These sync write handlers call invalidate_proxy_cache without "
        "block_on (the coroutine would never run):\n  " + "\n  ".join(unbridged)
    )

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Class guard: no route the real app mounts is shadowed by an earlier one.

Routing is first match wins, in mount order. A route declared after another route
that already matches its URL is never reached: the request is answered by the
earlier handler, usually with a parameter value that was meant to be a path
segment. Two cases shipped that way and are what this guard was written against:

* ``GET /v1/admin/mcp-registries/health`` after ``GET /v1/admin/mcp-registries/{registry_id}``
  (the health check was answered by get-by-id with id ``health``);
* ``DELETE .../models/{model_name:path}/register`` after
  ``DELETE .../models/{model_name:path}`` (the greedy path parameter swallowed
  ``/register``, so "unregister from the gateway" ran "remove model").

For every mounted route the guard builds one concrete URL and asks the routes, in
order, which one takes it. The answer has to be the route itself. The route list
comes from ``create_app()``, so a shadowed route fails here the day it lands, in
any router and across routers.

A second guard keeps router mount order free. Where two routes can take the same
URL, their order decides which one answers. Every such pair has to live inside
one router, where the order is visible in one file; a pair split over two
routers would make the order of the router list load-bearing, and another
distribution adding routers (the Enterprise Edition) would have to know where they go.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.routing import Match

_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")

# One value per path-parameter convertor. The string value has no slash, so it
# fills exactly one segment; the path value has one, so a greedy parameter is
# exercised the way a real nested name (``org/model``) exercises it. The values
# must not equal a literal path segment of any route, or a correctly ordered
# static route (``/reviews/sample`` before ``/reviews/{review_id}``) would take
# the parameter route's URL; `test_sample_values_are_not_route_segments` holds that.
_SAMPLE = {
    "IntegerConvertor": "7",
    "FloatConvertor": "1.5",
    "UUIDConvertor": "00000000-0000-4000-8000-000000000000",
    "PathConvertor": "zz-shadow-probe/zz-nested",
}
_SAMPLE_STRING = "zz-shadow-probe"


def _concrete(path: str, convertors: dict) -> str:
    def fill(match: re.Match) -> str:
        convertor = type(convertors.get(match.group(1))).__name__
        return _SAMPLE.get(convertor, _SAMPLE_STRING)

    return _PARAM.sub(fill, path)


def _scope(kind: str, path: str, method: Optional[str]) -> dict:
    scope = {"type": kind, "path": path, "root_path": "", "path_params": {}, "headers": []}
    if method is not None:
        scope["method"] = method
    return scope


def _first_taker(
    contexts: List[RouteContext], kind: str, path: str, method: Optional[str]
) -> Optional[RouteContext]:
    scope = _scope(kind, path, method)
    for context in contexts:
        if context.matches(scope)[0] == Match.FULL:
            return context
    return None


def _websocket_urls(context: RouteContext, prefixes: Iterable[str]) -> List[str]:
    """A WebSocket route does not expose its mounted path, only the one it was
    declared with. Find the mount prefix by asking the route itself."""
    declared = _concrete(
        context.original_route.path,
        getattr(context.original_route, "param_convertors", {}) or {},
    )
    return [
        prefix + declared
        for prefix in prefixes
        if context.matches(_scope("websocket", prefix + declared, None))[0] == Match.FULL
    ]


def find_shadowed(app: FastAPI) -> List[Tuple[str, str, str]]:
    """Every (method, route, taker) where the route's own URL is taken by an
    earlier route. Empty means no route is shadowed."""
    contexts = list(iter_route_contexts(app.router.routes))
    http = [c for c in contexts if c.methods]
    websocket = [c for c in contexts if not c.methods]

    # Mount prefixes seen on the HTTP side: "", "/v1", ...
    prefixes = {""}
    for context in http:
        parts = context.path.split("/")
        for depth in (2, 3):
            if len(parts) > depth:
                prefixes.add("/".join(parts[:depth]))

    found: List[Tuple[str, str, str]] = []
    for context in http:
        url = _concrete(context.path, context.param_convertors or {})
        for method in sorted(context.methods):
            taker = _first_taker(http, "http", url, method)
            if taker is not context:
                found.append((method, context.path, taker.path if taker else "<no route>"))
    for context in websocket:
        urls = _websocket_urls(context, sorted(prefixes))
        if not urls:
            found.append(("WS", context.original_route.path, "<mount prefix not found>"))
        for url in urls:
            taker = _first_taker(websocket, "websocket", url, None)
            if taker is not context:
                found.append(
                    ("WS", url, taker.original_route.path if taker else "<no route>")
                )
    return found


def _owner(context: RouteContext) -> str:
    """The router factory a route was declared in: the module, plus the
    enclosing factory function for the usual closure-style endpoints."""
    endpoint = context.endpoint or context.original_route.endpoint
    return f"{endpoint.__module__}:{endpoint.__qualname__.rsplit('.<locals>', 1)[0]}"


def find_cross_router_order_dependence(app: FastAPI) -> List[Tuple[str, str, str, str, str]]:
    """Every (method, earlier route, its owner, later route, its owner) where the
    later route would also take the earlier route's URL and the two were declared
    by different router factories. Swapping those two routers changes behaviour."""
    http = [c for c in iter_route_contexts(app.router.routes) if c.methods]
    owners = [_owner(c) for c in http]
    found = []
    for index, earlier in enumerate(http):
        url = _concrete(earlier.path, earlier.param_convertors or {})
        for method in sorted(earlier.methods):
            scope = _scope("http", url, method)
            for offset, later in enumerate(http[index + 1 :], start=index + 1):
                if owners[offset] == owners[index]:
                    continue
                if later.matches(scope)[0] == Match.FULL:
                    found.append((method, earlier.path, owners[index], later.path, owners[offset]))
    return found


# ---------------------------------------------------------------------------
# The guard detects the class (proved on small apps that put each bug back).
# ---------------------------------------------------------------------------


def _app(*routes: Tuple[str, str]) -> FastAPI:
    router = APIRouter(prefix="/things")
    for index, (method, path) in enumerate(routes):

        def handler() -> dict:  # one distinct endpoint per route
            return {}

        handler.__name__ = f"handler_{index}"
        router.add_api_route(path, handler, methods=[method])
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    return app


def test_a_static_path_declared_after_a_parameter_is_reported():
    app = _app(("GET", "/{thing_id}"), ("GET", "/health"))
    assert find_shadowed(app) == [("GET", "/v1/things/health", "/v1/things/{thing_id}")]


def test_a_static_path_declared_before_a_parameter_is_clean():
    assert find_shadowed(_app(("GET", "/health"), ("GET", "/{thing_id}"))) == []


def test_a_suffix_declared_after_a_greedy_path_parameter_is_reported():
    app = _app(("DELETE", "/{name:path}"), ("DELETE", "/{name:path}/register"))
    assert find_shadowed(app) == [
        ("DELETE", "/v1/things/{name:path}/register", "/v1/things/{name:path}")
    ]


def test_a_suffix_declared_before_a_greedy_path_parameter_is_clean():
    app = _app(("DELETE", "/{name:path}/register"), ("DELETE", "/{name:path}"))
    assert find_shadowed(app) == []


def test_the_same_path_with_another_method_is_not_shadowing():
    assert find_shadowed(_app(("GET", "/{thing_id}"), ("DELETE", "/{thing_id}"))) == []


def test_a_route_declared_twice_is_reported():
    app = _app(("GET", "/{thing_id}"), ("GET", "/{other_id}"))
    assert find_shadowed(app) == [("GET", "/v1/things/{other_id}", "/v1/things/{thing_id}")]


def test_a_websocket_route_whose_mount_cannot_be_located_is_reported():
    # Fail closed: a route the guard cannot build a URL for is a finding, not a pass.
    router = APIRouter(prefix="/streams")

    @router.websocket("/tail")
    async def tail(websocket) -> None:  # pragma: no cover - never called
        return None

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    assert find_shadowed(app) == [("WS", "/streams/tail", "<mount prefix not found>")]


def test_shadowing_across_two_routers_is_reported():
    first, second = APIRouter(prefix="/things"), APIRouter(prefix="/things")

    @first.get("/{thing_id}")
    def get_thing() -> dict:
        return {}

    @second.get("/live")
    def live_things() -> dict:
        return {}

    app = FastAPI()
    app.include_router(first, prefix="/v1")
    app.include_router(second, prefix="/v1")
    assert find_shadowed(app) == [("GET", "/v1/things/live", "/v1/things/{thing_id}")]


def _two_router_app(first_path: str, second_path: str) -> FastAPI:
    def make_first() -> APIRouter:
        router = APIRouter(prefix="/things")

        @router.get(first_path)
        def first_handler() -> dict:
            return {}

        return router

    def make_second() -> APIRouter:
        router = APIRouter(prefix="/things")

        @router.get(second_path)
        def second_handler() -> dict:
            return {}

        return router

    app = FastAPI()
    app.include_router(make_first(), prefix="/v1")
    app.include_router(make_second(), prefix="/v1")
    return app


def test_a_static_route_and_its_parameter_route_in_two_routers_is_order_dependent():
    # Reachable today (static first), so the shadow guard is silent; but the
    # two routers cannot be swapped, which is what this guard reports.
    app = _two_router_app("/live", "/{thing_id}")
    assert find_shadowed(app) == []
    found = find_cross_router_order_dependence(app)
    assert [(method, earlier, later) for method, earlier, _, later, _ in found] == [
        ("GET", "/v1/things/live", "/v1/things/{thing_id}")
    ]


def test_unrelated_routes_in_two_routers_are_not_order_dependent():
    assert find_cross_router_order_dependence(_two_router_app("/live", "/{thing_id}/detail")) == []


def test_order_dependence_inside_one_router_is_allowed():
    # Static before parameter in one router: correct, visible in one file.
    assert find_cross_router_order_dependence(_app(("GET", "/live"), ("GET", "/{thing_id}"))) == []


def test_a_shadowed_websocket_route_is_reported():
    router = APIRouter(prefix="/streams")

    @router.websocket("/{stream_id}")
    async def one_stream(websocket) -> None:  # pragma: no cover - never called
        return None

    @router.websocket("/tail")
    async def tail(websocket) -> None:  # pragma: no cover - never called
        return None

    @router.get("/count")  # the mount prefix is read from the HTTP side
    def count_streams() -> dict:
        return {}

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    assert find_shadowed(app) == [("WS", "/v1/streams/tail", "/streams/{stream_id}")]


# ---------------------------------------------------------------------------
# The real app.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_app() -> FastAPI:
    from tests.app_factory import create_app

    return create_app()


def test_sample_values_are_not_route_segments(real_app):
    segments = {
        segment
        for context in iter_route_contexts(real_app.router.routes)
        for segment in (context.path or context.original_route.path).split("/")
    }
    samples = {_SAMPLE_STRING, *(part for value in _SAMPLE.values() for part in value.split("/"))}
    # "7" and "1.5" are typed values; no route has a literal numeric segment.
    assert not samples & segments


def test_no_route_of_the_real_app_is_shadowed(real_app):
    shadowed = find_shadowed(real_app)
    assert shadowed == [], (
        "These routes are never reached: an earlier route takes their URL. Declare "
        "the more specific route first.\n"
        + "\n".join(f"  {method} {path}  <- taken by {taker}" for method, path, taker in shadowed)
    )


def test_no_pair_of_routes_depends_on_router_mount_order(real_app):
    found = find_cross_router_order_dependence(real_app)
    assert found == [], (
        "These route pairs are split over two routers, so the order the routers are "
        "mounted in decides which one answers. Declare both in the same router, the "
        "more specific one first.\n"
        + "\n".join(
            f"  {method} {earlier} ({a})  also taken by  {later} ({b})"
            for method, earlier, a, later, b in found
        )
    )

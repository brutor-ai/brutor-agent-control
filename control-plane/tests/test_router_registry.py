# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The router registry (`app/composition/routers.py`) is the only place the control
plane's routers are listed, and `create_app` mounts exactly that list, in order.

Why it matters: the registry is the seam another distribution (the Enterprise Edition) extends.
A router mounted somewhere else would be invisible to it, and would also escape
the order the registry documents (routing is first match wins).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts

from app.composition.routers import (
    ENTERPRISE_FEATURES,
    ROUTERS,
    RouterSpec,
    mount_routers,
)
from tests.source_trees import python_sources, source_label


APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _route_table(app: FastAPI) -> List[Tuple[str, str, str]]:
    """(methods, path, endpoint) of every route, in mount order."""
    rows = []
    for context in iter_route_contexts(app.router.routes):
        # A WebSocket route exposes its handler on the declared route only.
        endpoint = context.endpoint or context.original_route.endpoint
        rows.append(
            (
                ",".join(sorted(context.methods or [])),
                context.path or context.original_route.path,
                f"{endpoint.__module__}.{endpoint.__qualname__}",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# The list itself.
# ---------------------------------------------------------------------------


def test_router_names_are_unique():
    names = [spec.name for spec in ROUTERS]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)


def test_every_feature_is_a_known_enterprise_feature():
    unknown = {spec.name: spec.feature for spec in ROUTERS if spec.feature not in ENTERPRISE_FEATURES | {None}}
    assert unknown == {}


# ---------------------------------------------------------------------------
# Mounting.
# ---------------------------------------------------------------------------


def _router(path: str) -> APIRouter:
    router = APIRouter()

    def handler() -> dict:
        return {}

    router.add_api_route(path, handler, methods=["GET"])
    return router


def test_mount_routers_mounts_in_the_order_given_with_each_prefix():
    seen = []
    specs = (
        RouterSpec("first", lambda s: (seen.append(("first", s)), _router("/a"))[1]),
        RouterSpec("second", lambda s: (seen.append(("second", s)), _router("/b"))[1], prefix=""),
        RouterSpec("third", lambda s: (seen.append(("third", s)), _router("/c"))[1], prefix="/x"),
    )
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    services = object()

    mount_routers(app, services, specs)  # type: ignore[arg-type]

    assert seen == [("first", services), ("second", services), ("third", services)]
    assert [path for _, path, _ in _route_table(app)] == ["/v1/a", "/b", "/x/c"]


@pytest.fixture(scope="module")
def real_app() -> FastAPI:
    from tests.app_factory import create_app

    return create_app()


def _calls(tree: ast.AST, attribute: str) -> List[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]


def test_no_router_is_mounted_outside_the_registry():
    """`include_router` on the application is called in one place. (Routers
    that nest sub-routers of their own call it on an `APIRouter`, inside the
    endpoint packages; those are part of the router the registry builds.)"""
    offenders = {}
    for path in python_sources(APP_DIR):
        relative = source_label(path)
        if relative.startswith("api/"):
            continue
        lines = _calls(ast.parse(path.read_text(encoding="utf-8")), "include_router")
        if lines:
            offenders[relative] = lines
    assert list(offenders) == ["composition/routers.py"], offenders
    assert len(offenders["composition/routers.py"]) == 1


def test_the_guard_above_sees_a_direct_mount():
    tree = ast.parse("app.include_router(make_router(), prefix='/v1')\n")
    assert _calls(tree, "include_router") == [1]


def test_create_app_mounts_exactly_the_registry_in_order(real_app):
    """The Community half (the Enterprise Edition's is in
    `enterprise/tests/test_router_registry.py`): the application mounts the
    composition of the edition under test, whose first routers are the
    registry's, in the registry's order. In a Community run that is all of it."""
    from app.composition.services import build_services
    from app.core.logging_system import setup_logging
    from tests.app_factory import COMMUNITY, composition, edition_under_test

    only_registry = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    composed = composition().routers
    assert composed[: len(ROUTERS)] == ROUTERS
    if edition_under_test() == COMMUNITY:
        assert composed == ROUTERS
    mount_routers(only_registry, build_services(setup_logging("INFO", use_json=False)), composed)

    builtin = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    mounted = [row for row in _route_table(real_app) if row[1] not in builtin]
    assert mounted == _route_table(only_registry)
    assert len(mounted) > 600

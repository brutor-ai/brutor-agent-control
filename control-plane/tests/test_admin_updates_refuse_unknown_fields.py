# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
An admin update refuses a field it does not know.

One rule for every route that changes something with PATCH or PUT: the
body is a model that lists its fields, and a field that is not listed is
answered 422 naming it. A field that was dropped silently is how a
console and an API drift apart: the form shows a setting, the save
answers 200, and nothing was stored.
"""

from __future__ import annotations

from pydantic import BaseModel

from tests.app_factory import create_app

# Routes whose body is not a document of fields: what it is instead.
NOT_A_DOCUMENT = {
    "/v1/admin/mcp-registries/instance/proxy/{path:path}": "forwarded to the registry as it is",
}


def _updates(app):
    def walk(routes, prefix=""):
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from walk(included.routes, prefix + route.include_context.prefix)
            elif getattr(route, "dependant", None) is not None and getattr(route, "methods", None):
                yield prefix + route.path, route

    for path, route in walk(app.routes):
        if not path.startswith("/v1/admin") or path in NOT_A_DOCUMENT:
            continue
        for method in sorted(route.methods & {"PATCH", "PUT"}):
            for body in route.dependant.body_params:
                yield f"{method} {path}", body.field_info.annotation


def test_every_update_states_its_fields_and_refuses_any_other():
    seen, wrong = 0, []
    for operation, annotation in _updates(create_app()):
        seen += 1
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            wrong.append(f"{operation}: the body is {annotation}, not a model that lists its fields")
        elif annotation.model_config.get("extra") != "forbid":
            wrong.append(f"{operation}: {annotation.__name__} does not refuse unknown fields")
    assert seen > 60
    assert wrong == []

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Community Edition's recorded OpenAPI document and how a document is
written down.

`canonicalize` was `_canonicalize` of `tests/test_openapi_snapshot.py`. That
test is the Enterprise Edition's (`enterprise/tests/test_openapi_snapshot.py`); the Community snapshot test and the factory tests use the same
function, so it lives here and both suites import it.

The Community recording is `tests/fixtures/openapi_snapshot_community.json`.
The Enterprise Edition's document (its recording and its export) is under
`enterprise/` (`enterprise/tests/enterprise_documents.py`): a Community tree
carries the Community document alone. The Admin Console, the docs site and
the packages read both by path.
"""

from __future__ import annotations

import enum
import json
import typing
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
COMMUNITY_SNAPSHOT_PATH = FIXTURES / "openapi_snapshot_community.json"


def canonicalize(spec: dict) -> str:
    """Dump the spec to a stable, diff-friendly JSON string.

    `sort_keys=True` means swapping two dict keys in the Python code
    doesn't produce a spurious diff. `indent=2` keeps the snapshot
    file reviewable line-by-line in PRs.
    """
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def schema_names_qualified_by_module(document: dict) -> list:
    """The schema names FastAPI had to qualify with a module path.

    Two models that share a class name cannot both be published under it, so
    the framework publishes each as `<module path>__<ClassName>` (dots
    written `__`). Such a name changes whenever a file moves, and a model one
    composition publishes as `Name` becomes `some__module__Name` in a
    composition that mounts a second `Name`. `Body_...` names
    are the framework's own, built from the route path (`{id}` is written
    `__id__`), and say nothing about a collision.
    """
    return sorted(
        name
        for name in document.get("components", {}).get("schemas", {})
        if "__" in name and not name.startswith("Body_")
    )


def _classes_in(annotation: object, seen: set) -> None:
    if isinstance(annotation, type):
        from pydantic import BaseModel

        if annotation in seen:
            return
        if issubclass(annotation, BaseModel):
            seen.add(annotation)
            for field in annotation.model_fields.values():
                _classes_in(field.annotation, seen)
            return
        if issubclass(annotation, enum.Enum):
            seen.add(annotation)
            return
    for argument in typing.get_args(annotation):
        _classes_in(argument, seen)


def published_classes(application: object) -> set:
    """Every Pydantic model and enum class the application's document is
    generated from: what its routes take and return (the same fields the
    framework collects), and everything those reach through their fields."""
    from fastapi.openapi.utils import get_fields_from_routes

    seen: set = set()
    for field in get_fields_from_routes(application.routes):  # type: ignore[attr-defined]
        _classes_in(field.field_info.annotation, seen)
    return seen


def classes_sharing_a_name(application: object) -> dict:
    """`{class name: [module.qualname, ...]}` for every name two or more
    published classes carry. Catches what the document cannot show: two
    same-named models with the SAME fields are published as one schema under
    the plain name, until one of them gains a field and both are renamed."""
    by_name: dict = {}
    for cls in published_classes(application):
        by_name.setdefault(cls.__name__, set()).add(f"{cls.__module__}.{cls.__qualname__}")
    return {name: sorted(where) for name, where in sorted(by_name.items()) if len(where) > 1}

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Community Edition's OpenAPI document.

`enterprise/tests/test_openapi_snapshot.py` holds the document of the full
composition (`enterprise/tests/fixtures/openapi_snapshot.json`). This file
holds the document of the Community composition, recorded from the same build
with the Enterprise entries taken out (`tests/community_process.py`), in
`tests/fixtures/openapi_snapshot_community.json`.

The Enterprise entries are not in the built-in registries; they come in as
an extension. "The Community application is the same application with or
without the extension" is a byte comparison and not an opinion.

Also held here: the Community document has no path under an Enterprise route
prefix (`editions.yaml`, `control_plane.enterprise_route_prefixes`). The
comparisons with the Enterprise document (every Community operation is one of
the full composition's; the Enterprise document is a strict superset at path,
operation and schema level) are the Enterprise half's,
`enterprise/tests/test_community_openapi_snapshot.py`: a Community tree has no
Enterprise document.

To re-record on purpose:
    BRUTOR_UPDATE_COMMUNITY_OPENAPI_SNAPSHOT=1 pytest tests/test_community_openapi_snapshot.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.app_factory import create_community_app
from tests.edition_boundary import control_plane_boundary
from tests.openapi_documents import canonicalize as _canonicalize

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "openapi_snapshot_community.json"
UPDATE_ENV = "BRUTOR_UPDATE_COMMUNITY_OPENAPI_SNAPSHOT"
METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _community_spec() -> dict:
    return create_community_app().openapi()


def _operations(spec: dict) -> dict:
    return {
        f"{method.upper()} {path}": operation
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    }


@pytest.fixture(scope="module")
def community() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_community_openapi_surface_snapshot() -> None:
    current = _canonicalize(_community_spec())
    if os.getenv(UPDATE_ENV) == "1":
        SNAPSHOT_PATH.write_text(current, encoding="utf-8")
        pytest.skip(f"{UPDATE_ENV}=1 set: snapshot regenerated at {SNAPSHOT_PATH}. Commit it.")
    assert SNAPSHOT_PATH.exists(), f"no snapshot at {SNAPSHOT_PATH}; record it with {UPDATE_ENV}=1"
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert current == expected, (
        "\nThe Community Edition's OpenAPI surface has drifted from the committed snapshot.\n"
        f"If INTENTIONAL, regenerate:\n    {UPDATE_ENV}=1 pytest tests/test_community_openapi_snapshot.py\n"
        "If UNINTENTIONAL, fix the code; do not edit the fixture.\n"
    )


def test_the_community_document_has_no_enterprise_route_prefix(community) -> None:
    prefixes = [p for group in control_plane_boundary()["enterprise_route_prefixes"].values() for p in group]
    assert len(prefixes) > 15
    under = sorted(
        path for path in community["paths"] if any(path == p or path.startswith(p + "/") for p in prefixes)
    )
    assert under == []


def test_the_community_tree_holds_the_community_document_alone() -> None:
    """The Enterprise document (its recording and its export) is the
    Enterprise Edition's, under `enterprise/`: outside it there is one OpenAPI
    document, the Community one."""
    repo = SNAPSHOT_PATH.parents[2]
    skipped = ("enterprise", ".venv", "node_modules", "mcp-server-builds", ".git")
    found = sorted(
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*.json")
        if path.relative_to(repo).parts[0] not in skipped
        and ("openapi" in path.name or '"openapi": "3.' in path.read_text(encoding="utf-8", errors="replace")[:4096])
    )
    assert found == ["tests/fixtures/openapi_snapshot_community.json"]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
CP-14 — one tenant-scoping rule, not twenty-five copies of it.

`_resolve_tenant_id` / `_tenant_of` was a five-line copy in twenty-five
endpoint modules. It had already drifted: most copies silently ignored a
mismatched `?tenant_id=`, while `compliance.py` and `data_erasure.py`
refused it with 403. The copies are now aliases of one implementation in
`app/core/admin_user/dependencies.py`, and this module pins both the
behaviour and the absence of new copies.

Why 403 is the correct half of the fork: ignoring the parameter answers a
question nobody asked. The operator requested tenant B and gets tenant A's
rows under a heading that says B; on a write path — an erasure, a config
change — they believe they acted on B and actually acted on A. There is no
reading under which silently substituting a different tenant is the safer
failure.
"""

import ast
import os
import pathlib

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from app.constants.admin import AdminType
from app.core.admin_user.dependencies import resolve_tenant_id
from tests.source_trees import python_sources

ADMIN_ENDPOINTS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "api" / "v1" / "endpoints" / "admin"
)


class _Admin:
    def __init__(self, tenant_id="t1"):
        self.tenant_id = tenant_id


def test_tenant_admin_is_pinned_to_its_own_tenant():
    assert resolve_tenant_id(_Admin(), "tenant") == "t1"
    assert resolve_tenant_id(_Admin(), "tenant", "t1") == "t1"


def test_tenant_admin_naming_another_tenant_is_refused():
    with pytest.raises(HTTPException) as exc:
        resolve_tenant_id(_Admin(), "tenant", "t2")
    assert exc.value.status_code == 403


def test_admin_type_enum_and_plain_string_agree():
    """Call sites pass whichever they have; `AdminType` is a str enum."""
    assert resolve_tenant_id(_Admin(), AdminType.TENANT_ADMIN_TYPE) == "t1"
    with pytest.raises(HTTPException):
        resolve_tenant_id(object(), AdminType.SYSTEM_ADMIN_TYPE)


def test_no_endpoint_module_declares_its_own_copy():
    """A fresh copy is how the rule drifted the first time."""
    offenders = []
    for path in python_sources(ADMIN_ENDPOINTS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                "_resolve_tenant_id",
                "_tenant_of",
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these modules declare their own tenant resolver instead of aliasing "
        "app.core.admin_user.dependencies.resolve_tenant_id:\n  "
        + "\n  ".join(offenders)
    )


def test_the_aliases_point_at_the_shared_implementation():
    """Guard the guard — if the alias stopped resolving, the test above
    would pass while every module ran its own logic again."""
    import importlib

    for module, attr in (
        ("ai_systems", "_resolve_tenant_id"),
        ("compliance", "_resolve_tenant_id"),
        ("data_erasure", "_resolve_tenant_id"),
        ("end_user_groups", "_resolve_tenant_id"),
    ):
        mod = importlib.import_module(f"app.api.v1.endpoints.admin.{module}")
        assert getattr(mod, attr) is resolve_tenant_id, module

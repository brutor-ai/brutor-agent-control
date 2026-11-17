# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The test suite takes its application from one place: `tests/app_factory.py` says which edition is under test and returns that
edition's factory, and the admin endpoint harness gets the edition's
additions through it.

Held here:

* the default is the full composition, and its application is the one the
  OpenAPI golden describes (so the suite tests what it tested before);
* no test file reaches `app.app.create_app` itself, and the harness imports
  no Enterprise module: a Community checkout can import both;
* in a run for the Community Edition (`BRUTOR_TEST_EDITION=community`, a
  process of its own here) the application is the Community composition's
  and the harness has no system administrator and no login route for one.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.composition.extensions import EXTENSION_INTERFACE_VERSION

from tests import app_factory
from tests.edition_boundary import REPO, control_plane_boundary, enterprise_paths, import_edges
from tests.source_trees import test_label, test_sources

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures"


class TestTheDefaultEdition:
    def test_it_is_the_full_composition(self, monkeypatch):
        monkeypatch.delenv(app_factory.EDITION_ENV, raising=False)
        assert app_factory.edition_under_test() == app_factory.ENTERPRISE


    def test_an_unknown_edition_is_refused(self, monkeypatch):
        monkeypatch.setenv(app_factory.EDITION_ENV, "trial")
        with pytest.raises(ValueError, match="BRUTOR_TEST_EDITION"):
            app_factory.edition_under_test()
        with pytest.raises(ValueError):
            app_factory.app_factory()
        with pytest.raises(ValueError):
            app_factory.harness_extras()


    def test_arguments_reach_the_factory(self):
        from app.composition.extensions import Extension

        with pytest.raises(ValueError, match="names are not unique"):
            app_factory.create_app(extensions=[Extension(name="twice", interface_version=EXTENSION_INTERFACE_VERSION), Extension(name="twice", interface_version=EXTENSION_INTERFACE_VERSION)])


# ---------------------------------------------------------------------------
# Nobody goes round it.
# ---------------------------------------------------------------------------

# Files that name `app.app.create_app` on purpose.
_NAMES_THE_FACTORY = {
    "app_factory.py": "the one place",
    "community_import_probe.py": "a Community server's start, in a process of its own; imports nothing of the suite",
    "test_edition_guard.py": "reads the source of the Community factory",
    "test_composition_flip.py": "holds the two factories against each other, and reads the entry points",
    "test_packaging.py": "reads the entry point's source for the factory it names (the text, never the function)",
    # The Enterprise Edition's own suite (walked since L6, with `tests/`).
    "enterprise/tests/test_app_factory.py": "the Enterprise half of this file: the factory helper's Enterprise cases",
    "enterprise/tests/test_composition_flip.py": "holds the Enterprise factory against the Community one, and reads the entry points",
}


def _reaches_create_app(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.app":
            if any(alias.name == "create_app" for alias in node.names):
                return True
        if isinstance(node, ast.Attribute) and node.attr == "create_app":
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "from app.app import create_app" in node.value or "app.app.create_app" in node.value:
                return True
    return False


def test_no_test_file_takes_the_application_from_anywhere_else():
    found = sorted(
        test_label(path)
        for path in test_sources()
        if test_label(path) != "test_app_factory.py" and _reaches_create_app(ast.parse(path.read_text(encoding="utf-8")))
    )
    # (The Enterprise suite's files are named where the checkout has them.)
    here = [name for name in _NAMES_THE_FACTORY if (TESTS / name).exists() or (REPO / name).exists()]
    assert found == sorted(here)
    assert len(here) >= 5


def _enterprise_imports(rel: str) -> list:
    """Imports of Enterprise modules by one test-support file, any scope."""
    boundary = control_plane_boundary()
    from tests import edition_boundary

    paths = enterprise_paths(boundary)
    modules = {edition_boundary.module_name(f): f for f in edition_boundary.python_files()}
    visitor = edition_boundary._Imports(rel)
    visitor.visit(ast.parse((REPO / rel).read_text(encoding="utf-8")))
    found = []
    for base, name, _line in visitor.found:
        target = (edition_boundary._resolve(f"{base}.{name}", modules) if name else None) or edition_boundary._resolve(
            base, modules
        )
        if target is not None and edition_boundary.is_enterprise(target, paths):
            found.append(target)
    return sorted(set(found))


def test_the_factory_module_imports_nothing_at_import_time():
    """A subprocess that installs into a scratch database imports it before
    anything else; `tests/conftest.py` would replace its `DATABASE_URL`."""
    tree = ast.parse((TESTS / "app_factory.py").read_text(encoding="utf-8"))
    top = sorted(
        (node.module if isinstance(node, ast.ImportFrom) else alias.name)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )
    assert sorted(set(top)) == ["__future__", "dataclasses", "os", "typing"]


# ---------------------------------------------------------------------------
# A run for the Community Edition (a process of its own).
# ---------------------------------------------------------------------------

_COMMUNITY_RUN = """
    import json
    from tests import app_factory
    from tests.admin_endpoint_harness import build_admin_harness
    from tests.openapi_documents import canonicalize as _canonicalize
    from app.composition.edition import composed_edition
    from app.database.models.admin import SystemAdmin

    harness = build_admin_harness([], logger_name="community-run")
    session = harness.Factory()
    result = {
        "edition_under_test": app_factory.edition_under_test(),
        "composed_edition": composed_edition(),
        "system_admins": session.query(SystemAdmin).count(),
        "system_login": harness.client.post(
            "/v1/admin/users/system/login", json={"username": "sysadmin", "password": "Secret123!"}
        ).status_code,
        "tenant_login": "Authorization" in harness.headers("t1"),
        "extras_are_none": app_factory.harness_extras() is app_factory.NO_EXTRAS,
    }
    session.close()
    harness.close()
    open(OUTPUT, "w").write(_canonicalize(app_factory.create_app().openapi()))
    print("COMMUNITY_RUN " + json.dumps(result, sort_keys=True))
"""


def test_a_run_for_the_community_edition(tmp_path):
    output = tmp_path / "openapi.json"
    env = {**os.environ, app_factory.EDITION_ENV: app_factory.COMMUNITY, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("COV_CORE_SOURCE", None)
    proc = subprocess.run(
        [sys.executable, "-c", f"OUTPUT = {str(output)!r}\n" + textwrap.dedent(_COMMUNITY_RUN)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("COMMUNITY_RUN ")]
    assert lines, proc.stderr[-4000:]
    assert json.loads(lines[-1][len("COMMUNITY_RUN ") :]) == {
        "edition_under_test": "community",
        "composed_edition": "community",
        "system_admins": 0,
        "system_login": 404,
        "tenant_login": True,
        "extras_are_none": True,
    }
    expected = (FIXTURES / "openapi_snapshot_community.json").read_text(encoding="utf-8")
    assert output.read_text(encoding="utf-8") == expected


def test_no_community_test_asserts_by_the_checkout():
    """A Community test that added Enterprise cases when the checkout had
    `enterprise/` (`HAS_ENTERPRISE`) would assert different things in
    different checkouts. The Enterprise cases are in `enterprise/tests`; a
    file here does not ask whether the
    Enterprise package is present. (`source_trees.py` walks it where it is,
    for the rules that hold every source file to the same standard and name
    none.)"""
    asks = ("HAS_ENTERPRISE", "ENTERPRISE.is_dir(", "ENTERPRISE.exists(", "ENTERPRISE_TESTS.is_dir(", "ENTERPRISE_TESTS.exists(")
    offenders = sorted(
        f"{path.name}: {needle}"
        for path in TESTS.rglob("*.py")
        if path.name not in ("source_trees.py", "test_app_factory.py")
        for needle in asks
        if needle in path.read_text(encoding="utf-8")
    )
    assert offenders == []

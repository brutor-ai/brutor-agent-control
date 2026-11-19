# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The composition of the two editions: `app.app.create_app` is the
Community Edition; the Enterprise Edition is `brutor_enterprise.app.create_app`,
the same application composed with one extension.
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest


from app.composition.features import ENTERPRISE_FEATURES
from tests.community_process import fresh_registries


# ---------------------------------------------------------------------------
# A hook the extension registered that fails is an error, not a skip.
# ---------------------------------------------------------------------------


def test_the_provider_seed_fails_when_the_hook_does(monkeypatch):
    """The point's contract: a hook that raises fails the seed."""
    from app.composition.hooks import HOOKS, LLM_PROVIDER_CATALOG_ROW, HookRegistry, HookSpec
    from app.database import init_llm_providers

    def broken(**kwargs):
        raise ImportError("not installed")

    registry = HookRegistry([HookSpec("broken", LLM_PROVIDER_CATALOG_ROW, broken)])
    monkeypatch.setattr(HOOKS, "_specs", registry._specs)
    with pytest.raises(ImportError):
        init_llm_providers._apply_import_descriptor(obj=object(), key="openai")


# ---------------------------------------------------------------------------
# The built-in registries are the Community Edition.
# ---------------------------------------------------------------------------


def _builtin_lists():
    from app.composition import identity, permissions, principals, provisioning, routers, seeding, startup

    return {
        "routers": routers.ROUTERS,
        "startup": startup.STARTUP,
        "shutdown": startup.SHUTDOWN,
        "install_seed": seeding.INSTALL_SEED,
        "tenant_provisioning": provisioning.TENANT_PROVISIONING,
        "permissions": permissions.BUILTIN_PERMISSIONS,
        "principals": principals.BUILTIN_PRINCIPALS,
        "identity_protocols": identity.BUILTIN_IDENTITY_PROTOCOLS,
    }


@pytest.mark.parametrize("kind", sorted(_builtin_lists()))
def test_no_built_in_entry_belongs_to_an_enterprise_feature(kind):
    entries = _builtin_lists()[kind]
    assert len(entries) > 0
    assert [getattr(e, "name", None) or e.value for e in entries if e.feature is not None] == []


def test_the_built_in_lists_have_these_sizes():
    """So that an entry cannot leave both editions unnoticed: Community here,
    Enterprise in `test_the_extension_has_these_entries`."""
    assert {kind: len(entries) for kind, entries in _builtin_lists().items()} == {
        "routers": 80,
        "startup": 13,
        "shutdown": 1,
        "install_seed": 9,
        "tenant_provisioning": 15,
        "permissions": 123,
        "principals": 1,
        "identity_protocols": 1,
    }


def test_the_hook_registry_starts_empty():
    """Every behaviour hook is an Enterprise behaviour; the points are Community's."""
    import ast
    from pathlib import Path

    import app.composition.hooks as hooks

    tree = ast.parse(Path(hooks.__file__).read_text(encoding="utf-8"))
    assert [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "HookSpec"] == []
    assert "HOOKS = HookRegistry()" in Path(hooks.__file__).read_text(encoding="utf-8")


def test_services_holds_no_enterprise_manager():
    import dataclasses

    from app.composition.services import Services

    names = {f.name for f in dataclasses.fields(Services)}
    assert names & {"docker_deploy_manager", "k8s_deploy_manager", "registry_manager"} == set()
    assert len(names) == 14


# ---------------------------------------------------------------------------
# The extension is the Enterprise Edition, all of it.
# ---------------------------------------------------------------------------

EXTENSION_FIELDS = (
    "routers",
    "startup",
    "shutdown",
    "install_seed",
    "tenant_provisioning",
    "permissions",
    "principals",
    "hooks",
    "identity_protocols",
)


# ---------------------------------------------------------------------------
# The two applications.
# ---------------------------------------------------------------------------


def test_the_community_factory_composes_no_extension():
    from app.composition.edition import composed_enterprise_features
    from tests.app_factory import create_community_app
    from tests.community_process import community_composition

    application = create_community_app()
    assert application.state.extensions == ()
    with community_composition():
        assert composed_enterprise_features() == frozenset()


# ---------------------------------------------------------------------------
# Entry points choose the edition; nothing inside the application does.
# ---------------------------------------------------------------------------


def _repo_file(name):
    from tests.edition_boundary import REPO

    return (REPO / name).read_text(encoding="utf-8")


def test_community_code_does_not_import_the_enterprise_package():
    """`brutor_enterprise` is imported by Enterprise modules (the boundary
    file's list, and everything under `enterprise/`, where the Enterprise
    entry point is since L5) and by nothing that ships in a Community build:
    the root `main.py` is the Community entry point and is held like any
    other Community file."""
    import ast

    from tests.edition_boundary import REPO, control_plane_boundary, enterprise_paths, is_enterprise, python_files

    paths = enterprise_paths(control_plane_boundary())
    offenders = []
    files = python_files()
    assert "main.py" in files and "app/cli.py" in files and "app/launcher.py" in files
    for rel in files:
        if is_enterprise(rel, paths):
            continue
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("brutor_enterprise"):
                names = [node.value]
            if any(name.split(".")[0] == "brutor_enterprise" for name in names):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def edition_of_a_process_that_loaded(entry_point: str):
    """Load `entry_point` in a process of its own (the registries are
    process-wide) and return (the `EDITION ...` lines it printed, the end of
    its stderr)."""
    import os
    import subprocess

    from tests.edition_boundary import REPO

    script = (
        # The developer's `.env`, which an entry point loads from beside the
        # sources, is not part of what is tested.
        "import dotenv\n"
        "dotenv.find_dotenv = lambda *a, **k: ''\n"
        "dotenv.load_dotenv = lambda *a, **k: False\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('entry_point', {entry_point!r})\n"
        "spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
        "from app.composition.edition import composed_edition, composed_enterprise_features\n"
        "from app.composition.extensions import SEEDING\n"
        "print('EDITION', composed_edition(), len(composed_enterprise_features()), 'system_admin' in SEEDING.names())\n"
    )
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql://test:test@127.0.0.1:1/test_brutor",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("COV_CORE_SOURCE", None)
    env.pop("PYTHONPATH", None)  # the entry point finds `enterprise/` itself
    proc = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    return [line for line in proc.stdout.splitlines() if line.startswith("EDITION ")], proc.stderr[-2000:]


# The Community entry point. `enterprise/main.py` is held by the Enterprise
# half (`enterprise/tests/test_composition_flip.py`): what this file
# asserts does not depend on whether the checkout has `enterprise/`.
@pytest.mark.parametrize("entry_point, edition, features", [("main.py", "community", 0)])
def test_an_entry_point_makes_the_process_its_edition_before_any_command(entry_point, edition, features):
    """`init` seeds the database and `edition-reset` checks it without
    building an application: the process-wide registries must already be the
    edition's when the entry point has been loaded. (An Enterprise `init`
    that had not registered the extension would install a Community database
    and then refuse it.)"""
    lines, stderr = edition_of_a_process_that_loaded(entry_point)
    assert lines == [f"EDITION {edition} {features} {edition == 'enterprise'}"], stderr

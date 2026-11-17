# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The Community composition loads no Enterprise module and no Enterprise-only
library: both lists are empty.

`tests/community_import_probe.py` is run in a process of its own, twice:

* **record**: build the Community application (the `create_app` of `app/app.py`) and its
  OpenAPI document, and list which Enterprise modules (`editions.yaml`,
  `control_plane.enterprise_modules`) and which Enterprise-only libraries
  (`docker`, `kubernetes`, `qdrant_client`, `onelogin`, `aiohttp`) are loaded
  afterwards. None may be.
* **block**: the same start with those names refused at import, as in a
  Community build that does not ship them. It must start.

What the probe does not see: an import inside a function that the start does
not call (a request handler, a seed step). The import guard
(`tests/test_edition_import_guard.py`) sees those in the source.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.edition_boundary import (
    ENTERPRISE_ENTRY_POINT,
    ENTERPRISE_ONLY_LIBRARIES,
    REPO,
    control_plane_boundary,
    enterprise_module_names,
)

PROBE = Path(__file__).parent / "community_import_probe.py"
ENTERPRISE_PACKAGE = "brutor_enterprise"

def _run(mode: str, tmp_path_factory) -> dict:
    boundary = control_plane_boundary()
    listing = tmp_path_factory.mktemp("probe") / "boundary.json"
    listing.write_text(
        json.dumps(
            {
                # `brutor_enterprise` is the Enterprise package itself (`enterprise/`).
                "modules": enterprise_module_names(boundary) + [ENTERPRISE_PACKAGE],
                "libraries": list(ENTERPRISE_ONLY_LIBRARIES),
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        # Port 1 refuses at once: nothing here may reach a database.
        "DATABASE_URL": "postgresql://test:test@127.0.0.1:1/test_brutor",
        "JWT_SECRET": os.environ.get("JWT_SECRET", "test-jwt-secret-not-for-production"),
        "JWT_ALGORITHM": os.environ.get("JWT_ALGORITHM", "HS256"),
        "ENCRYPTION_KEY": os.environ.get("ENCRYPTION_KEY", "a" * 44 + "="),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.pop("COV_CORE_SOURCE", None)  # the probe is not part of the coverage run
    proc = subprocess.run(
        [sys.executable, str(PROBE), mode, str(listing)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("PROBE_RESULT ")]
    assert lines, f"the probe printed no result (exit {proc.returncode}):\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1][len("PROBE_RESULT ") :])


@pytest.fixture(scope="module")
def recorded_run(tmp_path_factory) -> dict:
    return _run("record", tmp_path_factory)


@pytest.fixture(scope="module")
def blocked_run(tmp_path_factory) -> dict:
    return _run("block", tmp_path_factory)


def test_the_probe_knows_the_enterprise_modules():
    """Otherwise an empty answer would mean nothing."""
    from tests.edition_boundary import enterprise_paths, is_enterprise, module_name, python_files

    boundary = control_plane_boundary()
    names = enterprise_module_names(boundary)
    # A feature is listed as one package, so the list is short;
    # what counts is that every feature has a name and that every Enterprise
    # file of the tree is under a blocked name (the package itself is blocked
    # in `_run`).
    assert len(names) >= len(boundary["enterprise_modules"]) == 11
    blocked = names + [ENTERPRISE_PACKAGE]
    files = [f for f in python_files() if is_enterprise(f, enterprise_paths(boundary))]
    # (None in a Community checkout, which has no Enterprise package.)
    assert len(files) > 100 or not (REPO / "enterprise").is_dir()
    for rel in files:
        if rel == ENTERPRISE_ENTRY_POINT:
            # A script beside the package, not a module anything imports; the
            # package it loads is blocked.
            continue
        name = module_name(rel)
        assert any(name == b or name.startswith(b + ".") for b in blocked), rel


def test_the_unblocked_community_start_works(recorded_run):
    assert recorded_run["starts"], recorded_run["error"]


def test_the_community_start_loads_no_enterprise_module(recorded_run):
    assert recorded_run["enterprise_modules_loaded"] == []


def test_the_community_start_loads_no_enterprise_library(recorded_run):
    assert recorded_run["enterprise_libraries_loaded"] == []


def test_the_community_start_does_not_load_the_enterprise_package(recorded_run):
    assert recorded_run["enterprise_package_loaded"] == []


def test_the_community_application_starts_without_the_enterprise_edition(blocked_run):
    """Every Enterprise module, the Enterprise package and every
    Enterprise-only library refused at import: the application is built."""
    assert blocked_run["starts"], f"{blocked_run['error']} (refused: {blocked_run['first_refused']}, asked by {blocked_run['asked_by']})"
    assert blocked_run["first_refused"] is None and blocked_run["asked_by"] is None


def test_a_blocked_start_loads_nothing_of_the_enterprise_edition(blocked_run):
    assert blocked_run["enterprise_modules_loaded"] == []
    assert blocked_run["enterprise_libraries_loaded"] == []
    assert blocked_run["enterprise_package_loaded"] == []


def test_the_block_refuses(tmp_path_factory):
    """The probe's own check: a start that does import an Enterprise module
    is stopped by the block and says where."""
    result = _run("block-selftest", tmp_path_factory)
    assert result["starts"] is False
    # The module the self-test imports is `brutor_enterprise.licence.features`;
    # the import stops at the package, which is blocked.
    assert result["first_refused"] == "brutor_enterprise"

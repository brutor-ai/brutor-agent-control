# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Where the product code of this checkout is.

About thirty tests are gates over the whole source tree: they walk `app/` and
hold every file to a rule (every write is audited, every binding route
publishes an invalidation, no handler resolves a tenant by hand, ...). The
Enterprise Edition's modules live in `enterprise/brutor_enterprise/`, one
package per feature. A gate that walked `app/` alone would not look at them
and would say nothing.

`python_sources(root)` is what those tests walk: every `.py` under `root`,
and, where this checkout has the Enterprise package (an Enterprise run; a
Community checkout has none), the Enterprise files of the same kind:

* `app/`                       -> everything under `enterprise/brutor_enterprise/`
* `app/api/`, `app/api/v1/`    -> every feature's `endpoints/` and `models/`
* `app/api/v1/endpoints/...`   -> every feature's `endpoints/`
* `app/services/`              -> every feature's `services/`
* `app/database/`              -> every feature's seed modules (`seed_*.py`)

So the Community suite, run for the Enterprise Edition, holds the Enterprise
code to the same rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "app"
ENTERPRISE = REPO / "enterprise" / "brutor_enterprise"
# (There is no "does this checkout have the Enterprise package" flag here, on
# purpose: a test of the Community suite whose assertions NAME files or cases
# asserts the same in every checkout, and the Enterprise cases are in
# `enterprise/tests` (`tests/test_app_factory.py` holds it).


def _enterprise_sources(root: Path) -> List[Path]:
    if not ENTERPRISE.is_dir():
        return []
    try:
        parts = root.resolve().relative_to(APP).parts
    except ValueError:
        return []
    everything = sorted(ENTERPRISE.rglob("*.py"))
    if not parts:
        return everything

    def under(*kinds: str) -> List[Path]:
        return [p for p in everything if len(p.relative_to(ENTERPRISE).parts) > 2 and p.relative_to(ENTERPRISE).parts[1] in kinds]

    if parts in (("api",), ("api", "v1")):
        return under("endpoints", "models")
    if parts[:3] == ("api", "v1", "endpoints"):
        return under("endpoints")
    if parts[:3] == ("api", "v1", "models"):
        return under("models")
    if parts == ("services",):
        return under("services")
    if parts == ("database",):
        return [p for p in everything if p.name.startswith("seed_")]
    return []


def community_sources(root: Path) -> List[Path]:
    """Every `.py` under `root`, sorted: the Community package's alone. For a
    test of the Community suite whose assertions name files, so that what it
    asserts is the same in every checkout."""
    return sorted(Path(root).rglob("*.py"))


def enterprise_sources(root: Path) -> List[Path]:
    """The Enterprise files of the kind that `root` holds in the Community
    package (see the module text), sorted. For the Enterprise half of such a
    test, under `enterprise/tests`."""
    return _enterprise_sources(Path(root))


def python_sources(root: Path) -> List[Path]:
    """Every `.py` under `root`, sorted, then the Enterprise files of the same
    kind (see the module text), sorted."""
    root = Path(root)
    return sorted(root.rglob("*.py")) + _enterprise_sources(root)


TESTS = REPO / "tests"
ENTERPRISE_TESTS = REPO / "enterprise" / "tests"


def test_sources() -> List[Path]:
    """Every `.py` of the test trees of this checkout: the Community suite
    (`tests/`), then, where the checkout has it, the Enterprise Edition's own
    (`enterprise/tests/`).

    A gate over the test files (no test destroys the configured database, no
    test names a reachable database, no test builds the application round the
    factory, ...) walks this. One that kept walking `tests/` alone would not
    see the files under `enterprise/tests/`."""
    found = sorted(TESTS.rglob("*.py"))
    if ENTERPRISE_TESTS.is_dir():
        found += sorted(ENTERPRISE_TESTS.rglob("*.py"))
    return [path for path in found if "__pycache__" not in path.parts]


test_sources.__test__ = False  # a helper, not a test


def test_label(path: Path) -> str:
    """A test file's path as a gate names it: relative to `tests/` for the
    Community suite (as before), relative to the repository for the
    Enterprise one."""
    path = Path(path).resolve()
    try:
        return str(path.relative_to(TESTS))
    except ValueError:
        return str(path.relative_to(REPO))


test_label.__test__ = False


def source_label(path: Path) -> str:
    """A file's path as a test names it: relative to `app/` for a Community
    file (as before L4), relative to the repository for an Enterprise one."""
    path = Path(path).resolve()
    try:
        return str(path.relative_to(APP))
    except ValueError:
        return str(path.relative_to(REPO))


def admin_endpoint_module(filename: str) -> Path:
    """The source file of an admin router module, by its file name: under
    `app/api/v1/endpoints/admin/`, or (since L4) in the `endpoints/` package
    of the Enterprise feature it belongs to."""
    community = APP / "api" / "v1" / "endpoints" / "admin" / filename
    if community.exists():
        return community
    found = sorted(ENTERPRISE.glob(f"*/endpoints/{filename}")) if ENTERPRISE.is_dir() else []
    if len(found) != 1:
        raise FileNotFoundError(f"{filename}: no admin router module of that name ({len(found)} in the Enterprise package)")
    return found[0]

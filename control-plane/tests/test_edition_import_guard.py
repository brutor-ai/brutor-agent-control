# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Boundary guard: Community code does not import Enterprise code.

The boundary file lists the Enterprise modules of this tree
(`editions.yaml`, `control_plane.enterprise_modules`). Every other module is
Community, and after the split it is built and shipped without them, so an
import of one would be an `ImportError` at a customer's start-up.

Today some Community modules still import Enterprise ones: the composition
registries name the Enterprise routers, steps and services they are tagged
with. Those are written down in `tests/fixtures/edition_import_allowlist.json`
and the list is a ratchet:

* an edge that is not on the list fails (a new tangle);
* an entry whose edge is gone fails until it is deleted (so the list is
  always exactly what is left);
* the list may not be longer than `CEILING`, which is lowered with it and is
  never raised.

The guard is strict: the list is empty.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.edition_boundary import (
    REPO,
    control_plane_boundary,
    enterprise_paths,
    import_edges,
    is_enterprise,
    python_files,
)

ALLOWLIST = Path(__file__).parent / "fixtures" / "edition_import_allowlist.json"

# The number of edges when the list was last shortened. Lower it with the
# list. Never raise it: a new edge is a defect, not a new entry.
#   52  2026-11-11  L0, as found
#   50  2026-11-11  L1: the provider seed and the dead status poller no longer import (-4);
#                   the hook and the router their code moved behind are registered (+2)
#    1  2026-11-11  L3: the composition was turned round; `app/composition/` imports no Enterprise
#                   module (the 49 edges from it are gone, with the registries' Enterprise entries)
#    0  2026-11-11  L3: the boundary file lists the whole `enterprise/brutor_enterprise/policy_as_code/services/policy` package
CEILING = 0

# Of those, the edges that do not start in `app/composition/`. The registries
# there are where a Community -> Enterprise reference may live until the
# Enterprise entries leave for an extension (L3): an edge anywhere else is a
# tangle in a feature module. The one way an entry is ever added to the list
# is a composition entry that takes the place of such a tangle (the code
# moved behind a registry), and then both numbers still go down.
#   5  2026-11-11  L0, as found
#   1  2026-11-11  L1: what is left is the `enterprise/brutor_enterprise/policy_as_code/services/policy` package re-exporting its own
#                  (Enterprise) engine; the package moves as a whole (L4)
#   0  2026-11-11  L3: the boundary file lists that package as a whole
OUTSIDE_COMPOSITION_CEILING = 0

# Since L3 the registries hold no Enterprise entry: an edge from `app/composition/` or from the
# application module is never allowed again, on the list or off it.
COMPOSITION_CEILING = 0


def _allowed() -> list:
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))["edges"]


@pytest.fixture(scope="module")
def boundary():
    return control_plane_boundary()


@pytest.fixture(scope="module")
def found(boundary):
    return import_edges(boundary)


def test_no_community_module_imports_an_enterprise_module_beyond_the_list(found):
    new = sorted(set(found) - set(_allowed()))
    assert new == [], (
        "Community code imports Enterprise code (source -> target :: name). "
        "Hand over through a registry or a behaviour hook instead "
        "(app/composition/); do not add to the list:\n  " + "\n  ".join(new)
    )


def test_every_entry_of_the_list_is_still_an_edge(found):
    gone = sorted(set(_allowed()) - set(found))
    assert gone == [], (
        "these edges are gone; delete them from "
        f"{ALLOWLIST.name} and lower CEILING:\n  " + "\n  ".join(gone)
    )


def test_the_list_is_sorted_and_has_no_duplicates():
    allowed = _allowed()
    assert allowed == sorted(set(allowed))


def test_the_list_only_shrinks():
    assert len(_allowed()) <= CEILING
    # ... and the ceiling follows it down.
    assert CEILING == len(_allowed()), "lower CEILING to the length of the list"


def test_no_community_module_imports_an_enterprise_module(found):
    """Strict since L6: the list is empty and stays empty, so the rule is the
    plain one. (The tests above keep the list's own rules; with nothing on
    it they say the same thing.)"""
    assert _allowed() == []
    assert found == []
    assert CEILING == OUTSIDE_COMPOSITION_CEILING == COMPOSITION_CEILING == 0


def test_the_composition_imports_no_enterprise_module(found):
    """Hard since L3: not a ratchet. `app/app.py` and `app/composition/*` are
    what every Community start imports."""
    composition = [
        edge for edge in sorted(set(found) | set(_allowed())) if edge.startswith(("app/composition/", "app/app.py"))
    ]
    assert len(composition) == COMPOSITION_CEILING == 0, composition


def test_the_edges_outside_the_composition_only_shrink():
    outside = [edge for edge in _allowed() if not edge.startswith("app/composition/")]
    assert len(outside) == OUTSIDE_COMPOSITION_CEILING, (
        "lower OUTSIDE_COMPOSITION_CEILING to the number of edges that do not start in app/composition/ "
        "(never raise it)"
    )


# ---------------------------------------------------------------------------
# The scan sees every form of import (on a made-up tree).
# ---------------------------------------------------------------------------

_BOUNDARY = {"enterprise_modules": {"x": ["app/ent/", "app/one_ent.py"]}}


def _tree(tmp_path: Path, community_source: str) -> Path:
    files = {
        "app/__init__.py": "",
        "app/ent/__init__.py": "VALUE = 1\n",
        "app/ent/deep.py": "def f(): ...\n",
        "app/one_ent.py": "class C: ...\n",
        "app/pkg/__init__.py": "",
        "app/pkg/community.py": community_source,
        "app/pkg/neighbour.py": "X = 1\n",
    }
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "source, edge",
    [
        ("from app.ent.deep import f\n", "app/ent/deep.py :: f"),
        ("import app.ent.deep\n", "app/ent/deep.py :: *module*"),
        ("import app.ent.deep as d\n", "app/ent/deep.py :: *module*"),
        ("from app.ent import deep\n", "app/ent/deep.py :: *module*"),
        ("from app.ent import VALUE\n", "app/ent/__init__.py :: VALUE"),
        ("from app import one_ent\n", "app/one_ent.py :: *module*"),
        ("from .. import one_ent\n", "app/one_ent.py :: *module*"),
        ("from ..ent.deep import f\n", "app/ent/deep.py :: f"),
        ("from ..one_ent import C\n", "app/one_ent.py :: C"),
        ("def g():\n    from app.one_ent import C\n    return C\n", "app/one_ent.py :: C"),
        ("async def g():\n    import app.one_ent\n", "app/one_ent.py :: *module*"),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app.one_ent import C\n", "app/one_ent.py :: C"),
        ("try:\n    from app.one_ent import C\nexcept ImportError:\n    C = None\n", "app/one_ent.py :: C"),
        ("import importlib\nm = importlib.import_module('app.ent.deep')\n", "app/ent/deep.py :: *string*"),
        ("PATCH = 'app.one_ent.C'\n", "app/one_ent.py :: *string*"),
    ],
)
def test_the_scan_sees_this_form(tmp_path, source, edge):
    root = _tree(tmp_path, source)
    assert import_edges(_BOUNDARY, root) == [f"app/pkg/community.py -> {edge}"]


@pytest.mark.parametrize(
    "source",
    [
        "from app.pkg.neighbour import X\n",
        "from . import neighbour\n",
        "import os\nimport app.pkg.neighbour\n",
        "NOTE = 'see app/ent/deep.py for the Enterprise side'\n",
        "TEXT = 'app.enterprise is not a module here'\n",
    ],
)
def test_the_scan_does_not_report_this(tmp_path, source):
    assert import_edges(_BOUNDARY, _tree(tmp_path, source)) == []


def test_an_enterprise_module_may_import_another(tmp_path):
    root = _tree(tmp_path, "X = 1\n")
    (root / "app/ent/deep.py").write_text("from app.one_ent import C\n", encoding="utf-8")
    assert import_edges(_BOUNDARY, root) == []


def test_the_scan_covers_the_community_tree(boundary):
    """The Community half (the Enterprise Edition's, which also counts its
    own files, is in `enterprise/tests/test_edition_import_guard.py`)."""
    files = python_files()
    community = [f for f in files if not is_enterprise(f, enterprise_paths(boundary))]
    assert len(community) > 500 and "main.py" in community and "app/cli.py" in community and "app/app.py" in files

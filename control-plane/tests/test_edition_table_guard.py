# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Boundary guard: Community code does not use the Enterprise-only tables.

The models of every table stay in the Community package (one schema), but the
tables listed as Enterprise-only (`editions.yaml`,
`control_plane.enterprise_only_tables`) are written by Enterprise features
alone. A Community module that queries one is either a tangle left to undo,
or one of the few places that must see every table whatever the edition:
the AI System closure, coverage and governance metrics, erasure, retention
and the edition guard itself. Those are *permanent*, and each is listed with
its reason in the boundary file (`control_plane.enterprise_table_readers`).

`tests/fixtures/edition_table_allowlist.json` holds every Community file that
uses such a table today, and what it uses. It is a ratchet:

* a file, or a use within a file, that is not on the list fails;
* an entry that is no longer true fails until it is deleted;
* the number of files that are not permanent may not exceed `CEILING`, which
  is lowered with the list and never raised;
* `permanent` is set for exactly the files the boundary file gives a reason
  for.

What the scan can see is described in `tests/edition_boundary.py`
(`table_uses`). It cannot see a table reached through a name built at run
time; the modules that do that on purpose read the table list from
`app/composition/edition.py`, which the scan reports (`map:`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.edition_boundary import (
    REPO,
    control_plane_boundary,
    enterprise_model_names,
    enterprise_only_tables,
    enterprise_relationships,
    table_uses,
)

ALLOWLIST = Path(__file__).parent / "fixtures" / "edition_table_allowlist.json"

# Files on the list that are NOT permanent. Every file left on the list is
# permanent, so it is 0; never raise it.
CEILING = 0


def _allowed() -> dict:
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))["files"]


@pytest.fixture(scope="module")
def boundary():
    return control_plane_boundary()


@pytest.fixture(scope="module")
def found(boundary):
    return table_uses(boundary)


def test_every_enterprise_only_table_has_a_name_the_scan_looks_for(boundary):
    """A table with neither a model class nor an association table in the
    model package could only be reached by SQL text; today there is none."""
    names = enterprise_model_names(boundary)
    assert sorted(set(enterprise_only_tables(boundary)) - set(names.values())) == []


def test_the_relationships_into_enterprise_only_tables_are_these(boundary):
    # A new one is a new way for Community code to reach an Enterprise table
    # without naming it.
    assert enterprise_relationships(boundary) == {
        "cache_configs": "resource_group_cache_configs",
        "connector": "kb_connectors",
        "docker_deployment": "docker_deployments",
        "ecs_deployment": "ecs_deployments",
        "kb_collections": "resource_group_kb_collections",
        "kb_connectors": "resource_group_kb_connectors",
        "kubernetes_deployment": "kubernetes_deployments",
        "prompt_cache_policies": "resource_group_prompt_cache_policies",
    }


def test_no_community_file_uses_an_enterprise_only_table_beyond_the_list(found):
    allowed = _allowed()
    new = {
        rel: sorted(set(uses) - set(allowed.get(rel, {}).get("uses", [])))
        for rel, uses in found.items()
        if set(uses) - set(allowed.get(rel, {}).get("uses", []))
    }
    assert new == {}, (
        "Community code uses an Enterprise-only table. Move the code to the "
        "Enterprise side or hand over through a registry; do not add to the list:\n  "
        + "\n  ".join(f"{rel}: {uses}" for rel, uses in sorted(new.items()))
    )


def test_every_entry_of_the_list_is_still_true(found):
    gone = {}
    for rel, entry in _allowed().items():
        missing = sorted(set(entry["uses"]) - set(found.get(rel, [])))
        if missing:
            gone[rel] = missing
    assert gone == {}, (
        f"no longer true; delete from {ALLOWLIST.name} (the whole file entry when "
        "nothing is left) and lower CEILING:\n  "
        + "\n  ".join(f"{rel}: {uses}" for rel, uses in sorted(gone.items()))
    )


def test_the_list_is_well_formed():
    allowed = _allowed()
    assert list(allowed) == sorted(allowed)
    for rel, entry in allowed.items():
        assert set(entry) == {"uses", "permanent"}, rel
        assert entry["uses"] and entry["uses"] == sorted(set(entry["uses"])), rel
        assert isinstance(entry["permanent"], bool), rel
        assert (REPO / rel).exists(), rel


def test_the_list_only_shrinks():
    not_permanent = [rel for rel, entry in _allowed().items() if not entry["permanent"]]
    assert len(not_permanent) <= CEILING
    assert CEILING == len(not_permanent), "lower CEILING to the number of files that are not permanent"


def test_every_file_on_the_list_is_permanent():
    """Strict since L6: the list holds no temporary entry. A Community file
    that names an Enterprise-only table is either one of the readers the
    boundary file gives a reason for, or a defect."""
    assert sorted(rel for rel, entry in _allowed().items() if not entry["permanent"]) == []
    assert CEILING == 0


def test_permanent_means_the_boundary_file_gives_the_reason(boundary):
    """Community code may read or delete rows of
    Enterprise-only tables for a written allow-list only."""
    readers = boundary["enterprise_table_readers"]
    for rel, reason in readers.items():
        assert (REPO / rel).exists(), rel
        assert isinstance(reason, str) and len(reason.split()) >= 5, f"{rel}: say why"
    allowed = _allowed()
    permanent = sorted(rel for rel, entry in allowed.items() if entry["permanent"])
    # Every permanent entry has a reason...
    assert sorted(set(permanent) - set(readers)) == []
    # ... and a file with a reason that the scan reports is marked permanent.
    assert sorted(rel for rel in readers if rel in allowed and not allowed[rel]["permanent"]) == []


def test_a_permanent_file_only_reads_what_its_reason_covers(found):
    """A permanent file is not a free pass for the system administrator's
    tables or anything else that turns up in it later: its uses are pinned
    like everyone else's (the two tests above); this one states it."""
    allowed = _allowed()
    for rel, entry in allowed.items():
        if entry["permanent"]:
            assert set(found.get(rel, [])) == set(entry["uses"]), rel


# ---------------------------------------------------------------------------
# The scan sees every form of use (on a made-up tree).
# ---------------------------------------------------------------------------

_BOUNDARY = {
    "enterprise_modules": {"x": ["app/ent.py"]},
    "enterprise_only_tables": {"x": ["ent_rows", "group_ent_rows"]},
}

_MODELS = '''
from sqlalchemy import Column, String, Table
from sqlalchemy.orm import relationship

group_ent_rows = Table("group_ent_rows", None)


class EntRow:
    __tablename__ = "ent_rows"


class Group:
    __tablename__ = "groups"
    ent_rows = relationship("EntRow", secondary=group_ent_rows)
    owner = relationship("Owner")
'''


def _tree(tmp_path: Path, community_source: str) -> Path:
    files = {
        "app/__init__.py": "",
        "app/ent.py": "from app.database.models.things import EntRow\nq = EntRow\n",
        "app/database/__init__.py": "",
        "app/database/models/__init__.py": "",
        "app/database/models/things.py": _MODELS,
        "app/composition/__init__.py": "",
        "app/composition/edition.py": "ENTERPRISE_ONLY_TABLES = {}\n\ndef enterprise_only_tables(): ...\n",
        "app/community.py": community_source,
    }
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "source, uses",
    [
        ("from app.database.models.things import EntRow\n", ["import:EntRow"]),
        ("from app.database.models.things import EntRow\ndef f(x: EntRow) -> None: ...\n", ["import:EntRow"]),
        ("from app.database.models.things import EntRow\ndef f(db):\n    return db.query(EntRow)\n", ["model:EntRow"]),
        ("def f(db):\n    from app.database.models.things import EntRow as E\n    return db.query(E)\n", ["model:EntRow"]),
        ("from app.database.models import things\ndef f(db):\n    return db.query(things.EntRow)\n", ["model:EntRow"]),
        ("from app.database.models.things import group_ent_rows\nx = group_ent_rows.c\n", ["model:group_ent_rows"]),
        ("from sqlalchemy import text\nQ = text('SELECT count(*) FROM ent_rows WHERE x = 1')\n", ["sql:ent_rows"]),
        ("Q = 'delete from \"ent_rows\"'\n", ["sql:ent_rows"]),
        ("def f(t):\n    return f'UPDATE ent_rows SET a = {t}'\n", ["sql:ent_rows"]),
        ("Q = 'select 1 from groups g join public.group_ent_rows r on true'\n", ["sql:group_ent_rows"]),
        ("def f(group):\n    return list(group.ent_rows)\n", ["relationship:ent_rows"]),
        ("def f(group):\n    group.ent_rows = []\n", ["relationship:ent_rows"]),
        ("from app.composition.edition import enterprise_only_tables\n", ["map:enterprise_only_tables"]),
        ("from app.composition.edition import ENTERPRISE_ONLY_TABLES\n", ["map:ENTERPRISE_ONLY_TABLES"]),
    ],
)
def test_the_scan_sees_this_use(tmp_path, source, uses):
    assert table_uses(_BOUNDARY, _tree(tmp_path, source)) == {"app/community.py": uses}


@pytest.mark.parametrize(
    "source",
    [
        '"""Rows of ent_rows are deleted from ent_rows by Enterprise."""\n',
        "NAME = 'ent_rows'\n",
        "Q = 'select 1 from groups'\n",
        "Q = 'select 1 from ent_rows_archive'\n",
        "def f(group):\n    return group.owner\n",
    ],
)
def test_the_scan_does_not_report_this(tmp_path, source):
    assert table_uses(_BOUNDARY, _tree(tmp_path, source)) == {}

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Gate: no Python file of this repository uses a name it neither defines nor
imports.

A move of the Enterprise code dropped `from app.database.models.admin import
TenantAdmin` from `app/services/compliance/resolvers/oversight.py`; two
compliance statements failed on every evaluation with a NameError that the
resolver runner turns into "could not be evaluated", and nothing failed. The
same search then found two more that only fail when a particular request
arrives (free text on `/proxy-logs/facets`; a policy dry run that
lists machines or connectors for an existing group).

Python finds an undefined name when the line runs, so a test only catches one
on a line it reaches. This gate reads every file instead (pyflakes: undefined
name, undefined local, a name in `__all__` that does not exist) — the
product code of both editions, the migrations, the entry points, the scripts
and both test suites.

A name used only in an annotation counts: import it, under `TYPE_CHECKING`
where importing it at run time would be a cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
from pyflakes import checker, messages
import ast

from tests.source_trees import APP, ENTERPRISE_TESTS, REPO, TESTS, python_sources

UNDEFINED = (messages.UndefinedName, messages.UndefinedLocal, messages.UndefinedExport)
EXCLUDED_DIRS = {".venv", ".venv-test", "worktrees", "mcp-server-builds", "__pycache__", ".git"}


def _files() -> List[Path]:
    files = list(python_sources(APP))
    for root in (REPO / "alembic", REPO / "scripts", TESTS, ENTERPRISE_TESTS):
        if root.is_dir():
            files += sorted(root.rglob("*.py"))
    files += sorted(REPO.glob("*.py"))
    enterprise = REPO / "enterprise"
    if enterprise.is_dir():
        files += sorted(enterprise.glob("*.py"))
    return [f for f in files if not EXCLUDED_DIRS & set(f.relative_to(REPO).parts)]


def undefined_names(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = checker.Checker(tree, filename=str(path)).messages
    return [
        f"{path.relative_to(REPO) if path.is_relative_to(REPO) else path}:{m.lineno}: {m.message % m.message_args}"
        for m in sorted(found, key=lambda m: m.lineno)
        if isinstance(m, UNDEFINED)
    ]


def test_the_walk_covers_the_trees():
    files = {f.relative_to(REPO).as_posix() for f in _files()}
    assert "app/services/compliance/resolvers/oversight.py" in files
    assert "main.py" in files and "tests/test_undefined_names.py" in files
    assert any(f.startswith("alembic/versions/") for f in files)
    # The Community trees alone (1,169 files when this was set, the history
    # being one baseline revision); the
    # Enterprise half (`enterprise/tests/test_undefined_names.py`) holds the
    # walk to the files under `enterprise/` where they are.
    community = [f for f in files if not f.startswith("enterprise/")]
    assert len(community) > 1150, len(community)


def test_no_file_uses_an_undefined_name():
    problems = [line for f in _files() for line in undefined_names(f)]
    assert not problems, "undefined names:\n" + "\n".join(problems)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("def f(db):\n    return db.query(TenantAdmin)\n", "undefined name 'TenantAdmin'"),
        ("def f():\n    x: Dict[str, int] = {}\n    return x\n", "undefined name 'Dict'"),
        ("def f(a: 'Missing') -> None:\n    pass\n", "undefined name 'Missing'"),
        ("__all__ = ['nothing_here']\n", "undefined name 'nothing_here' in __all__"),
    ],
)
def test_the_gate_sees_what_it_is_for(tmp_path, source, expected):
    """The checker is not vacuous: each shape of the defect is reported."""
    f = tmp_path / "sample.py"
    f.write_text(source)
    assert any(expected in line for line in undefined_names(f)), undefined_names(f)

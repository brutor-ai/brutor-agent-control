# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Every ``server_default`` a migration declares must also be on the ORM column.

Fresh installs build the schema with ``create_all`` (the ORM), upgrades
build it with Alembic. When a migration sets a database-side default
and the model only sets a Python-side ``default=``, the two kinds of
install diverge: an ``INSERT`` that does not name the column — the Rust
core, raw SQL, a test fixture — succeeds on a migrated database and
fails with a NOT NULL violation on a fresh one. That is how
``audit_chain_prunes.pruned_at`` and nine assurance columns drifted
(found via the core's regenerated schema fixture), and it is the
same class as the check-results index predicate drift fixed by
``a165_ckr_index_predicate``.

The migrations are parsed with ``ast`` — ``op.create_table`` and
``op.add_column`` columns carrying ``server_default=`` (anything but
``None``) — and each surviving column is looked up on ``Base.metadata``.
A later ``op.alter_column(..., server_default=None)`` releases a column.
"""

import ast
import glob
import os

import pytest

import app.database.models  # noqa: F401  (registers every model on Base)
from app.database import Base

VERSIONS = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions")


def _str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _column_name(call):
    """``sa.Column("name", ...)`` → ``"name"`` (also bare ``Column``)."""
    if not isinstance(call, ast.Call):
        return None
    fn = call.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
    if name != "Column" or not call.args:
        return None
    return _str(call.args[0])


def _has_server_default(call):
    for kw in call.keywords:
        if kw.arg == "server_default":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


def declared_server_defaults():
    """{(table, column)} declared with a server default across all migrations,
    minus any later explicitly cleared."""
    declared, cleared = set(), set()
    for path in sorted(glob.glob(os.path.join(VERSIONS, "*.py"))):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            op = node.func.attr
            if op == "create_table" and node.args:
                table = _str(node.args[0])
                for col in node.args[1:]:
                    name = _column_name(col)
                    if table and name and _has_server_default(col):
                        declared.add((table, name))
            elif op == "add_column" and len(node.args) >= 2:
                table, col = _str(node.args[0]), node.args[1]
                name = _column_name(col)
                if table and name and _has_server_default(col):
                    declared.add((table, name))
            elif op == "alter_column" and len(node.args) >= 2:
                table, name = _str(node.args[0]), _str(node.args[1])
                for kw in node.keywords:
                    if kw.arg == "server_default" and isinstance(kw.value, ast.Constant) and kw.value.value is None:
                        cleared.add((table, name))
    return declared - cleared


def test_migrations_declare_server_defaults_somewhere():
    assert len(declared_server_defaults()) > 50, "the migration scan found almost nothing — parser broken?"


def test_every_migrated_server_default_is_on_the_orm_column():
    tables = Base.metadata.tables
    missing = []
    for table, column in sorted(declared_server_defaults()):
        t = tables.get(table)
        if t is None or column not in t.columns:
            continue  # table or column since dropped/renamed; nothing to mirror
        if t.columns[column].server_default is None:
            missing.append(f"{table}.{column}")
    assert missing == [], (
        "These columns get a database default on upgraded installs but not on "
        "fresh ones (create_all); add the matching `server_default=` to the model:\n  "
        + "\n  ".join(missing)
    )

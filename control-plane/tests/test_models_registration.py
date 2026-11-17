# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Guard-rail tests for ``app/database/models``.

Background
----------
Tables are created at boot via ``Base.metadata.create_all()`` (see
``app/database/__init__.py`` ``init_database``). SQLAlchemy only knows
about a table when its model class has been *imported* — declaring the
class adds its ``__table__`` to the shared ``Base.metadata``. So the
mechanism that decides "does this table exist on a fresh DB?" is the
list of top-level imports in ``app/database/models/__init__.py``.

The bug pattern we're guarding against
--------------------------------------
A developer adds a new model file (e.g. ``kb_document.py`` with
``__tablename__ = "kb_documents"``), wires it into routes and
services, but forgets to add the import line to
``app/database/models/__init__.py``. Locally everything works because
their existing dev DB still has the table from a previous build, and
the import side-effect happens transitively through service imports.
But on a *fresh* DB (CI, a new dev, a re-provisioned trial), the table
is silently never created — and any background poller hitting it
crashes with ``relation … does not exist``.

These tests catch that at the module level so the bug can never reach
``main`` undetected:

  1. Every ``*.py`` model file with at least one declarative class is
     imported by ``models/__init__.py`` (so its ``__table__`` lands in
     ``Base.metadata``).
  2. Every class declared in ``models/__init__.py`` is also re-exported
     via ``__all__`` — keeps wildcard-imports honest and surfaces the
     "added to imports but not to __all__" oversight that started this.
  3. Tables that the gateway demonstrably needs to exist on a fresh DB
     (the ones whose absence broke pollers in incident-2026-04) are
     present in ``Base.metadata`` by name. A regression test in case
     someone removes an import.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Set

import pytest

import app.database.models as models_pkg
from app.database import Base


_MODELS_DIR = Path(models_pkg.__file__).parent
_INIT_FILE = _MODELS_DIR / "__init__.py"


def _model_module_files() -> list[Path]:
    """Every ``app/database/models/*.py`` file that *defines* at least
    one SQLAlchemy declarative class. We exclude ``__init__.py`` (it's
    the importer, not a model file) and any file that has no class
    definitions at all (e.g. constants-only modules)."""
    files = []
    for path in sorted(_MODELS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        # A "model file" = has at least one ClassDef. Cheap heuristic;
        # avoids picking up future utility modules that don't define
        # tables. If a file has classes but none are SQLAlchemy models,
        # the import-presence test below still requires the import,
        # which is a fine convention.
        if any(isinstance(node, ast.ClassDef) for node in tree.body):
            files.append(path)
    return files


def _imported_module_names_in_init() -> Set[str]:
    """Parse ``models/__init__.py`` and return the set of submodule
    names it imports from (``app.database.models.<name>``).

    Using AST rather than a regex so future formatting changes (line
    continuations, parenthesised lists, conditional imports) don't
    silently fool the test.
    """
    tree = ast.parse(_INIT_FILE.read_text())
    found: Set[str] = set()
    prefix = "app.database.models."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix):
            found.add(node.module[len(prefix):])
    return found


def _all_names_from_init() -> Set[str]:
    """The literal contents of ``__all__`` in ``models/__init__.py``.

    Returns an empty set if ``__all__`` is missing or not a literal
    list — this is itself a failure mode the test catches.
    """
    tree = ast.parse(_INIT_FILE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        return set()
                    if isinstance(value, (list, tuple)):
                        return {str(v) for v in value}
    return set()


def _names_imported_in_init() -> Set[str]:
    """Every symbol bound by an ``ImportFrom app.database.models.…``
    statement at module top level. This is the union of:

      * the imported names themselves
      * any aliases (``import X as Y`` → both X and Y are eligible
        re-exports; we record Y because that's what's bound).

    Used to cross-check ``__all__``: every locally-bound name
    should be either listed in ``__all__`` or be a clearly-private
    constant (we don't enforce that part — leading-underscore checks
    catch nothing the existing convention isn't already handling).
    """
    tree = ast.parse(_INIT_FILE.read_text())
    bound: Set[str] = set()
    prefix = "app.database.models."
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
    return bound


# ---------------------------------------------------------------------------
# 1. Every model file is imported by models/__init__.py
# ---------------------------------------------------------------------------

def test_every_model_file_is_imported_by_init():
    """If a model file isn't imported, its ``__tablename__`` never
    registers with ``Base.metadata`` and ``create_all()`` silently
    skips it on fresh DBs. This test fails the build before that bug
    can ship."""
    expected = {p.stem for p in _model_module_files()}
    actual = _imported_module_names_in_init()
    missing = expected - actual
    assert not missing, (
        f"Model files exist on disk but are not imported in "
        f"app/database/models/__init__.py: {sorted(missing)}. "
        "Add `from app.database.models.<module> import <Class>` so "
        "Base.metadata.create_all() will build the table on fresh DBs."
    )


# ---------------------------------------------------------------------------
# 2. Every imported name appears in __all__
# ---------------------------------------------------------------------------

def test_imported_names_are_in_all():
    """``__all__`` is the public surface. A name imported into the
    package but missing from ``__all__`` is a foot-gun: ``from
    app.database.models import *`` silently skips it, IDE auto-import
    can't find it, and discovery tools (alembic autogenerate, schema
    reflection helpers) that walk ``__all__`` quietly drop the model.

    Allowlist below: a few names are deliberately not exported (e.g.
    constants used only by their parent module). Add new names here
    only with a justifying comment.
    """
    bound = _names_imported_in_init()
    declared = _all_names_from_init()
    missing = bound - declared

    # Names that are intentionally bound for side-effects (so the
    # model class registers with Base.metadata) but not part of the
    # package's public API. Empty by default — every entry needs a
    # comment in this list explaining why it's skipped.
    intentionally_not_exported: Set[str] = set()

    really_missing = missing - intentionally_not_exported
    assert not really_missing, (
        f"Names imported into app/database/models/__init__.py but absent "
        f"from __all__: {sorted(really_missing)}. Add them to __all__ or, "
        "if they're internal-only side-effect imports, list them in this "
        "test's `intentionally_not_exported` set with a comment."
    )


# ---------------------------------------------------------------------------
# 3. Critical tables are registered with Base.metadata
# ---------------------------------------------------------------------------

# Tables whose absence has previously broken the gateway — pollers
# that hit them log a stream of `relation does not exist` errors and
# silently disable the corresponding feature. Anchor them as a
# regression test so removing an import (the only way these can drop
# off Base.metadata) breaks CI rather than production.
_CRITICAL_TABLES = {
    # KB / RAG ingestion pipeline
    "kb_collections",
    "kb_documents",
    "kb_chunks",
    # Tool approval expiry sweeper (Rust proxy)
    "tool_approval_requests",
    # Skill execution + agentic flows
    "agent_skill_executions",
    # Batch job poller
    "batch_jobs",
    # Resource health alert reconciliation
    "system_alerts",
    # Semantic cache configuration
    "semantic_cache_configs",
    "semantic_cache_tool_rules",
    # Multimodal Files API. Without this table the
    # gateway-core image/audio routes have nowhere to register the
    # generated asset and fall back to embedding base64 in the chat
    # stream, which breaks reload-render and bloats proxy_logs.
    "media_files",
    # CIMD OAuth bridge (`a126_oauth_cimd_clients`). Shipped as an Alembic-only table with no
    # model, so upgraded DBs had it and every *fresh* install silently
    # didn't — the authorize bridge died on `relation does not exist`.
    # See test_migration_created_tables_have_models below.
    "oauth_cimd_clients",
}


def test_critical_tables_are_in_base_metadata():
    """Pin the set of tables the gateway depends on at boot. Each
    entry here was added because its absence caused a real incident."""
    have = {t.name for t in Base.metadata.tables.values()}
    missing = _CRITICAL_TABLES - have
    assert not missing, (
        f"Critical tables missing from Base.metadata: {sorted(missing)}. "
        "Their model files exist but their `from … import` line in "
        "app/database/models/__init__.py was probably removed. Restore "
        "the import; fresh-DB deployments will silently skip these "
        "tables otherwise."
    )


# ---------------------------------------------------------------------------
# 4. Every model has __tablename__
# ---------------------------------------------------------------------------

def test_every_declarative_class_has_tablename():
    """Sanity check on the model files themselves: every direct
    subclass of ``Base`` must declare ``__tablename__``. Catches the
    typo case where someone copies a model file and forgets to rename
    the table — they'd silently inherit the original's tablename and
    SQLAlchemy would produce a confusing duplicate-mapping error at
    import time, not here. This test localises the failure to the
    offending file."""
    # Walk all declarative classes registered with Base.
    missing: list[str] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        # `__tablename__` is required for declarative classes that
        # aren't abstract. Abstract bases have `__abstract__ = True`.
        if getattr(cls, "__abstract__", False):
            continue
        if not getattr(cls, "__tablename__", None):
            missing.append(f"{cls.__module__}.{cls.__name__}")
    assert not missing, (
        f"Declarative classes without __tablename__: {missing}. "
        "Add `__tablename__ = \"<plural_snake_case>\"` so the table "
        "registers on Base.metadata."
    )


# ---------------------------------------------------------------------------
# 5. Every table an Alembic migration creates also has a model
# ---------------------------------------------------------------------------

_ALEMBIC_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _tables_created_by_migrations() -> Set[str]:
    """Net set of tables the migration chain creates.

    Only ``upgrade()`` is parsed. Every migration's ``downgrade()``
    mirrors its creates with ``op.drop_table``, so walking the whole
    module would cancel out every table and make this test vacuous.

    A table created by one revision and dropped by a later one is
    subtracted, so retired tables don't demand a model.
    """
    created: Set[str] = set()
    dropped: Set[str] = set()

    for path in sorted(_ALEMBIC_VERSIONS_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for func in tree.body:
            if not (isinstance(func, ast.FunctionDef) and func.name == "upgrade"):
                continue

            # Table names are not always literals at the call site.
            # `a119_drop_ai_systems` retires the ai_systems entity with
            #     for tbl in ("ai_system_members", ...): op.drop_table(tbl)
            # so resolve loop variables bound to a literal sequence first,
            # otherwise those drops are invisible and the test reports
            # long-retired tables as missing models.
            loop_vars: dict[str, Set[str]] = {}
            for node in ast.walk(func):
                if not (isinstance(node, ast.For) and isinstance(node.target, ast.Name)):
                    continue
                try:
                    values = ast.literal_eval(node.iter)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(values, (list, tuple, set)):
                    strings = {v for v in values if isinstance(v, str)}
                    if strings:
                        loop_vars.setdefault(node.target.id, set()).update(strings)

            def _table_names(call: ast.Call) -> Set[str]:
                if not call.args:
                    return set()
                arg = call.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return {arg.value}
                if isinstance(arg, ast.Name):
                    return set(loop_vars.get(arg.id, ()))
                return set()

            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr == "create_table":
                    created |= _table_names(node)
                elif node.func.attr == "drop_table":
                    dropped |= _table_names(node)

    return created - dropped


def test_migration_created_tables_have_models():
    """A table that exists only in ``alembic/versions/`` is created on
    *upgraded* databases and silently missing on *fresh* ones.

    ``init_database`` takes two different paths: an existing DB gets the
    migration chain applied, but a fresh DB is built with
    ``Base.metadata.create_all()`` and then simply *stamped* at head —
    the migrations are never replayed. So ``create_all`` is the only
    thing that builds tables on a new install, and it only knows about
    tables that have a model.

    This is how ``oauth_cimd_clients`` (``a126_oauth_cimd_clients``) shipped: fine on every
    developer and staging DB, missing on every fresh install and trial
    bundle, with the CIMD OAuth bridge failing at runtime.

    Adding a model for the table fixes both paths. The model must mirror
    the migration exactly (same columns, constraints, and nullability),
    or the two paths produce subtly different schemas.
    """
    from_migrations = _tables_created_by_migrations()
    in_metadata = {t.name for t in Base.metadata.tables.values()}

    # Tables deliberately owned by something other than these models.
    # Every entry needs a comment saying who does own it.
    externally_owned: Set[str] = {
        # Alembic's own bookkeeping table — created by Alembic itself,
        # never by a model.
        "alembic_version",
    }

    missing = from_migrations - in_metadata - externally_owned
    assert not missing, (
        f"Tables created by Alembic migrations with no SQLAlchemy model: "
        f"{sorted(missing)}. Fresh installs build the schema with "
        "create_all() and then stamp the Alembic head, so these tables "
        "will NOT exist on a new deployment even though the migration "
        "chain reports head. Add a model under app/database/models/ that "
        "mirrors the migration exactly, or list the table in this test's "
        "`externally_owned` set with a comment naming its real owner."
    )


# ---------------------------------------------------------------------------
# 6. Every COLUMN an Alembic migration adds also exists on the model
# ---------------------------------------------------------------------------


def _columns_added_by_migrations(versions_dir: Path = _ALEMBIC_VERSIONS_DIR) -> dict[str, Set[str]]:
    """Net ``{table: {column, ...}}`` the migration chain adds after
    ``create_table``.

    Only ``upgrade()`` is parsed, for the same reason as the table-level
    scan above: every ``downgrade()`` mirrors its adds with
    ``op.drop_column``, so walking the whole module cancels them out.

    Renames are followed (``op.alter_column(..., new_column_name=...)``)
    and later drops subtract, so a column added and then retired does not
    demand a model attribute.
    """
    added: dict[str, Set[str]] = {}

    for path in sorted(versions_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue

        # Column names are not always literals at the call site.
        # `a094_llm_prune_catalog_defaults` prunes
        # eight dead llm_model columns with
        #     DROPPED_LLM_MODEL_COLUMNS = ("cost_per_audio_second", ...)
        #     for col in DROPPED_LLM_MODEL_COLUMNS: op.drop_column("llm_model", col)
        # so module-level string sequences have to be resolved first, or those
        # drops are invisible and long-retired columns get reported as missing
        # from the model. Same problem the table-level scan solves for
        # `for tbl in (...): op.drop_table(tbl)`.
        module_consts: dict[str, Set[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            try:
                values = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if not isinstance(values, (list, tuple, set)):
                continue
            strings = {v for v in values if isinstance(v, str)}
            for target in node.targets:
                if isinstance(target, ast.Name) and strings:
                    module_consts[target.id] = strings

        for func in tree.body:
            if not (isinstance(func, ast.FunctionDef) and func.name == "upgrade"):
                continue

            loop_vars: dict[str, Set[str]] = {}
            for node in ast.walk(func):
                if not (isinstance(node, ast.For) and isinstance(node.target, ast.Name)):
                    continue
                if isinstance(node.iter, ast.Name):
                    names = module_consts.get(node.iter.id)
                    if names:
                        loop_vars.setdefault(node.target.id, set()).update(names)
                    continue
                try:
                    values = ast.literal_eval(node.iter)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(values, (list, tuple, set)):
                    strings = {v for v in values if isinstance(v, str)}
                    if strings:
                        loop_vars.setdefault(node.target.id, set()).update(strings)

            def _col_names(arg: ast.expr) -> Set[str]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    return {arg.value}
                if isinstance(arg, ast.Name):
                    return set(loop_vars.get(arg.id, ()))
                return set()

            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                op = node.func.attr
                if op not in {"add_column", "drop_column", "alter_column"}:
                    continue
                if not node.args:
                    continue
                table_arg = node.args[0]
                if not (isinstance(table_arg, ast.Constant) and isinstance(table_arg.value, str)):
                    continue
                table = table_arg.value

                if op == "add_column":
                    # op.add_column("t", sa.Column("name", ...))
                    if len(node.args) < 2 or not isinstance(node.args[1], ast.Call):
                        continue
                    col_args = node.args[1].args
                    if col_args and isinstance(col_args[0], ast.Constant):
                        added.setdefault(table, set()).add(col_args[0].value)

                elif op == "drop_column":
                    if len(node.args) >= 2:
                        for name in _col_names(node.args[1]):
                            added.setdefault(table, set()).discard(name)

                else:  # alter_column — only a rename changes the name
                    new_name = next(
                        (kw.value.value for kw in node.keywords
                         if kw.arg == "new_column_name"
                         and isinstance(kw.value, ast.Constant)),
                        None,
                    )
                    if new_name is None:
                        continue
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        added.setdefault(table, set()).discard(node.args[1].value)
                    added.setdefault(table, set()).add(new_name)

    return added


def test_migration_added_columns_exist_on_models():
    """A column that exists only in ``alembic/versions/`` breaks fresh
    installs exactly the way a missing *table* does — but more quietly.

    The table-level test above ends by saying the model "must mirror the
    migration exactly". This is the test that actually enforces it.

    Why it matters more than it looks: an upgraded database gets the
    column from the migration, and the Rust core's integration fixture is
    a ``pg_dump`` of an Alembic-applied database — so both CI and every
    developer machine have the column. Only a *fresh* install lacks it,
    and nothing errors until the first write reaches the missing column.
    The proxy's audit writer is fire-and-forget, so that write fails
    silently and the audit trail is simply empty.

    This is how ``proxy_logs.turn_id`` / ``turn_seq`` (``a142_turn_layer``) shipped:
    present in the migration, in the Rust struct and in the writer's
    INSERT, absent from the SQLAlchemy model. Every governed call on a
    new deployment would have failed to record.
    """
    from_migrations = _columns_added_by_migrations()

    missing: list[str] = []
    for table, columns in sorted(from_migrations.items()):
        model_table = Base.metadata.tables.get(table)
        if model_table is None:
            # A table with no model at all is the other test's problem;
            # reporting it here too would just duplicate the failure.
            continue
        model_columns = {c.name for c in model_table.columns}
        for column in sorted(columns - model_columns):
            missing.append(f"{table}.{column}")

    assert not missing, (
        f"Columns added by Alembic migrations with no matching model "
        f"attribute: {missing}. Fresh installs build the schema with "
        "create_all() from the models and then stamp the Alembic head, so "
        "these columns will NOT exist on a new deployment even though "
        "`alembic current` reports head. Add the column to the model under "
        "app/database/models/, mirroring the migration's type and "
        "nullability exactly."
    )


def test_column_parity_scan_is_not_vacuous(tmp_path):
    """The scan above passes trivially if the AST walk stops matching.

    The schema starts from one baseline revision that creates every table
    (no ``op.add_column``), so the history alone cannot prove the scan
    works: it is run on a planted revision that adds, renames and drops.
    """
    (tmp_path / "x001_planted.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "def upgrade():\n"
        "    op.add_column('planted', sa.Column('kept', sa.Integer()))\n"
        "    op.add_column('planted', sa.Column('old_name', sa.Integer()))\n"
        "    op.alter_column('planted', 'old_name', new_column_name='new_name')\n"
        "    op.add_column('planted', sa.Column('gone', sa.Integer()))\n"
        "    op.drop_column('planted', 'gone')\n"
        "def downgrade():\n"
        "    op.drop_column('planted', 'kept')\n"
    )
    assert _columns_added_by_migrations(tmp_path) == {"planted": {"kept", "new_name"}}


# ---------------------------------------------------------------------------
# 7. Every named INDEX an Alembic migration creates also exists in a model
# ---------------------------------------------------------------------------
#
# Same fresh-install hazard as tables and columns: `op.create_index("ix_foo",
# ...)` runs on upgraded databases, but a fresh install is built from
# `Base.metadata.create_all()` and stamped at head — so an index that lives
# only in a migration is silently absent on new deployments. That is how
# `ix_usage_logs_source_created` (`a095_traffic_data_import`) shipped: present everywhere it had
# been upgraded, missing on every fresh install, turning source-filtered
# usage queries into sequential scans.
#
# A model expresses an index either in `__table_args__` (`Index("ix_...", ...)`)
# or as `Column(..., index=True)`, which SQLAlchemy auto-names `ix_<table>_<col>`.
# Both land in `Base.metadata`, so we compare against the metadata's index
# names directly rather than re-deriving them.


def _indexes_created_by_migrations() -> dict[str, str]:
    """Net ``{index_name: table_name}`` the migration chain creates.

    Only ``upgrade()`` is parsed (``downgrade()`` mirrors every create with
    ``drop_index``). An index created and later dropped is subtracted. The
    table each index sits on is captured too, so callers can drop indexes
    whose table is itself net-retired (Postgres drops an index with its
    table, without an explicit ``drop_index``).
    """
    created: dict[str, str] = {}
    dropped: Set[str] = set()

    for path in sorted(_ALEMBIC_VERSIONS_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue

        # Module-level string constants (e.g. `_TABLE = "audit_..."`).
        module_consts: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                try:
                    val = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(val, str):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            module_consts[tgt.id] = val

        for func in tree.body:
            if not (isinstance(func, ast.FunctionDef) and func.name == "upgrade"):
                continue
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in {"create_index", "drop_index"}:
                    continue
                if not node.args:
                    continue
                name_arg = node.args[0]
                if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
                    continue
                if node.func.attr == "create_index":
                    table = ""
                    if len(node.args) > 1:
                        t = node.args[1]
                        if isinstance(t, ast.Constant) and isinstance(t.value, str):
                            table = t.value
                        elif isinstance(t, ast.Name):
                            table = module_consts.get(t.id, "")
                    created[name_arg.value] = table
                else:
                    dropped.add(name_arg.value)

    return {name: tbl for name, tbl in created.items() if name not in dropped}


def test_migration_created_indexes_exist_in_metadata():
    """A named index that exists only in ``alembic/versions/`` is created on
    upgraded databases and silently missing on fresh installs.

    Mirror every migration index in the model — in ``__table_args__`` as
    ``Index("ix_name", ...)`` or, for a single-column index, as
    ``Column(..., index=True)`` (SQLAlchemy auto-names it ``ix_<table>_<col>``).
    """
    idx_to_table = _indexes_created_by_migrations()

    # Exclude indexes whose TABLE is itself net-retired — Postgres drops an
    # index together with its table (no explicit `drop_index`), so these
    # indexes exist on no database and need no model. Covers the ai_systems
    # entity tables (`a119_drop_ai_systems`) and skill_access_rules (dropped by
    # `a086_drop_skill_access_rules`).
    live_tables = _tables_created_by_migrations()
    from_migrations = {
        name
        for name, tbl in idx_to_table.items()
        # Keep if the table survives, or if we couldn't resolve the table name
        # (be conservative — a resolvable-only exclusion can't silently pass a
        # real gap on a live table).
        if not tbl or tbl in live_tables
    }

    in_metadata: Set[str] = set()
    for table in Base.metadata.tables.values():
        for idx in table.indexes:
            if idx.name:
                in_metadata.add(idx.name)

    missing = from_migrations - in_metadata
    assert not missing, (
        f"Indexes created by Alembic migrations with no counterpart in a "
        f"model's Base.metadata: {sorted(missing)}. Fresh installs build the "
        "schema with create_all() and stamp the Alembic head, so these "
        "indexes will NOT exist on a new deployment. Add the index to the "
        "model (Index(...) in __table_args__ or index=True on the column, "
        "matching the migration's index NAME exactly)."
    )


def test_index_parity_scan_is_not_vacuous():
    """Guard against the AST walk silently matching nothing."""
    found = _indexes_created_by_migrations()
    assert len(found) > 10, (
        f"Only {len(found)} migration-created indexes found — the AST scan is "
        "probably broken rather than there being so few indexes."
    )

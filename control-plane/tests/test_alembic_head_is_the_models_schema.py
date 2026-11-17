# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The migrations build exactly the schema the models describe.

A new installation builds its schema with `alembic upgrade head` (the
baseline revision and every revision after it); the application reads and
writes that schema through the models in `app/database/models/`. When a
revision and a model disagree (a default on one side only, a JSON column that
is JSONB in the other, an index the model declares and no revision creates),
the application works against a database it does not describe. This test
builds one empty database with the revisions and one with
`Base.metadata.create_all()` and compares what Postgres holds in each:
tables, columns (type, nullability, default, generated expression, comment),
constraints, indexes, enum types, sequences and table comments. Column order
is not compared: a revision appends a column, a model may declare it anywhere.

Both builds run in a subprocess: `DATABASE_URL` is read at import and the
declarative `Base` is built once per process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.admin_endpoint_harness import throwaway_pg_database

pytestmark = pytest.mark.skipif(
    os.environ.get("BRUTOR_PG_TESTS") != "1",
    reason="needs Postgres; set BRUTOR_PG_TESTS=1 (and BRUTOR_PG_TEST_URL for a non-default server)",
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_SNAPSHOT = {
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attgenerated, col_description(c.oid, a.attnum)
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
           AND c.relname <> 'alembic_version'
    """,
    "tables": """
        SELECT c.relname, obj_description(c.oid, 'pg_class')
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
         WHERE c.relkind = 'r' AND c.relname <> 'alembic_version'
    """,
    "constraints": """
        SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid)
          FROM pg_constraint k
          JOIN pg_class c ON c.oid = k.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
         WHERE c.relname <> 'alembic_version'
    """,
    "indexes": """
        SELECT tablename, indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    """,
    "enums": """
        SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
          FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
          JOIN pg_namespace n ON n.oid = t.typnamespace AND n.nspname = 'public'
         GROUP BY t.typname
    """,
    "sequences": """
        SELECT s.sequencename, s.data_type::text, s.start_value, s.increment_by
          FROM pg_sequences s WHERE s.schemaname = 'public'
    """,
    "functions_triggers_extensions": """
        SELECT 'function', p.proname FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
        UNION ALL
        SELECT 'trigger', tgname FROM pg_trigger WHERE NOT tgisinternal
        UNION ALL
        SELECT 'extension', extname FROM pg_extension WHERE extname <> 'plpgsql'
    """,
}

_BUILDS = {
    "migrations": """
        from app.database import _alembic_config
        from alembic import command
        command.upgrade(_alembic_config(), "head")
        print("BUILD_OK")
    """,
    "models": """
        from app.database import Base, engine, models  # noqa: F401
        Base.metadata.create_all(bind=engine)
        print("BUILD_OK")
    """,
}


def _build(url: str, how: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "JWT_SECRET": os.environ.get("JWT_SECRET", "x" * 48),
        "ENCRYPTION_KEY": os.environ.get("ENCRYPTION_KEY", "a" * 44 + "="),
    }
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_BUILDS[how])],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or "BUILD_OK" not in proc.stdout:
        pytest.fail(f"building the schema from the {how} failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")


def _snapshot(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {k: sorted(tuple(map(str, r)) for r in conn.execute(text(q))) for k, q in _SNAPSHOT.items()}
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def built():
    urls = {}
    gens = {how: throwaway_pg_database(f"schema_{how}") for how in _BUILDS}
    try:
        for how, gen in gens.items():
            urls[how] = next(gen)
            _build(urls[how], how)
        yield {how: _snapshot(url) for how, url in urls.items()}
    finally:
        for gen in gens.values():
            gen.close()


@pytest.mark.parametrize("kind", sorted(_SNAPSHOT))
def test_the_migrations_build_the_models_schema(built, kind):
    by_migrations, by_models = set(built["migrations"][kind]), set(built["models"][kind])
    only_migrations = sorted(by_migrations - by_models)
    only_models = sorted(by_models - by_migrations)
    assert not only_migrations and not only_models, (
        f"{kind}: the revisions and the models describe different schemas.\n"
        f"only from the revisions: {only_migrations[:20]}\nonly from the models: {only_models[:20]}"
    )


def test_the_snapshot_sees_a_schema(built):
    """Not vacuous: both builds hold the tables, and a planted difference shows."""
    assert len(built["models"]["tables"]) > 100
    planted = set(built["models"]["columns"]) | {("planted", "x", "integer", "False", "None", "", "None")}
    assert planted != set(built["migrations"]["columns"])

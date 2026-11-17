# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Fresh-install schema test — runs the real ``init_database()`` against an
empty Postgres and checks the resulting schema is complete.

Why this needs a real database
------------------------------
``init_database`` has two paths that build the schema by *different means*:
an existing DB gets the Alembic chain applied, while a fresh DB is built with
``Base.metadata.create_all()`` and then *stamped* at head — the migrations are
never replayed. So a table that exists only in ``alembic/versions/`` is
created on every upgraded database and silently missing on every new install,
while ``alembic current`` still reports head.

That is not hypothetical: ``oauth_cimd_clients`` (migration ``a126_oauth_cimd_clients``) shipped
exactly that way and the CIMD OAuth bridge failed at runtime on fresh
installs and trial bundles.

``test_models_registration.py`` catches the same bug statically and runs
everywhere. This test is the runtime counterpart: it also proves
``create_all()`` actually *executes* cleanly, which static analysis cannot.
A model declaring a duplicate index, for example, parses fine and then raises
``DuplicateTable`` the moment a real install runs.

Why a subprocess
----------------
``DATABASE_URL`` is read during import, and the declarative ``Base`` is built
at import time with every model registered against it. Re-importing in-process
creates a *second* ``Base`` with empty metadata while the models stay bound to
the first, so ``create_all()`` silently creates nothing. A fresh install is a
fresh process, so the test uses one — which is also the more faithful
simulation.

Skipped unless ``BRUTOR_PG_TESTS=1``, so the default SQLite-backed suite is
unaffected. CI sets it.

The database is this module's own
----------------------------------
The install runs in a scratch database created on ``BRUTOR_PG_TEST_URL``'s
server and dropped afterwards (``throwaway_pg_database``). It never touches
the database that URL names. It used to: the fixture ran ``DROP SCHEMA public
CASCADE`` on the URL's database, and once a full-suite run pointed at
a working database erased it. ``test_no_test_destroys_the_configured_database``
keeps that from coming back, here or in any other test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.admin_endpoint_harness import throwaway_pg_database
from tests.source_trees import test_sources

needs_pg = pytest.mark.skipif(
    os.environ.get("BRUTOR_PG_TESTS") != "1",
    reason="needs Postgres; set BRUTOR_PG_TESTS=1 (and BRUTOR_PG_TEST_URL for a non-default server)",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def scratch_url():
    yield from throwaway_pg_database("fresh_install")


@pytest.fixture(scope="module")
def fresh_install(scratch_url) -> str:
    """Boot the app's real init path into an empty scratch database."""
    url = scratch_url

    boot = textwrap.dedent(
        """
        import app.database as db
        import app.database.models  # noqa: F401  (registers models on Base)
        db.init_database()
        print("INIT_OK")
        """
    )
    env = {
        **os.environ,
        "DATABASE_URL": url,
        # init_database() seeds a default admin; keep it deterministic.
        "JWT_SECRET": os.environ.get("JWT_SECRET", "x" * 48),
        "ENCRYPTION_KEY": os.environ.get("ENCRYPTION_KEY", "a" * 44 + "="),
    }
    proc = subprocess.run(
        [sys.executable, "-c", boot],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0 or "INIT_OK" not in proc.stdout:
        pytest.fail(
            "init_database() failed on an empty database — this is what a new "
            f"install does.\n\nstdout:\n{proc.stdout[-4000:]}\n\nstderr:\n{proc.stderr[-4000:]}"
        )
    return url


@needs_pg
def test_fresh_install_creates_every_migration_table(fresh_install: str) -> None:
    """A brand-new install must end up with every table the migration chain
    would have produced."""
    from tests.test_models_registration import _tables_created_by_migrations

    engine = create_engine(fresh_install)
    try:
        actual = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    missing = sorted(_tables_created_by_migrations() - actual - {"alembic_version"})
    assert not missing, (
        f"Fresh install is missing tables the migration chain creates: {missing}. "
        "create_all() only builds tables that have a SQLAlchemy model, and the "
        "fresh path stamps Alembic head instead of replaying migrations — so a "
        "migration-only table never appears on a new deployment. Add a model "
        "under app/database/models/ mirroring the migration."
    )


@needs_pg
def test_fresh_install_is_stamped_at_head(fresh_install: str) -> None:
    """A stamp that doesn't match head means the next upgrade replays or skips
    migrations against a schema that doesn't match their assumptions."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    engine = create_engine(fresh_install)
    try:
        with engine.connect() as conn:
            stamped = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()

    head = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini"))).get_current_head()
    assert stamped == head, f"Fresh install stamped {stamped!r} but Alembic head is {head!r}."


@needs_pg
def test_fresh_install_keys_oauth_tokens_with_the_identity_provider(fresh_install: str) -> None:
    """A new install has the key the migration chain leaves,
    and not the one it drops — the gateway's upsert names the columns."""
    engine = create_engine(fresh_install)
    try:
        inspector = inspect(engine)
        uniques = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("oauth_tokens")}
        indexes = {i["name"] for i in inspector.get_indexes("oauth_tokens")}
    finally:
        engine.dispose()
    assert uniques == {
        "uq_oauth_tokens_tenant_idp_user_server": ["tenant_id", "idp_id", "idp_user_id", "mcp_server_id"],
    }
    assert "ix_oauth_tokens_tenant_user_server" not in indexes


# Statements that destroy a whole database's contents. A test may run them
# only inside a database it created itself; none may aim them at the database
# an environment variable names, because that variable is one typo away from
# a working database.
_DESTRUCTIVE = ("DROP SCHEMA", "DROP OWNED", "drop_all(")
# kb_oidc builds an in-memory SQLite engine of its own and drops that.
_OWN_ENGINE = {"test_kb_oidc_derivation.py"}


def test_no_test_destroys_the_configured_database() -> None:
    """Runs everywhere (no Postgres needed): the database was lost to a
    ``DROP SCHEMA public CASCADE`` on ``BRUTOR_PG_TEST_URL``'s database."""
    me = Path(__file__).name
    offenders = []
    for path in test_sources():
        if path.name in _OWN_ENGINE | {me}:
            continue
        source = path.read_text(encoding="utf-8")
        offenders += [f"{path.relative_to(REPO_ROOT)}: {needle}" for needle in _DESTRUCTIVE if needle in source]
    assert not offenders, (
        "These tests destroy a database wholesale. Create a scratch database with "
        "tests.admin_endpoint_harness.throwaway_pg_database and work there instead:\n"
        + "\n".join(offenders)
    )


def test_no_test_defaults_to_the_live_database() -> None:
    """Runs everywhere: three Postgres suites once fell back
    to ``ai_gateway`` when ``BRUTOR_PG_TEST_URL`` was unset. Every test reads
    the URL through ``tests.pg_test_url.pg_test_url``, which defaults to the
    server's ``postgres`` database and refuses ``ai_gateway``."""
    import re

    allowed = {"pg_test_url.py", Path(__file__).name}
    reads = re.compile(r"""environ(?:\.get\(|\[)\s*["'](BRUTOR_PG_TEST_URL|DATABASE_URL)["']\s*,\s*["'][^"']*/ai_gateway""")
    direct = re.compile(r"""environ(?:\.get\(|\[)\s*["']BRUTOR_PG_TEST_URL["']""")
    offenders = []
    for path in test_sources():
        if path.name in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        if reads.search(source):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: defaults a database URL to ai_gateway")
        if direct.search(source):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: reads BRUTOR_PG_TEST_URL itself; use pg_test_url()")
    assert not offenders, "\n".join(offenders)

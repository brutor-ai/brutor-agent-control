# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A run has two identifiers, and the console uses the other one.

`ai_system_runs.id` is the internal `run-<ULID>` primary key. `root_task_id`
is what correlates the run's steps — and it is the *only* one a user ever
sees: it comes back on every LLM response as `X-Brutor-Run-Id`, it is the
console's Chain column, portal feedback is keyed on it, and the trajectory
search puts it on every action row.

So "Inspect" on a trajectory action handed the root to endpoints that
matched only on `id`, and every inspect returned 404 with the run sitting
right there in the table. `_run_by_either_id` accepts both.

Two classes of regression are pinned here:

  * **Behaviour** — both identifiers resolve, unknown ids don't, and the
    tenant filter still applies. Accepting a second identifier is exactly
    the kind of change that quietly widens a lookup across tenants.
  * **Structure** — all three `{run_id}` routes are actually registered.
    While fixing the lookup this module's router factory was split in half
    by a badly placed helper: the file still imported, `ast.parse` still
    succeeded, and every run route silently vanished. A test that only
    exercised the helper would have passed.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.admin.ai_system_runs import (
    _run_by_either_id,
    create_ai_system_runs_router,
)
from app.database.models.ai_system_run import AISystemRun

RUN_PK = "run-01M14CCMXEFNEN7ZTHK8ADKVRJ"
RUN_ROOT = "5GF1QGTJMZCC32GVXA05Y0X7SR"
TENANT = "default"
OTHER_TENANT = "someone-else"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AISystemRun.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AISystemRun(
            id=RUN_PK,
            tenant_id=TENANT,
            root_task_id=RUN_ROOT,
            started_at=datetime.now(timezone.utc),
            terminal_state="abandoned",
            finalized_by="idle",
        )
    )
    session.commit()
    yield session
    session.close()


# ── Behaviour ────────────────────────────────────────────────────────────

def test_finds_run_by_internal_primary_key(db):
    run = _run_by_either_id(db, RUN_PK, TENANT)
    assert run is not None
    assert run.root_task_id == RUN_ROOT


def test_finds_run_by_root_task_id(db):
    """The regression: this is what the console actually sends."""
    run = _run_by_either_id(db, RUN_ROOT, TENANT)
    assert run is not None
    assert run.id == RUN_PK


def test_both_identifiers_resolve_to_the_same_row(db):
    assert _run_by_either_id(db, RUN_PK, TENANT).id == _run_by_either_id(db, RUN_ROOT, TENANT).id


def test_unknown_identifier_is_not_found(db):
    assert _run_by_either_id(db, "run-does-not-exist", TENANT) is None
    assert _run_by_either_id(db, "NOTAREALROOTTASKID", TENANT) is None


@pytest.mark.parametrize("identifier", [RUN_PK, RUN_ROOT])
def test_tenant_isolation_holds_for_both_identifiers(db, identifier):
    """Widening a lookup to a second column is exactly how a tenant filter
    gets dropped by accident. Neither identifier may cross tenants."""
    assert _run_by_either_id(db, identifier, OTHER_TENANT) is None


def test_empty_identifier_matches_nothing(db):
    assert _run_by_either_id(db, "", TENANT) is None


# ── Structure ────────────────────────────────────────────────────────────

def test_all_run_routes_are_registered():
    """Guards the router factory itself.

    The lookup fix was first applied by inserting a module-level helper in
    the middle of `create_ai_system_runs_router`, which terminated the
    factory early and swallowed every route below it into the helper. The
    module still imported cleanly. Only counting the registered routes
    catches that.
    """
    router = create_ai_system_runs_router(logging.getLogger("test"))
    paths = {r.path for r in router.routes}

    for expected in (
        "/admin/runs/{run_id}",
        "/admin/runs/{run_id}/trajectory",
        "/admin/runs/{run_id}/consequences",
    ):
        assert expected in paths, f"{expected} is not registered; paths={sorted(paths)}"

    # The factory also owns the per-system run endpoints; if it were split
    # again those would disappear too.
    assert any("/ai-systems/" in p and "/runs" in p for p in paths), sorted(paths)


def test_router_factory_spans_all_its_routes():
    """A structural assertion in source terms: every `{run_id}` route must
    be *inside* the factory, not orphaned at module scope after it."""
    import ast
    import inspect

    import app.api.v1.endpoints.admin.ai_system_runs as mod

    tree = ast.parse(inspect.getsource(mod))
    factory = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "create_ai_system_runs_router"
    )
    helper = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_run_by_either_id"
    )
    # The helper must sit entirely before the factory, never nested in it.
    assert helper.end_lineno < factory.lineno

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pytest conftest — sets the environment required by `app.database.__init__`
at import time so tests that touch any app module don't have to stub the
env themselves. The DATABASE_URL does not have to point at a real DB; we
only need `create_engine` to build a dialect without opening a connection.

Why the unconditional override on DATABASE_URL (not setdefault):
    A developer running gateway-control natively will have
    ``DATABASE_URL`` exported in their shell pointing at the live local
    Postgres. With ``setdefault`` that real URL bleeds into pytest, so
    any test that happens to grab a fresh ``SessionLocal()`` (instead of
    going through the overridden ``get_db`` dependency) hits the live
    DB — which may already be saturated by the running ``python main.py``
    worker, producing a psycopg2 ``select()`` hang. Unit tests that need
    a DB build their own in-memory SQLite engine; nothing in
    ``tests/test_*.py`` legitimately needs the live URL. The
    ``tests/live/`` suite is HTTP-only and never imports SQLAlchemy
    against this engine, so forcing a fake URL is safe everywhere.
"""

import os

# Port 1 refuses connections at once: an accidental connect fails fast instead
# of reaching whatever Postgres listens on localhost:5432 — on a dev machine
# that is the LIVE stack (a trial stack's Postgres has logged "password
# authentication failed for user test" during a pytest run). Pinned by test_test_db_placeholder_is_unreachable.
os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:1/test_brutor"
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")
# Needed by `app.core.admin_user.dependencies`, which refuses to build an
# admin dependency without it. Several suites set this themselves at import
# time, which meant modules that *don't* — tests/traffic_import — only passed
# when one of those happened to be collected first. That is an ordering
# accident, not a pass: the same suite fails on its own, and would fail in CI
# the moment selection or sharding changed. `setdefault`, so any module that
# wants a different algorithm still wins.
os.environ.setdefault("JWT_ALGORITHM", "HS256")
# The accounts a first start creates have no default password (owner decision
# Q11): the install seed refuses to run without the administrator's, and the
# Enterprise Edition's tenant creation without one. The suite sets what it
# needs here, to values of its own (never a stock password, which the rule refuses); a test of the
# refusal takes them out again (`tests/test_initial_passwords.py`).
os.environ.setdefault("BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD", "suite-tenant-admin-password")
os.environ.setdefault("BRUTOR_SYSTEM_ADMIN_PASSWORD", "suite-system-admin-password")
os.environ.setdefault("BRUTOR_TRIAL_USER_PASSWORD", "Trial123!")


def pytest_configure(config):
    """The edition under test (`tests/app_factory.py`). An
    Enterprise run registers the Enterprise extension once, as an Enterprise
    server does at start, so that what a test calls without building an
    application sees the Enterprise composition. A Community run
    (`BRUTOR_TEST_EDITION=community`) registers nothing."""
    from tests import app_factory

    if app_factory.edition_under_test() == app_factory.ENTERPRISE:
        app_factory.become_enterprise_process()

    # The coverage floor of each package measured, for the edition under test.
    from tests import coverage_floors

    coverage_floors.register(config)


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True, scope="module")
def _a_community_run_stays_community():
    """In a Community run no test file may leave the Enterprise extension
    registered: every later test would run as the Enterprise Edition and say
    nothing (the registries are process-wide). The file that did it gets an
    error, and the registries are put back so that the rest of the run is
    still a Community run. A test that composes the extension itself does it
    inside `tests.community_process.fresh_registries()`."""
    yield
    from tests import app_factory

    if app_factory.edition_under_test() != app_factory.COMMUNITY:
        return
    from app.composition.edition import composed_enterprise_features

    leaked = sorted(composed_enterprise_features())
    if leaked:
        from tests.community_process import become_community_process, let_enterprise_in

        for owner, attr, value in become_community_process():
            if attr == "register":
                let_enterprise_in(owner)
        raise AssertionError(f"this test file left Enterprise features registered in a Community run: {leaked}")


# Make postgres-specific JSONB columns round-trip as JSON when tests
# run against the in-memory SQLite engine. Production uses
# `from sqlalchemy.dialects.postgresql import JSONB` on columns like
# `tenants.data_residency`, `tenants.compliance_profile`, and the
# per-check surface lists on `guardrail_configs`. SQLite's compiler
# rejects JSONB outright, and a plain TEXT fallback would force tests
# to JSON-encode/decode by hand. Borrowing the JSON type's
# bind/result processors gives JSONB dict-in / dict-out behaviour on
# SQLite without changing production behaviour.
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import JSON as _JSON


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


# On any other dialect the ORIGINAL processors run. Returning None there (as
# this shim once did) made every JSONB dict unbindable on Postgres
# ("can't adapt type 'dict'") in the BRUTOR_PG_TESTS runs, so a Postgres-only
# route could not be smoke-tested against a row carrying a JSONB value.
_JSONB_BIND_PROCESSOR = JSONB.bind_processor
_JSONB_RESULT_PROCESSOR = JSONB.result_processor


def _jsonb_bind_processor(self, dialect):
    if dialect.name == "sqlite":
        # Honour none_as_null, so a column declared that way stores SQL NULL
        # here as it does on Postgres.
        return _JSON(none_as_null=self.none_as_null).bind_processor(dialect)
    return _JSONB_BIND_PROCESSOR(self, dialect)


def _jsonb_result_processor(self, dialect, coltype):
    if dialect.name == "sqlite":
        return _JSON().result_processor(dialect, coltype)
    return _JSONB_RESULT_PROCESSOR(self, dialect, coltype)


JSONB.bind_processor = _jsonb_bind_processor
JSONB.result_processor = _jsonb_result_processor


# Register a no-op `pg_notify(channel, payload)` on every SQLite test
# connection. Production fires Postgres LISTEN/NOTIFY via
# `SELECT pg_notify(:channel, :payload)` (see
# app/services/system_ops/notify.py) from session-revoke, cache
# invalidation, etc. The in-memory SQLite engine the unit tests use has
# no such function, so those code paths raise
# `OperationalError: no such function: pg_notify`. A no-op shim lets the
# tests exercise the surrounding logic (the NOTIFY itself is
# fire-and-forget and has no return value to assert on). Same spirit as
# the JSONB->JSON shim above: make Postgres-specific SQL behave under the
# SQLite test dialect without changing production behaviour. Scoped to
# pytest only — conftest is never imported by the running app.
from sqlalchemy import event as _sa_event
from sqlalchemy.engine import Engine as _SAEngine


@_sa_event.listens_for(_SAEngine, "connect")
def _register_sqlite_pg_notify_shim(dbapi_connection, connection_record):
    # Only pysqlite connections expose `create_function`; psycopg2 / real
    # Postgres connections don't, so this is a SQLite-only no-op.
    create_function = getattr(dbapi_connection, "create_function", None)
    if create_function is not None:
        create_function("pg_notify", 2, lambda _channel, _payload: None)


# The one sequence in which the shared SQLite connection keeps a
# write a server would lose (`tests/refusal_write_detector.py`). A test in
# which it happens has seen a state no server would be in, so it fails —
# unless it carries the `refusal_keeps_write` marker, which says the case is
# known, why the test still means something, and where it is held on a
# database with real connections.
from tests import refusal_write_detector as _kept  # noqa: E402

_kept.install()


def pytest_runtest_setup(item):
    _kept.EVENTS.clear()


@_pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    outcome = yield
    events = list(dict.fromkeys(_kept.EVENTS))
    _kept.EVENTS.clear()
    if not events:
        return
    log = os.environ.get("BRUTOR_KEPT_WRITE_LOG")
    if log:
        with open(log, "a") as fh:
            for e in events:
                fh.write(f"{item.nodeid}\t{e.request}\t{','.join(e.tables)}\n")
        return
    if item.get_closest_marker("refusal_keeps_write") is not None or outcome.excinfo is not None:
        return
    lines = "\n".join(f"  {e}" for e in events)
    outcome.force_exception(
        AssertionError(
            "a request flushed a change, another session committed on the suite's shared "
            "SQLite connection, and the request then ended without committing: the change "
            "was KEPT here and a server would have LOST it.\n"
            f"{lines}\n"
            "Either the route must not write before it refuses (or must commit what has to "
            "survive, as `lockout.count_failure` does), or the test declares the case with "
            "@pytest.mark.refusal_keeps_write(reason=...) and "
            "tests/test_refusal_write_semantics.py holds it on a database with real connections."
        )
    )

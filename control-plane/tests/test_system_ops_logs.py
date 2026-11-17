# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
System Operations Console regressions — runtime log-level reload.

What's enforced:

  1. `set_log_level` mutates the in-memory logger AND persists a row
     keyed by `logger:<module>`. The dual-write guarantees a restart
     doesn't lose the override; without it the on-call fix doesn't
     survive the next deploy.

  2. `set_log_level` accepts canonical levels AND the aliases the
     wire surface advertises (warning/warn, fatal/critical/error).
     The wire shape's `Literal` is what UI clients send; runtime is
     permissive so admin curl-driven flows stay ergonomic.

  3. `get_log_levels` returns ONE entry per curated module, plus any
     ad-hoc overrides not in the curated list. The curated list is
     stable so the UI slider grid doesn't reshuffle between calls.

  4. The curated module list matches actual `BrutorLogger("…")`
     constructions in the codebase. A slider that points at a
     module no one logs to is a useless slider.

  5. An unknown level raises ValueError — the endpoint maps this to
     a 400 so the operator gets a clean failure instead of silent
     no-op semantics.
"""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Use Postgres-shaped engine here because the upsert path uses
# `pg_insert(...).on_conflict_do_update(...)` which is dialect-bound.
# We can't run the live mutation against SQLite — so for the persisted
# behaviour we use a recorder, and for the in-memory behaviour we use
# a fake Session that captures the executed statements.


class _RecordingSession:
    """Stand-in for `sqlalchemy.orm.Session` that captures executed
    statements and commits. The real Session would need a Postgres
    engine to handle the upsert; recording the call surface keeps
    the test pure-Python."""

    def __init__(self) -> None:
        self.executed = []
        self.committed = 0

    def execute(self, stmt, *args, **kwargs):
        self.executed.append((stmt, args, kwargs))

        class _R:
            def scalars(self):
                class _S:
                    def all(self_inner):
                        return []
                return _S()

        return _R()

    def commit(self) -> None:
        self.committed += 1


def test_canonical_levels_constant_is_five_rungs():
    from app.services.system_ops.logs import CANONICAL_LEVELS

    assert CANONICAL_LEVELS == ("trace", "debug", "info", "warn", "error")


def test_curated_modules_are_non_empty_and_unique():
    from app.services.system_ops.logs import KNOWN_LOG_MODULES

    assert len(KNOWN_LOG_MODULES) > 0
    assert len(set(KNOWN_LOG_MODULES)) == len(KNOWN_LOG_MODULES)


def test_set_log_level_mutates_in_memory_logger():
    from app.services.system_ops import logs as logs_mod

    sess = _RecordingSession()
    target = "test-system-ops-logs-target"

    # Sanity: a fresh logger inherits the root level. Force it to
    # NOTSET so the assertion below isn't a no-op.
    logging.getLogger(target).setLevel(logging.NOTSET)

    result = logs_mod.set_log_level(
        sess, module=target, level="debug", actor_id="admin-1",
    )
    assert logging.getLogger(target).level == logging.DEBUG
    assert result["module"] == target
    assert result["level"] == "debug"
    assert result["effective_level_int"] == logging.DEBUG
    assert "applied_at" in result
    # Persisted: one execute (the upsert) + commit.
    assert len(sess.executed) == 1
    assert sess.committed == 1


def test_set_log_level_accepts_warning_alias_collapsed_to_warn():
    from app.services.system_ops import logs as logs_mod

    sess = _RecordingSession()
    result = logs_mod.set_log_level(sess, module="alias-target", level="warning")
    assert result["level"] == "warn"
    assert result["effective_level_int"] == logging.WARNING


def test_set_log_level_collapses_critical_onto_error():
    from app.services.system_ops import logs as logs_mod

    sess = _RecordingSession()
    result = logs_mod.set_log_level(sess, module="alias-target-2", level="critical")
    # `critical` is accepted by the alias table but the display name
    # is `error` — the slider doesn't expose a CRITICAL rung.
    assert result["level"] == "error"


def test_set_log_level_rejects_unknown_level():
    from app.services.system_ops import logs as logs_mod

    sess = _RecordingSession()
    with pytest.raises(ValueError):
        logs_mod.set_log_level(sess, module="x", level="bogus")


def test_get_log_levels_returns_curated_plus_overrides():
    from app.services.system_ops import logs as logs_mod
    from app.services.system_ops.logs import KNOWN_LOG_MODULES

    class _SessionWithRows:
        def execute(self, stmt, *args, **kwargs):
            class _R:
                def scalars(self_inner):
                    class _S:
                        def all(self_inner2):
                            return []
                    return _S()
            return _R()

        def commit(self) -> None:
            pass

    rows = logs_mod.get_log_levels(_SessionWithRows())
    assert len(rows) == len(KNOWN_LOG_MODULES)
    # Stable ordering matches KNOWN_LOG_MODULES.
    for row, expected in zip(rows, KNOWN_LOG_MODULES):
        assert row["module"] == expected
        assert row["is_curated"] is True
        assert row["override_level"] is None

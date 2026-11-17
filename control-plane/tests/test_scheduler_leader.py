# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
CP-5 — per-tick leader election for the background schedulers.

Gunicorn defaults to two workers and each one runs the FastAPI
lifespan, so before this every scheduler ticked twice per container.
These tests pin the three behaviours the fix rests on: the winner
runs and releases, the loser skips, and a non-Postgres engine (the
in-memory SQLite the unit suite uses) never blocks a tick.
"""

from unittest.mock import MagicMock

import pytest

from app.services.scheduler_leader import (
    LEASE_CLASSID,
    lock_key,
    run_as_leader,
    scheduler_lease,
)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    def __init__(self, try_lock_result):
        self._try_lock_result = try_lock_result
        self.statements = []
        self.closed = False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(self._try_lock_result)
        return _FakeResult(True)

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, dialect_name="postgresql", try_lock_result=True):
        self.dialect = MagicMock()
        self.dialect.name = dialect_name
        self.conn = _FakeConn(try_lock_result)

    def connect(self):
        return self.conn


def test_lock_key_is_stable_and_in_int32_range():
    # `hash()` is salted per process, so it would give every Gunicorn
    # worker a different key and defeat the lock entirely. CRC32 is
    # the fixed-across-processes property this depends on.
    assert lock_key("audit_export") == lock_key("audit_export")
    for name in ("audit_export", "data_retention", "assurance_digest"):
        assert -(2 ** 31) <= lock_key(name) < 2 ** 31
    # Distinct schedulers must not share a lease.
    names = [
        "audit_export",
        "data_retention",
        "usage_import",
        "assurance_digest",
        "assurance_checks",
        "assurance_notifications",
        "ai_system_response",
        "ai_system_assurance",
        "skill_source_sync",
        "llm_retirement_warner",
        "compliance_evidence",
    ]
    assert len({lock_key(n) for n in names}) == len(names)


def test_leader_runs_and_releases():
    engine = _FakeEngine(try_lock_result=True)
    with scheduler_lease("audit_export", engine=engine) as leader:
        assert leader is True
    joined = " ".join(engine.conn.statements)
    assert "pg_try_advisory_lock" in joined
    assert "pg_advisory_unlock" in joined
    assert engine.conn.closed is True


def test_non_leader_skips_and_does_not_unlock():
    engine = _FakeEngine(try_lock_result=False)
    with scheduler_lease("audit_export", engine=engine) as leader:
        assert leader is False
    joined = " ".join(engine.conn.statements)
    # Unlocking a lock we never took would release the *other* worker's
    # lease mid-tick, which is worse than not locking at all.
    assert "pg_advisory_unlock" not in joined
    assert engine.conn.closed is True


def test_lock_is_released_when_the_tick_raises():
    engine = _FakeEngine(try_lock_result=True)
    with pytest.raises(RuntimeError):
        with scheduler_lease("data_retention", engine=engine):
            raise RuntimeError("tick blew up")
    assert "pg_advisory_unlock" in " ".join(engine.conn.statements)
    assert engine.conn.closed is True


def test_sqlite_engine_always_grants():
    engine = _FakeEngine(dialect_name="sqlite")
    with scheduler_lease("audit_export", engine=engine) as leader:
        assert leader is True
    assert engine.conn.statements == []


def test_run_as_leader_returns_none_when_skipped(monkeypatch):
    calls = []

    def _tick(arg):
        calls.append(arg)
        return "ran"

    engine = _FakeEngine(try_lock_result=False)
    monkeypatch.setattr("app.database.engine", engine, raising=False)
    assert run_as_leader("usage_import", _tick, 1) is None
    assert calls == []

    engine2 = _FakeEngine(try_lock_result=True)
    monkeypatch.setattr("app.database.engine", engine2, raising=False)
    assert run_as_leader("usage_import", _tick, 2) == "ran"
    assert calls == [2]


def test_lease_probe_failure_fails_open(monkeypatch):
    """A DB blip must not stop retention/export/digest platform-wide."""

    class _BrokenEngine(_FakeEngine):
        def connect(self):
            raise RuntimeError("pool exhausted")

    engine = _BrokenEngine()
    with scheduler_lease("audit_export", engine=engine) as leader:
        assert leader is True


def test_classid_is_separate_from_the_migration_lock():
    # init_database() takes pg_advisory_lock(8742) in the ONE-argument
    # space; these leases use the two-argument space, so they can never
    # collide however the hash lands.
    assert LEASE_CLASSID != 8742


# Every background loop `app.py` starts from the FastAPI lifespan, and the
# lease name each one claims. Gunicorn runs 2 workers by default and each
# runs its own lifespan, so a loop missing from this wiring ticks twice per
# container before it ticks twice per replica. (The Enterprise Edition's two
# loops are held by `enterprise/tests/test_scheduler_leader.py`.)
SCHEDULER_MODULES = {
    "app/services/audit_export/scheduler.py": "audit_export",
    "app/services/data_retention/scheduler.py": "data_retention",
    "app/services/ai_system_response/scheduler.py": "ai_system_response",
    "app/services/ai_system_response/assurance_scheduler.py": "ai_system_assurance",
    "app/services/notification_dispatcher.py": "assurance_notifications",
    "app/services/assurance_checks/scheduler.py": "assurance_checks",
    "app/services/assurance_digest_scheduler.py": "assurance_digest",
    "app/services/skill_source_sync/scheduler.py": "SCHEDULER_SKILL_SOURCE_SYNC",
    "app/services/llm/retirement_warner.py": "llm_retirement_warner",
    "app/services/compliance/scheduler.py": "compliance_evidence",
}


def check_scheduler_tick_is_leader_gated(module, lease_name):
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / module).read_text()
    assert "run_as_leader" in src or "scheduler_lease" in src, (
        f"{module} starts a background loop but never takes a scheduler "
        f"lease — with the default 2 gunicorn workers its tick runs twice "
        f"per container."
    )
    assert lease_name in src, (
        f"{module} no longer claims the lease name {lease_name!r}; renaming a "
        f"lease mid-deploy lets an old and a new worker both think they hold it"
    )


@pytest.mark.parametrize(
    "module,lease_name", sorted(SCHEDULER_MODULES.items()), ids=lambda v: str(v)
)
def test_every_scheduler_tick_is_leader_gated(module, lease_name):
    check_scheduler_tick_is_leader_gated(module, lease_name)


def test_lease_names_are_unique():
    """Two schedulers sharing a name would serialise against each other —
    one of them would simply stop running whenever the other ticks."""
    assert len(set(SCHEDULER_MODULES.values())) == len(SCHEDULER_MODULES)

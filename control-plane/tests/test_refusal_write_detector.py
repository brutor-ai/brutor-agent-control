# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The detector of a refused request's lost write (`tests/refusal_write_detector.py`) sees what it
is there to see, and nothing else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String, create_engine, text, update
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.pool import StaticPool

from tests import refusal_write_detector as detector

Base = declarative_base()


class Thing(Base):
    __tablename__ = "f442_thing"
    id = Column(Integer, primary_key=True)
    name = Column(String)


class Note(Base):
    __tablename__ = "f442_note"
    id = Column(Integer, primary_key=True)


@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(e)
    with Session(e) as s:
        s.add(Thing(id=1, name="a"))
        s.commit()
    detector.EVENTS.clear()
    yield e
    detector.EVENTS.clear()
    e.dispose()


def _another_session_commits(engine):
    with Session(engine) as other:
        other.add(Note())
        other.commit()


def _take():
    events = list(detector.EVENTS)
    detector.EVENTS.clear()
    return events


def test_a_flushed_change_kept_by_another_commit_and_then_rolled_back_is_seen(engine):
    request = Session(engine)
    request.get(Thing, 1).name = "changed"
    request.flush()
    _another_session_commits(engine)
    request.rollback()
    request.close()
    events = _take()
    assert [e.tables for e in events] == [("f442_thing",)]
    # The premise: the shared connection really did keep it.
    with Session(engine) as s:
        assert s.get(Thing, 1).name == "changed"


def test_a_value_computed_by_the_database_is_seen(engine):
    """`lockout.count_failure` writes `failed_login_attempts =
    failed_login_attempts + 1`: an attribute set to an SQL expression. (A
    planted bug showed that the detector missed exactly this: asked after the
    flush, the unit of work no longer calls the row modified.)"""
    request = Session(engine)
    row = request.get(Thing, 1)
    request.flush()
    row.name = Thing.name + "!"
    request.flush()
    request.refresh(row, ["name"])
    _another_session_commits(engine)
    request.rollback()
    request.close()
    assert [e.tables for e in _take()] == [("f442_thing",)]


def test_closing_without_a_commit_counts_as_not_committing(engine):
    request = Session(engine)
    request.add(Thing(id=2, name="b"))
    request.flush()
    _another_session_commits(engine)
    request.close()
    assert [e.tables for e in _take()] == [("f442_thing",)]


@pytest.mark.parametrize(
    "statement",
    [
        update(Thing).where(Thing.id == 1).values(name="x"),
        text("UPDATE f442_thing SET name = 'x' WHERE id = 1"),
        text("delete from f442_thing where id = 1"),
        text("INSERT INTO f442_thing (id, name) VALUES (7, 'x')"),
    ],
)
def test_a_statement_sent_without_the_unit_of_work_is_seen_too(engine, statement):
    request = Session(engine)
    request.execute(statement)
    _another_session_commits(engine)
    request.rollback()
    request.close()
    assert [e.tables for e in _take()] == [("f442_thing",)]


def test_a_request_that_commits_is_not_reported(engine):
    request = Session(engine)
    request.get(Thing, 1).name = "changed"
    request.flush()
    _another_session_commits(engine)
    request.commit()
    request.close()
    assert _take() == []


def test_a_request_that_only_read_is_not_reported(engine):
    request = Session(engine)
    assert request.get(Thing, 1).name == "a"
    request.execute(text("SELECT 1"))
    _another_session_commits(engine)
    request.rollback()
    request.close()
    assert _take() == []


def test_a_rollback_with_no_commit_in_between_is_not_reported(engine):
    """The shared connection is right in this case: the rollback undoes it."""
    request = Session(engine)
    request.get(Thing, 1).name = "changed"
    request.flush()
    request.rollback()
    request.close()
    assert _take() == []
    with Session(engine) as s:
        assert s.get(Thing, 1).name == "a"


def test_what_the_request_committed_itself_before_refusing_is_not_reported(engine):
    """`lockout.count_failure` commits on the request's own session and the
    route then refuses: nothing is left to lose."""
    request = Session(engine)
    request.get(Thing, 1).name = "counted"
    request.commit()
    _another_session_commits(engine)
    request.rollback()
    request.close()
    assert _take() == []


def test_sessions_with_a_connection_each_are_not_watched(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'own.db'}?timeout=0.05", future=True)
    Base.metadata.create_all(e)
    request = Session(e)
    request.add(Thing(id=1, name="a"))
    request.flush()
    request.rollback()
    request.close()
    e.dispose()
    assert _take() == []


def test_the_suite_fails_a_test_in_which_it_happens(pytester_like_run):
    result = pytester_like_run(
        """
        from sqlalchemy import Column, Integer, create_engine
        from sqlalchemy.orm import Session, declarative_base
        from sqlalchemy.pool import StaticPool
        import pytest

        Base = declarative_base()

        class T(Base):
            __tablename__ = "t"
            id = Column(Integer, primary_key=True)

        def _go():
            e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
            Base.metadata.create_all(e)
            request = Session(e)
            request.add(T(id=1))
            request.flush()
            with Session(e) as other:
                other.add(T(id=2))
                other.commit()
            request.rollback()
            request.close()

        def test_undeclared():
            _go()

        @pytest.mark.refusal_keeps_write(reason="declared")
        def test_declared():
            _go()

        def test_clean():
            pass
        """
    )
    assert "1 failed, 2 passed" in result, result
    assert "test_undeclared" in result and "a server would have LOST it" in result


@pytest.fixture()
def pytester_like_run(tmp_path):
    """Run a test file of the suite's own (so `tests/conftest.py` applies) in
    a child pytest and return its output."""
    import os
    import subprocess
    import sys
    import uuid
    from pathlib import Path

    here = Path(__file__).resolve().parent
    path = here / f"test_zz_f442_probe_{uuid.uuid4().hex[:8]}.py"

    def run(source: str) -> str:
        import textwrap

        path.write_text(textwrap.dedent(source))
        env = {k: v for k, v in os.environ.items() if k != "BRUTOR_KEPT_WRITE_LOG"}
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-cov", str(path)],
            cwd=here.parent, env=env, capture_output=True, text=True, timeout=300,
        )
        return proc.stdout + proc.stderr

    try:
        yield run
    finally:
        path.unlink(missing_ok=True)

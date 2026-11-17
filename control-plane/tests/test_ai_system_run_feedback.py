# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Run feedback — the run ledger's only true-outcome signal.

`ai_system_runs.feedback_score` shipped carrying the comment "the only
true-outcome signal we will ever have", a read path in the admin API, and no
writer anywhere. This table is the writer.

Two properties are load-bearing and neither is obvious from reading the model:

  * **Feedback outlives — and precedes — the run it describes.** A run does not
    appear in `ai_system_runs` until it is finalized, and portal chats close on
    the 120-second idle sweeper while users click thumbs in about two. So there
    is deliberately no foreign key, and the tests below pin that absence: a
    well-meaning "add the FK for integrity" change would reject essentially
    every real rating.

  * **One rating per user per run.** The unique constraint is what makes the
    write an idempotent UPSERT and stops a user who changes their mind from
    accumulating duplicates that then double-count in the rollup.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from app.database import Base
from app.database.models import AISystemRun, AISystemRunFeedback


# ---------------------------------------------------------------------------
# The writer exists at all
# ---------------------------------------------------------------------------

def test_feedback_table_is_registered():
    """The gap that started this: a column with a reader and no writer."""
    assert "ai_system_run_feedback" in Base.metadata.tables


def test_run_ledger_still_carries_the_rollup_column():
    """The table is the source of truth; the column stays as its rollup.

    Removing the column would break the admin read path and the ledger's own
    honesty story, so this pins that Gap 3 ADDED a writer rather than moving
    the goalposts.
    """
    assert hasattr(AISystemRun, "feedback_score")


# ---------------------------------------------------------------------------
# Cardinality — one rating per user per run
# ---------------------------------------------------------------------------

def test_one_rating_per_user_per_run():
    """What makes the write an UPSERT instead of an append.

    Without it a user revising a thumbs-up to a thumbs-down leaves both rows
    behind and the rollup sums them to zero — the run reads as unrated by
    someone who rated it twice.
    """
    table = Base.metadata.tables["ai_system_run_feedback"]
    uniques = [
        tuple(c.name for c in c_.columns)
        for c_ in table.constraints
        if c_.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("tenant_id", "root_task_id", "user_id") in uniques


def test_scoped_by_tenant():
    """A rollup that is not tenant-scoped leaks ratings across tenants."""
    table = Base.metadata.tables["ai_system_run_feedback"]
    assert "tenant_id" in table.c
    assert not table.c.tenant_id.nullable


# ---------------------------------------------------------------------------
# The FK that must NOT exist
# ---------------------------------------------------------------------------

def test_no_foreign_key_to_ai_system_runs():
    """Feedback legitimately arrives before the run row exists.

    Portal chats are finalized by the 120s idle sweeper; users rate in about
    two seconds. An FK here would reject the rating with a constraint
    violation and the endpoint would 404 a user who did nothing wrong — the
    exact silent-loss failure this feature exists to fix.
    """
    table = Base.metadata.tables["ai_system_run_feedback"]
    targets = {fk.column.table.name for fk in table.foreign_keys}
    assert "ai_system_runs" not in targets, (
        "an FK to ai_system_runs would reject feedback for any run that has "
        "not finalized yet, which is nearly all of them at click time"
    )


# ---------------------------------------------------------------------------
# Score shape
# ---------------------------------------------------------------------------

def test_score_is_required_and_small():
    """NULL score is not a state: a cleared rating is a deleted row.

    Keeping exactly one representation of "not rated" is what lets every
    consumer treat absence as absence without a special case.
    """
    table = Base.metadata.tables["ai_system_run_feedback"]
    assert not table.c.score.nullable
    assert table.c.score.type.__class__.__name__ == "SmallInteger"


def test_score_type_matches_the_rollup_column():
    """The fold does `SUM(score)::smallint` into `ai_system_runs`.

    A widening here would make that cast silently lossy at scale rather than
    failing loudly.
    """
    feedback = Base.metadata.tables["ai_system_run_feedback"]
    runs = Base.metadata.tables["ai_system_runs"]
    assert (
        feedback.c.score.type.__class__
        is runs.c.feedback_score.type.__class__
    )


def test_comment_is_optional():
    """A thumbs-down with no explanation is still a signal worth having."""
    table = Base.metadata.tables["ai_system_run_feedback"]
    assert table.c.comment.nullable


# ---------------------------------------------------------------------------
# Fresh-DB parity — the create_all path must match the migration
# ---------------------------------------------------------------------------

def test_indexes_match_the_migration():
    """Fresh databases are built by `create_all`, not by migrations.

    Only the migrated path would be exercised in CI otherwise, and the two can
    drift silently — exactly how the a151_jsonb_alignment GIN/JSONB break shipped.
    """
    table = Base.metadata.tables["ai_system_run_feedback"]
    names = {ix.name for ix in table.indexes}
    assert "ix_ai_system_run_feedback_run" in names
    assert "ix_ai_system_run_feedback_triage" in names


def test_table_creates_cleanly(tmp_path):
    """The DDL is valid, not merely well-typed in Python."""
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.tables["ai_system_run_feedback"].create(engine)
    assert inspect(engine).has_table("ai_system_run_feedback")


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

@pytest.fixture()
def session(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.tables["ai_system_run_feedback"].create(engine)
    yield sessionmaker(bind=engine)()


def test_to_dict_round_trip(session):
    session.add(
        AISystemRunFeedback(
            id="f1",
            tenant_id="t1",
            root_task_id="run-1",
            user_id="u1",
            score=1,
            comment="spot on",
        )
    )
    session.commit()

    row = session.query(AISystemRunFeedback).one()
    d = row.to_dict()
    assert d["root_task_id"] == "run-1"
    assert d["score"] == 1
    assert d["comment"] == "spot on"
    # Timestamps serialise rather than leaking datetime objects into JSON.
    assert isinstance(d["created_at"], str)


def test_a_run_can_hold_ratings_from_several_users(session):
    """The case a single integer column cannot express.

    Two colleagues using the same assistant is ordinary, and the rollup has to
    be able to say "net zero" rather than silently keeping whichever arrived
    last.
    """
    for i, (user, score) in enumerate([("u1", 1), ("u2", -1)]):
        session.add(
            AISystemRunFeedback(
                id=f"f{i}",
                tenant_id="t1",
                root_task_id="run-1",
                user_id=user,
                score=score,
            )
        )
    session.commit()

    rows = session.query(AISystemRunFeedback).filter_by(root_task_id="run-1").all()
    assert len(rows) == 2
    assert sum(r.score for r in rows) == 0

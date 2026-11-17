# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Run feedback — the only true-outcome signal

Everything else in the run ledger is *behaviour*: step counts, tool calls,
tokens, cost, whether the HTTP calls succeeded. A run can be flawless by every
one of those measures and still have given the user a wrong answer. Nothing the
platform observes from the outside can tell the difference.

`ai_system_runs.feedback_score` was added for exactly that reason, with the
comment *"the only true-outcome signal we will ever have"* — and shipped with a
read path and no writer. This table is the writer.

## Why a table and not just the column

Two reasons, and the first one is fatal on its own.

**Timing.** A run does not exist in `ai_system_runs` until it is *finalized*.
Portal chats carry no `X-Brutor-Run-End` marker, so they are closed by the idle
sweeper — `DEFAULT_RUN_IDLE_TIMEOUT_SECONDS`, 120 seconds after the last call.
A user clicks thumbs-up about two seconds after reading the answer. Writing
straight to the column would therefore miss the row for essentially every real
rating, and the endpoint would answer `404 Run not found` to a user who had
done nothing wrong. Feedback has to be accepted *before* the run it describes
has rolled up, which means it needs somewhere of its own to live.

**Cardinality.** `feedback_score` is one integer. A run can have more than one
rater — a shared assistant, a system two colleagues both used. A single column
cannot say "two up, one down" and cannot let one user change their own mind
without silently overwriting somebody else's opinion. One row per
`(run, user)` can.

So this table is the source of truth and `ai_system_runs.feedback_score` is a
**rollup** of it — the sum of scores, which for the ordinary single-rater case
is exactly the ±1 the column always meant.

## Why the run may not exist yet is not a data-integrity problem

There is deliberately **no foreign key** to `ai_system_runs`. The whole point is
that feedback legitimately arrives first. `root_task_id` is the correlation key
the entire ledger is built on, and ownership is verified against `proxy_logs`
(which is written promptly) rather than against a row that may be minutes away.

Feedback for a run that never finalizes at all is retained rather than
discarded: an abandoned run that a user bothered to thumb-down is a *more*
interesting signal than average, not a less interesting one.
"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)

from app.database import Base


# Deliberately without `ResourceAuthorshipMixin`. That mixin records which
# *admin* created and last changed a configuration resource. This is not a
# configuration resource and its author is not an admin — `user_id` below is
# the rater, so authorship columns would restate it at best and contradict it
# at worst.
class AISystemRunFeedback(Base):
    """One user's rating of one run."""

    __tablename__ = "ai_system_run_feedback"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)

    # Deliberately not a foreign key — see the module docstring. The run this
    # describes is usually not in `ai_system_runs` yet when the rating lands.
    root_task_id = Column(
        String(255),
        nullable=False,
        comment="Correlates to ai_system_runs.root_task_id; no FK by design",
    )

    # Who rated. Scoped per user so one person changing their mind updates
    # their own row rather than overwriting a colleague's.
    user_id = Column(String(255), nullable=False)

    score = Column(
        SmallInteger,
        nullable=False,
        comment="+1 thumbs up, -1 thumbs down. Never 0 — a cleared rating is "
        "a deleted row, so 'not rated' has exactly one representation",
    )

    # Optional free text. The score says something went wrong; this is the only
    # place that can say *what*, and it is what makes a thumbs-down actionable
    # rather than merely countable.
    comment = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # One rating per user per run. This is what makes the write an UPSERT
        # and lets a user revise their rating without accumulating duplicates
        # that would then double-count in the rollup.
        UniqueConstraint(
            "tenant_id",
            "root_task_id",
            "user_id",
            name="uq_ai_system_run_feedback_user",
        ),
        # The rollup path: sum every score for one run.
        Index("ix_ai_system_run_feedback_run", "tenant_id", "root_task_id"),
        # "Show me what people disliked this week" — the review queue. Ordering
        # by score first keeps thumbs-down scans off the much larger set of
        # thumbs-up rows.
        Index(
            "ix_ai_system_run_feedback_triage",
            "tenant_id",
            "score",
            "created_at",
        ),
        {"comment": "Portal thumbs on AI System runs"},
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "root_task_id": self.root_task_id,
            "user_id": self.user_id,
            "score": self.score,
            "comment": self.comment,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

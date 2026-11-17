# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# a134_baselines_drift — migration suite

Two things here have teeth, and neither is the column list.

**The uniqueness key.** `ai_system_baselines` is upserted by the Rust
sweeper every fifteen minutes on `(tenant_id, ai_system_id, metric_key,
bucket, actor_id)`. SQL treats two NULLs as *distinct*, so if `actor_id`
were nullable the `ON CONFLICT` would never match for the system-wide row
and every sweep would insert instead of update — ~96 duplicate baselines
per metric per system per day, and no single row that is "the" baseline
any more. Nothing errors. The table just quietly rots. That is why
`actor_id` is NOT NULL with an `'all'` sentinel, exactly as `bucket`
already does, and why there is a behavioural test for it below rather
than only a string assertion.

**The `system_alerts` category CHECK.** Same shape as a132_liveness_expectations: the sweeper
writes drift alerts from a background task, and a constraint that has not
been widened rejects every one of them inside a loop nobody watches.
"""

from __future__ import annotations


import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.database.models.ai_system_drift import AISystemBaseline, BASELINE_ALL_ACTORS, BASELINE_LEARNING, BASELINE_STATES, CAUSE_LADDER, DRIFT_CLASSES, DRIFT_STATUSES, JS_DIVERGENCE_THRESHOLD, MIN_ABSOLUTE_RATE_CHANGE, MIN_RUNS_FOR_BASELINE, PAGE_HINKLEY_DELTA, PAGE_HINKLEY_THRESHOLD, ROBUST_Z_THRESHOLD
from app.database.models.system_alert import CATEGORIES


NEW_CATEGORIES = ("ai_system_behaviour", "ai_system_cost", "ai_system_dependency")


class TestTheUniquenessKey:
    """The upsert key, and the NULL trap inside it."""


    def test_actor_id_is_not_nullable_in_the_model(self):
        col = AISystemBaseline.__table__.c.actor_id
        assert not col.nullable
        assert col.server_default.arg == BASELINE_ALL_ACTORS

    def test_sentinel_matches_the_bucket_convention(self):
        # `bucket` already uses 'all' for "not sliced". Reusing the word
        # rather than inventing a second sentinel keeps the row readable.
        assert BASELINE_ALL_ACTORS == "all"
        assert AISystemBaseline.__table__.c.bucket.server_default.arg == "all"

    def test_a_duplicate_system_wide_baseline_is_actually_rejected(self):
        """The behavioural half — a string assertion would not have caught this.

        Built the fresh-install way (`create_all`), because that is the path
        a new customer takes and the path where a model/migration divergence
        goes unnoticed.
        """
        engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=engine, tables=[AISystemBaseline.__table__])
        session = sessionmaker(bind=engine)()

        def row(row_id: str) -> AISystemBaseline:
            return AISystemBaseline(
                id=row_id,
                tenant_id="t1",
                ai_system_id="sys-1",
                metric_key="step_count",
                bucket="all",
                actor_id=BASELINE_ALL_ACTORS,
                sample_count=60,
                state=BASELINE_LEARNING,
            )

        session.add(row("b-1"))
        session.commit()
        session.add(row("b-2"))

        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # And the per-actor row is still a distinct baseline.
        session.add(
            AISystemBaseline(
                id="b-3",
                tenant_id="t1",
                ai_system_id="sys-1",
                metric_key="step_count",
                bucket="all",
                actor_id="worker-7",
                sample_count=60,
                state=BASELINE_LEARNING,
            )
        )
        session.commit()
        assert session.query(AISystemBaseline).count() == 2
        session.close()


class TestAlertCategoryCheck:
    """The constraint the Rust sweeper's inserts depend on."""

    def test_categories_added_to_the_model_tuple(self):
        for category in NEW_CATEGORIES:
            assert category in CATEGORIES


class TestWireContractWithRust:
    """Values gateway-core writes literally.

    Mirrored in `brutor-gateway-core/src/services/drift/mod.rs`, whose unit
    tests assert the same numbers and strings from the other side. A
    threshold that disagrees across the two repos means the UI explains one
    detector while a different one fires.
    """

    def test_drift_classes(self):
        assert DRIFT_CLASSES == {
            "behavioural",
            "cost",
            "dependency",
            "model",
            "input",
            "conformance",
            # a147_operating_envelope — CP-emitted envelope breaches; mirrored in the Rust
            # drift_class module even though the sweeps never emit it, so
            # both planes agree on what a drift_class may say.
            "contract",
            # CP-emitted agent implementation findings (the
            # agent checks), so a response policy can target them. Mirror in
            # the Rust drift_class module like `contract`.
            "agent",
        }

    def test_drift_statuses(self):
        assert DRIFT_STATUSES == {
            "active",
            "acknowledged",
            "resolved",
            "false_positive",
        }

    def test_baseline_states(self):
        assert BASELINE_STATES == {"learning", "ready", "stale"}

    def test_detector_thresholds(self):
        assert MIN_RUNS_FOR_BASELINE == 50
        assert JS_DIVERGENCE_THRESHOLD == 0.15
        assert ROBUST_Z_THRESHOLD == 3.0
        assert MIN_ABSOLUTE_RATE_CHANGE == 0.02

    def test_page_hinkley_runs_in_robust_z_units(self):
        # The threshold must exceed what one observation can contribute
        # (robust_z saturates at 10), or a single bad afternoon reads as a
        # system that started behaving differently.
        assert PAGE_HINKLEY_DELTA == 0.5
        assert PAGE_HINKLEY_THRESHOLD == 25.0
        assert PAGE_HINKLEY_THRESHOLD > 10.0

    def test_the_cause_ladder_ends_in_an_honest_answer(self):
        # Every rung above it is a specific, checkable claim; the last one
        # is the admission that nothing lined up. Dropping it would force
        # the attributor to pick a plausible-sounding cause instead.
        assert CAUSE_LADDER[-1] == "unexplained"
        assert len(set(CAUSE_LADDER)) == len(CAUSE_LADDER)

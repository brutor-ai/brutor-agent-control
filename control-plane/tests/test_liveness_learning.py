# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Cadence learning + liveness model

The single most consequential decision in this module is that the window
is sized from the **p95** inter-run gap rather than the median. A window
at the median alerts on half of all normal quiet periods *by
construction* — and alert fatigue is the critical risk to liveness
monitoring. So that property is asserted directly, not implied.

The other behaviour worth pinning is the refusal: learning from too little
history must not produce a confident-looking expectation. An operator who
accepts a guess derived from three runs, gets paged, and stops trusting
the category has cost more than shipping nothing.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.database.models.ai_system_liveness import (
    AISystemLivenessExpectation,
    DEFAULT_MODE_BY_SYSTEM_KIND,
    LIVENESS_CONTINUOUS,
    LIVENESS_MODES,
    LIVENESS_ON_DEMAND,
    LIVENESS_SCHEDULED,
    LIVENESS_SPORADIC,
    MIN_RUNS_TO_LEARN_CADENCE,
    SILENCE_ALERTING_MODES,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.end_user_group import (
    EndUserGroup,
    end_user_group_members,
    resource_group_end_user_groups,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.liveness_learning import (
    default_mode_for,
    liveness_learning_service,
)

TENANT = "t1"
SYSTEM = "sys-1"


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            AISystemRun.__table__,
            AISystemLivenessExpectation.__table__,
            ResourceGroup.__table__,
            # ResourceGroup eagerly loads its end-user-group bindings, so
            # those tables must exist even though nothing here uses them.
            EndUserGroup.__table__,
            end_user_group_members,
            resource_group_end_user_groups,
        ],
    )
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Factory()
    yield s
    s.close()
    engine.dispose()


def add_system(session, kind: str = "agent"):
    session.add(
        ResourceGroup(
            id=SYSTEM,
            tenant_id=TENANT,
            name=SYSTEM,
            group_type=GroupType.AI_SYSTEM,
            system_kind=kind,
            hierarchy_level=0,
            inherit_resources=True,
            is_active=True,
            is_system=False,
        )
    )
    session.flush()


_n = {"i": 0}


def add_runs(session, gaps_seconds, *, base_hours_ago: int = 48):
    """Add runs separated by the given gaps, oldest first."""
    t = datetime.now(timezone.utc) - timedelta(hours=base_hours_ago)
    for gap in [0] + list(gaps_seconds):
        t = t + timedelta(seconds=gap)
        _n["i"] += 1
        session.add(
            AISystemRun(
                id=f"run-{_n['i']}",
                tenant_id=TENANT,
                root_task_id=f"root-{_n['i']}",
                ai_system_id=SYSTEM,
                started_at=t,
                terminal_state="completed",
                finalized_by="idle",
            )
        )
    session.flush()


class TestDefaultMode:
    @pytest.mark.parametrize("kind,expected", sorted(DEFAULT_MODE_BY_SYSTEM_KIND.items()))
    def test_kind_maps_to_a_valid_mode(self, kind, expected):
        assert default_mode_for(kind) == expected
        assert expected in LIVENESS_MODES

    def test_assistants_default_to_on_demand(self):
        # Humans are bursty and go on holiday. Paging about a quiet Sunday
        # on a chat assistant is how an alert category gets muted.
        assert default_mode_for("assistant") == LIVENESS_ON_DEMAND

    def test_integrations_default_to_scheduled(self):
        assert default_mode_for("integration") == LIVENESS_SCHEDULED

    def test_unknown_kind_falls_back_safely(self):
        # The safe fallback is the one that never pages.
        assert default_mode_for(None) == LIVENESS_ON_DEMAND
        assert default_mode_for("something-new") == LIVENESS_ON_DEMAND


class TestRefusesToGuess:
    def test_too_few_runs_is_not_confident(self, session):
        add_system(session)
        add_runs(session, [60] * 3)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.confident is False
        assert p.learned_from_runs == 4
        assert str(MIN_RUNS_TO_LEARN_CADENCE) in p.rationale

    def test_no_runs_at_all_is_not_confident(self, session):
        add_system(session)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.confident is False
        assert p.learned_from_runs == 0

    def test_unconfident_proposal_still_carries_the_kind_default(self, session):
        add_system(session, kind="integration")
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.mode == LIVENESS_SCHEDULED

    def test_all_runs_at_one_instant_is_not_a_cadence(self, session):
        add_system(session)
        add_runs(session, [0] * 20)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.confident is False
        assert "bulk import" in p.rationale


class TestWindowSizing:
    def test_window_is_sized_from_p95_not_median(self, session):
        add_system(session)
        # Mostly 60s apart, with a few long-but-normal gaps. A window sized
        # from the median (60s) would fire on every one of those gaps.
        add_runs(session, [60] * 18 + [3000, 3200])
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)

        assert p.confident is True
        assert p.median_gap_seconds is not None
        assert p.p95_gap_seconds is not None
        assert p.p95_gap_seconds > p.median_gap_seconds
        assert p.window_seconds >= p.p95_gap_seconds, (
            "a window below the observed p95 gap alerts on normal quiet periods"
        )

    def test_window_is_rounded_to_something_recognisable(self, session):
        add_system(session)
        add_runs(session, [847] * 15)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        # "every 15 minutes" reads as a decision; "every 847 seconds" reads
        # as a machine guessing.
        assert p.window_seconds in (900, 1800, 3600, 7200, 21600, 43200, 86400)

    def test_window_never_goes_below_the_floor(self, session):
        add_system(session)
        add_runs(session, [1] * 30)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.window_seconds >= 300, (
            "below five minutes ordinary jitter alone trips the detector"
        )

    def test_grace_scales_with_the_window(self, session):
        add_system(session)
        add_runs(session, [43000] * 15)  # ~12h apart
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.grace_seconds >= int(p.window_seconds * 0.1)

    def test_daily_job_gets_a_daily_window(self, session):
        add_system(session, kind="integration")
        add_runs(session, [86400] * 14, base_hours_ago=24 * 20)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.confident is True
        assert p.mode == LIVENESS_SCHEDULED
        assert p.window_seconds >= 86400


class TestExpectedMinRuns:
    def test_continuous_expects_roughly_the_observed_rate(self, session):
        add_system(session, kind="application")  # → continuous
        add_runs(session, [60] * 30)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.mode == LIVENESS_CONTINUOUS
        # Headroom on purpose — a quiet hour must not read as a collapse.
        assert p.expected_min_runs >= 1
        assert p.expected_min_runs <= p.window_seconds / 60

    def test_scheduled_expects_a_single_run(self, session):
        add_system(session, kind="integration")
        add_runs(session, [3600] * 15)
        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.expected_min_runs == 1


class TestModel:
    def test_mode_is_validated(self, session):
        with pytest.raises(ValueError, match="mode must be one of"):
            AISystemLivenessExpectation(
                id="x", tenant_id=TENANT, ai_system_id=SYSTEM, mode="whenever"
            )

    def test_negative_windows_rejected(self, session):
        with pytest.raises(ValueError, match="must not be negative"):
            AISystemLivenessExpectation(
                id="x", tenant_id=TENANT, ai_system_id=SYSTEM, window_seconds=-1
            )

    def test_expected_min_runs_must_be_at_least_one(self, session):
        with pytest.raises(ValueError, match="at least 1"):
            AISystemLivenessExpectation(
                id="x", tenant_id=TENANT, ai_system_id=SYSTEM, expected_min_runs=0
            )

    @pytest.mark.parametrize(
        "mode,expected",
        [
            (LIVENESS_CONTINUOUS, True),
            (LIVENESS_SCHEDULED, True),
            (LIVENESS_SPORADIC, False),
            (LIVENESS_ON_DEMAND, False),
        ],
    )
    def test_only_continuous_and_scheduled_alert_on_silence(self, mode, expected):
        row = AISystemLivenessExpectation(
            id="x", tenant_id=TENANT, ai_system_id=SYSTEM, mode=mode, enabled=True
        )
        assert row.alerts_on_silence is expected

    def test_disabled_never_alerts_whatever_the_mode(self):
        row = AISystemLivenessExpectation(
            id="x",
            tenant_id=TENANT,
            ai_system_id=SYSTEM,
            mode=LIVENESS_CONTINUOUS,
            enabled=False,
        )
        assert row.alerts_on_silence is False

    def test_silence_alerting_modes_constant_matches_the_property(self):
        for mode in LIVENESS_MODES:
            row = AISystemLivenessExpectation(
                id="x", tenant_id=TENANT, ai_system_id=SYSTEM, mode=mode, enabled=True
            )
            assert row.alerts_on_silence == (mode in SILENCE_ALERTING_MODES)

    def test_deadline_is_window_plus_grace(self):
        row = AISystemLivenessExpectation(
            id="x",
            tenant_id=TENANT,
            ai_system_id=SYSTEM,
            window_seconds=3600,
            grace_seconds=300,
        )
        assert row.deadline_seconds == 3900

    def test_learned_cadence_requires_enough_runs(self):
        row = AISystemLivenessExpectation(
            id="x", tenant_id=TENANT, ai_system_id=SYSTEM, learned_from_runs=3
        )
        assert row.has_learned_cadence is False
        row.learned_from_runs = MIN_RUNS_TO_LEARN_CADENCE
        assert row.has_learned_cadence is True

    def test_to_dict_exposes_the_derived_flags(self):
        row = AISystemLivenessExpectation(
            id="x",
            tenant_id=TENANT,
            ai_system_id=SYSTEM,
            mode=LIVENESS_SCHEDULED,
            window_seconds=86400,
            grace_seconds=3600,
            enabled=True,
        )
        d = row.to_dict()
        assert d["alerts_on_silence"] is True
        assert d["deadline_seconds"] == 90000
        assert d["mode"] == LIVENESS_SCHEDULED


class TestScoping:
    def test_other_systems_do_not_pollute_the_cadence(self, session):
        add_system(session)
        add_runs(session, [60] * 20)
        # A different system running at a wildly different rate.
        for i in range(20):
            session.add(
                AISystemRun(
                    id=f"other-{i}",
                    tenant_id=TENANT,
                    root_task_id=f"other-root-{i}",
                    ai_system_id="some-other-system",
                    started_at=datetime.now(timezone.utc) - timedelta(days=i),
                    terminal_state="completed",
                    finalized_by="idle",
                )
            )
        session.flush()

        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.median_gap_seconds == 60

    def test_replays_are_excluded(self, session):
        add_system(session)
        add_runs(session, [60] * 20)
        for i in range(50):
            session.add(
                AISystemRun(
                    id=f"replay-{i}",
                    tenant_id=TENANT,
                    root_task_id=f"replay-root-{i}",
                    ai_system_id=SYSTEM,
                    started_at=datetime.now(timezone.utc) - timedelta(seconds=i),
                    terminal_state="completed",
                    finalized_by="idle",
                    is_replay=True,
                )
            )
        session.flush()

        p = liveness_learning_service.propose(session, TENANT, SYSTEM)
        assert p.median_gap_seconds == 60, (
            "a replay is a simulation against recorded traffic — it never "
            "happened on a schedule"
        )

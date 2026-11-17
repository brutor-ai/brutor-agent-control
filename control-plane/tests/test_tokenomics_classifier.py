# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Tokenomics — Big-T workload classification

The class on a Mission Control card is a *measurement* of the run ledger,
so every expected class here is hand-derived from the seeded runs, never
from the code under test. The suite pins the four behaviours that make
the notation honest:

  * precedence — delegation beats a chatty loop beats a good cache: a
    chatty loop with a cache is still a chatty loop;
  * the evidence floor — below MIN_RUNS_FOR_CLASS there is NO class,
    because a class computed over a handful of runs is noise wearing
    notation;
  * scope — replays and out-of-window runs never influence the class;
  * rollups — worst-of, never averages (T_CLASS_RANK ordering).

The final section proves the materialization added by
``a149_health_card_fields``: `upsert_health`
writes verdict_status / t_class / k_median / a_max onto the health row,
and `to_dict` carries them (the triple rule).
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests.conftest  # noqa: F401 — registers the SQLite JSONB/notify shims

from app.database import Base
import app.database.models  # noqa: F401 — registers every model on Base.metadata
from app.database.models.ai_system_run import AISystemRun
from app.services.tokenomics import (
    CACHE_DOMINANT_SHARE,
    CLASSIFICATION_WINDOW_HOURS,
    DELEGATION_SHARE_THRESHOLD,
    EXPLAINERS,
    K_LINEAR_THRESHOLD,
    MIN_RUNS_FOR_CLASS,
    T_AGENTIC,
    T_CLASS_RANK,
    T_CONSTANT,
    T_LINEAR,
    T_MULTIPLICATIVE,
    classify_workload,
    worst_class,
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
    # Full schema: the materialization tests need resource_groups, health
    # and the drift/contract tables compute_health touches.
    Base.metadata.create_all(bind=engine)
    Factory = sessionmaker(bind=engine, autoflush=False, future=True)
    s = Factory()
    yield s
    s.close()
    engine.dispose()


_counter = {"n": 0}


def add_run(
    session,
    *,
    llm_calls: int = 1,
    actions: int = 1,
    cache_hits: int = 0,
    a2a_calls: int = 0,
    delegation_depth: int = 0,
    cost: float = 0.01,
    is_replay: bool = False,
    hours_ago: float = 1,
    system_id: Optional[str] = SYSTEM,
    tenant_id: str = TENANT,
    state: str = "completed",
) -> AISystemRun:
    _counter["n"] += 1
    n = _counter["n"]
    run = AISystemRun(
        id=f"run-{n}",
        tenant_id=tenant_id,
        root_task_id=f"root-{n}",
        ai_system_id=system_id,
        started_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        ended_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        terminal_state=state,
        finalized_by="idle",
        llm_call_count=llm_calls,
        action_count=actions,
        cache_hit_count=cache_hits,
        a2a_call_count=a2a_calls,
        max_delegation_depth=delegation_depth,
        total_cost_usd=cost,
        is_replay=is_replay,
    )
    session.add(run)
    session.flush()
    return run


def seed_many(session, count: int, **kwargs) -> None:
    for _ in range(count):
        add_run(session, **kwargs)


class TestEvidenceFloor:
    def test_below_the_floor_there_is_no_class(self, session):
        seed_many(session, MIN_RUNS_FOR_CLASS - 1, llm_calls=9)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["t_class"] is None
        assert result["k_median"] is None
        assert result["runs"] == MIN_RUNS_FOR_CLASS - 1

    def test_zero_runs_is_no_class_not_a_crash(self, session):
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["t_class"] is None
        assert result["runs"] == 0

    def test_exactly_at_the_floor_a_class_is_asserted(self, session):
        seed_many(session, MIN_RUNS_FOR_CLASS, llm_calls=1)
        assert classify_workload(session, TENANT, SYSTEM)["t_class"] == T_LINEAR


class TestClassRules:
    def test_one_call_per_task_is_linear(self, session):
        seed_many(session, 25, llm_calls=1)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["t_class"] == T_LINEAR
        assert result["k_median"] == 1.0

    def test_a_single_followup_is_still_ordinary_linear_practice(self, session):
        # k_median == 2 is not > K_LINEAR_THRESHOLD — classify→answer is
        # normal, not a hidden multiplier.
        seed_many(session, 25, llm_calls=int(K_LINEAR_THRESHOLD))
        assert classify_workload(session, TENANT, SYSTEM)["t_class"] == T_LINEAR

    def test_a_chatty_loop_is_multiplicative(self, session):
        seed_many(session, 25, llm_calls=5, actions=5)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["t_class"] == T_MULTIPLICATIVE
        assert result["k_median"] == 5.0

    def test_the_k_median_is_the_median_not_the_mean(self, session):
        # 13 runs at k=1, 12 at k=100: the mean is huge, the median is 1.
        seed_many(session, 13, llm_calls=1)
        seed_many(session, 12, llm_calls=100)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["k_median"] == 1.0
        assert result["t_class"] == T_LINEAR

    def test_cache_dominated_work_is_constant(self, session):
        seed_many(session, 25, llm_calls=1, actions=2, cache_hits=2)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["t_class"] == T_CONSTANT
        assert result["cache_share"] >= CACHE_DOMINANT_SHARE

    def test_delegation_by_a2a_calls_is_agentic(self, session):
        seed_many(session, 23, llm_calls=1)
        seed_many(session, 2, llm_calls=1, a2a_calls=1)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["delegation_share"] >= DELEGATION_SHARE_THRESHOLD
        assert result["t_class"] == T_AGENTIC

    def test_delegation_by_depth_is_agentic(self, session):
        seed_many(session, 23, llm_calls=1)
        seed_many(session, 2, llm_calls=1, delegation_depth=2)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["t_class"] == T_AGENTIC
        assert result["a_max"] == 2

    def test_incidental_delegation_stays_below_the_threshold(self, session):
        # 1 delegated run in 25 = 4% — below 5%, so not agentic.
        seed_many(session, 24, llm_calls=1)
        seed_many(session, 1, llm_calls=1, a2a_calls=1)
        assert classify_workload(session, TENANT, SYSTEM)["t_class"] == T_LINEAR


class TestPrecedence:
    def test_a_chatty_loop_with_a_good_cache_is_still_a_chatty_loop(self, session):
        seed_many(session, 25, llm_calls=6, actions=10, cache_hits=8)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["cache_share"] >= CACHE_DOMINANT_SHARE
        assert result["t_class"] == T_MULTIPLICATIVE

    def test_delegation_beats_the_chatty_loop(self, session):
        seed_many(session, 25, llm_calls=6, a2a_calls=1)
        assert classify_workload(session, TENANT, SYSTEM)["t_class"] == T_AGENTIC


class TestScope:
    def test_replays_never_influence_the_class(self, session):
        seed_many(session, 25, llm_calls=1)
        seed_many(session, 30, llm_calls=50, is_replay=True)
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["runs"] == 25
        assert result["t_class"] == T_LINEAR

    def test_runs_outside_the_window_never_influence_the_class(self, session):
        seed_many(session, 25, llm_calls=1)
        seed_many(
            session, 30, llm_calls=50,
            hours_ago=CLASSIFICATION_WINDOW_HOURS + 24,
        )
        result = classify_workload(session, TENANT, SYSTEM)
        assert result["runs"] == 25
        assert result["t_class"] == T_LINEAR

    def test_other_systems_and_tenants_are_invisible(self, session):
        seed_many(session, 25, llm_calls=1)
        seed_many(session, 30, llm_calls=50, system_id="sys-other")
        seed_many(session, 30, llm_calls=50, tenant_id="t2")
        assert classify_workload(session, TENANT, SYSTEM)["t_class"] == T_LINEAR


class TestWorstClass:
    def test_the_rank_orders_agentic_worst(self):
        assert (
            T_CLASS_RANK[T_AGENTIC]
            > T_CLASS_RANK[T_MULTIPLICATIVE]
            > T_CLASS_RANK[T_LINEAR]
            > T_CLASS_RANK[T_CONSTANT]
        )

    def test_worst_of_is_never_an_average(self):
        assert worst_class(T_CONSTANT, T_LINEAR, T_AGENTIC) == T_AGENTIC
        assert worst_class(T_LINEAR, T_MULTIPLICATIVE) == T_MULTIPLICATIVE

    def test_none_entries_contribute_nothing(self):
        assert worst_class(None, T_LINEAR, None) == T_LINEAR
        assert worst_class(None, None) is None
        assert worst_class() is None

    def test_an_unknown_label_is_ignored_not_ranked(self):
        assert worst_class("T(?)", T_CONSTANT) == T_CONSTANT


class TestExplainers:
    def test_every_explainer_has_a_title_and_a_body(self):
        assert EXPLAINERS, "explainers must not be empty"
        for key, entry in EXPLAINERS.items():
            assert entry.get("title"), f"{key} missing title"
            assert entry.get("body"), f"{key} missing body"

    def test_every_measured_class_has_its_own_info_box(self):
        # 2026-08-19 structure: the chip's info box explains the card's
        # OWN class, assuming the operator has read the guide; every
        # class the classifier can assert must therefore have an entry.
        for cls in (T_CONSTANT, T_LINEAR, T_MULTIPLICATIVE, T_AGENTIC):
            e = EXPLAINERS[f"t_class::{cls}"]
            assert e["title"].startswith(cls)
            assert e["body"]
            # Shared footer: measured, not estimated; points at the guide.
            footer = e["sections"][-1]["body"]
            assert "run ledger" in footer and "guide" in footer

    def test_the_guide_covers_the_whole_ladder(self):
        from app.services.tokenomics import WORKLOAD_GUIDE

        assert WORKLOAD_GUIDE["title"].startswith("Workload classes")
        text = str(WORKLOAD_GUIDE)
        # The industry ladder is complete — including the two classes the
        # classifier itself never asserts…
        for cls in ("T(1)", "T(log n)", "T(n)", "T(n·k)", "T(n·k·a)", "T(∞)"):
            assert cls in text, f"guide missing {cls}"
        # …and the guide says why Brutor doesn't assert them.
        assert "never asserted" in text
        # T(∞) must be described as bounded-where-configured, never as
        # structurally impossible. Depth genuinely is hard-capped; loop
        # iteration is only bounded by quotas an operator set, and dollar
        # caps need hard stop. Overclaiming here is what leaked into
        # marketing copy once already.
        assert "structurally prevented" not in text
        assert "structurally impossible" not in text
        assert "hard-capped" in text
        assert "only " in text and "configured them" in text
        assert "hard stop" in text
        assert "a finding, not a class" in text
        # Provenance and the two dashboard rules travel with it.
        assert "Tokenomics Foundation" in text and "Linux Foundation" in text
        assert "worst-of, never averages" in text
        assert "tokeneconomics.com" in text

    def test_the_guide_explains_the_notation_honestly(self):
        # The "why these letters" section (an operator's question):
        # T is tokens not time; n and k are inherited notation; a is the
        # invented mnemonic and understates exponential depth — and the
        # lineage is flagged as our reading, not the Foundation's claim.
        from app.services.tokenomics import WORKLOAD_GUIDE

        sec = next(
            s for s in WORKLOAD_GUIDE["sections"]
            if (s.get("title") or "").startswith("Why these letters")
        )
        assert "T is not time" in sec["body"]
        assert "tokens" in sec["body"]
        assert sec["points"][0].startswith("n is")
        assert sec["points"][1].startswith("k is")
        assert "mnemonic for agent" in sec["points"][2]
        assert "b^a" in sec["points"][2]
        text = str(WORKLOAD_GUIDE)
        assert "our reading" in text and "the Foundation doesn't state it" in text

    def test_the_card_concepts_are_all_covered(self):
        for key in (
            "t_class", "cost_per_task", "two_projections", "worst_of",
            "findings", "unattributed", "observed_plane",
        ):
            assert key in EXPLAINERS


class TestHealthMaterialization:
    """upsert_health writes the card judgments onto the health row."""

    def _system(self, session):
        from app.database.models.resource_group import GroupType, ResourceGroup
        from app.database.models.tenant import Tenant

        session.add(
            Tenant(
                id=TENANT, name=TENANT, display_name="T1",
                organization_name="T1",
                primary_contact_email="a@t1.example",
                is_active=True, is_default=False, is_trial=False,
                suspended=False, billing_plan="self_hosted", features={},
            )
        )
        system = ResourceGroup(
            id=SYSTEM, tenant_id=TENANT, name="Copilot",
            description="test system", hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM,
        )
        session.add(system)
        session.flush()
        return system

    def test_upsert_health_materializes_verdict_and_t_class(self, session):
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=5, actions=5)

        row = upsert_health(session, system)

        assert row.t_class == T_MULTIPLICATIVE
        assert row.k_median == 5.0
        assert row.a_max == 0
        assert row.verdict_status is not None

    def test_no_evidence_materializes_no_class(self, session):
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, MIN_RUNS_FOR_CLASS - 1, llm_calls=5)

        row = upsert_health(session, system)

        assert row.t_class is None

    def test_to_dict_carries_the_health_card_fields(self, session):
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=1)

        d = upsert_health(session, system).to_dict()

        for key in ("verdict_status", "t_class", "k_median", "a_max"):
            assert key in d, f"to_dict missing {key} (triple rule)"
        assert d["t_class"] == T_LINEAR


class TestCostComplexityFinding:
    """The class-change → drift-event contract (the article's promise).

    Honesty rules pinned:
      * a TRANSITION emits exactly one open `cost`/`t_class` finding with
        the readable summary and the measured evidence;
      * first classification and evidence-thinning (→ None) are not
        transitions;
      * a standing finding never stacks; a new transition supersedes it;
      * downward moves are low severity — good news is not an alarm;
      * a recent contract mint is attributed; otherwise rung 8 says
        `unexplained` plainly.
    """

    def _system(self, session):
        return TestHealthMaterialization._system(self, session)

    def _events(self, session):
        from app.database.models.ai_system_drift import AISystemDriftEvent

        return (
            session.query(AISystemDriftEvent)
            .filter(AISystemDriftEvent.metric_key == "t_class")
            .order_by(AISystemDriftEvent.detected_at)
            .all()
        )

    def test_first_classification_is_not_a_change(self, session):
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=1)
        upsert_health(session, system)

        assert self._events(session) == []

    def test_transition_up_emits_one_cost_finding(self, session):
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=1)
        upsert_health(session, system)          # T(n), no event
        seed_many(session, 40, llm_calls=8)     # median shifts decisively
        upsert_health(session, system)          # T(n·k) — the change

        events = self._events(session)
        assert len(events) == 1
        e = events[0]
        assert e.drift_class == "cost"
        assert e.direction == "up"
        assert e.severity == "medium"
        assert e.status == "active"
        assert e.attributed_cause == "unexplained"
        assert e.cause_evidence["summary"] == (
            "Workload class changed T(n) → T(n·k)"
        )
        assert e.cause_evidence["from_class"] == T_LINEAR
        assert e.cause_evidence["to_class"] == T_MULTIPLICATIVE
        assert e.cause_evidence["runs"] == 65

    def test_standing_finding_does_not_stack(self, session):
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=1)
        upsert_health(session, system)
        seed_many(session, 40, llm_calls=8)
        upsert_health(session, system)
        upsert_health(session, system)  # same state again

        assert len(self._events(session)) == 1

    def test_new_transition_supersedes_the_standing_finding(self, session):
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=1)
        upsert_health(session, system)
        seed_many(session, 40, llm_calls=8)
        upsert_health(session, system)          # → T(n·k), event 1
        # Delegation becomes how the system works → agentic.
        seed_many(session, 30, llm_calls=8, a2a_calls=1, delegation_depth=1)
        upsert_health(session, system)          # → T(n·k·a), event 2

        events = self._events(session)
        assert len(events) == 2
        assert events[0].status == "resolved"
        assert events[1].status == "active"
        assert events[1].severity == "high"     # arriving at the compounding class
        assert events[1].cause_evidence["to_class"] == T_AGENTIC

    def test_downward_move_is_low_severity_good_news(self, session):
        from app.database.models.ai_system_run import AISystemRun
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=8)
        upsert_health(session, system)          # T(n·k)
        session.query(AISystemRun).delete()
        seed_many(session, 25, llm_calls=1)
        upsert_health(session, system)          # → T(n)

        events = self._events(session)
        assert len(events) == 1
        assert events[0].direction == "down"
        assert events[0].severity == "low"
        assert events[0].attributed_cause == "unexplained"

    def test_evidence_thinning_to_none_is_not_a_change(self, session):
        from app.database.models.ai_system_run import AISystemRun
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=1)
        upsert_health(session, system)          # T(n)
        session.query(AISystemRun).delete()
        upsert_health(session, system)          # → None (below the floor)

        assert self._events(session) == []

    def test_recent_contract_mint_is_the_attributed_cause(self, session):
        from app.database.models.ai_system_contract import AISystemContract
        from app.services.ai_system_assurance import upsert_health

        system = self._system(session)
        seed_many(session, 25, llm_calls=1)
        upsert_health(session, system)
        session.add(
            AISystemContract(
                id="ct-1", tenant_id=TENANT, ai_system_id=SYSTEM,
                version=1, contract_hash="sha256:" + "a" * 64,
                resolved_closure={}, status="active",
            )
        )
        session.flush()
        seed_many(session, 40, llm_calls=8)
        upsert_health(session, system)

        events = self._events(session)
        assert len(events) == 1
        assert events[0].attributed_cause == "contract_change"
        assert events[0].cause_evidence["contract_version"] == 1

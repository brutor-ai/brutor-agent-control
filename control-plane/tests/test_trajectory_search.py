# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Trajectory search over Run > Step > Turn > Action.

These tests exist to pin *semantics*, not SQL. The two that matter most are
the honesty properties, because they are the ones a well-meaning refactor
breaks:

  * a run whose client asserted no phases must report `unphased` — never a
    synthetic single phase;
  * turn grouping must fall back to the SAME inference the run-ledger rollup
    uses, or the trajectory view and `turn_count` describe the same run
    differently, which is the "two surfaces disagree" bug class we have already
    shipped once.

They run against real Postgres via the shared `db` fixture, because the
queries lean on window functions and `FILTER`, which SQLite does not
faithfully reproduce.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database.models.proxy_log import ProxyLog
from app.services import trajectory_search
from tests.admin_endpoint_harness import throwaway_installed_pg_database

# Postgres only, and deliberately so: these queries use FILTER, ARRAY_AGG,
# MAKE_INTERVAL and window frames. Running them on the suite's SQLite would
# either fail to parse or — worse — parse and quietly mean something else,
# which is the one outcome a semantics test must never allow.
pytestmark = pytest.mark.skipif(
    os.environ.get("BRUTOR_PG_TESTS") != "1",
    reason="needs Postgres; set BRUTOR_PG_TESTS=1 and BRUTOR_PG_TEST_URL",
)

TENANT = "traj-test"


@pytest.fixture(scope="module")
def pg_url():
    yield from throwaway_installed_pg_database("trajectory_search")


@pytest.fixture(scope="module")
def pg_session(pg_url):
    engine = create_engine(pg_url)
    Session = sessionmaker(bind=engine)
    s = Session()
    # proxy_logs.tenant_id is a real FK, so the test tenant has to exist.
    # Created here rather than assumed, so the module is self-contained on a
    # freshly migrated database.
    s.execute(
        text(
            "INSERT INTO tenants "
            "  (id, name, display_name, primary_contact_email, is_active, "
            "   is_default, is_trial, suspended, features, settings) "
            "VALUES (:t, :t, :t, 'traj@test.invalid', true, "
            "        false, false, false, '{}'::json, '{}'::json) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"t": TENANT},
    )
    s.commit()
    yield s
    # Leave no residue: every row this module writes carries our tenant.
    s.rollback()
    s.execute(text("DELETE FROM proxy_logs WHERE tenant_id = :t"), {"t": TENANT})
    s.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    s.commit()
    s.close()
    engine.dispose()


@pytest.fixture
def db(pg_session):
    """Name matches the suite-wide fixture so the test bodies read the same."""
    pg_session.execute(text("DELETE FROM proxy_logs WHERE tenant_id = :t"), {"t": TENANT})
    pg_session.commit()
    return pg_session


def _action(
    *,
    run: str,
    seq: int,
    proxy_type: str = "llm",
    subtype: str | None = None,
    capability: str | None = None,
    model: str | None = None,
    step_id: str | None = None,
    step_name: str | None = None,
    turn_id: str | None = None,
    cost: float = 0.0,
    blocked: bool = False,
    pii: bool = False,
) -> ProxyLog:
    """One governed call, `seq` seconds into the run."""
    return ProxyLog(
        id=f"pl-{uuid.uuid4().hex[:12]}",
        tenant_id=TENANT,
        timestamp=datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
        + timedelta(seconds=seq),
        proxy_type=proxy_type,
        proxy_subtype=subtype,
        root_task_id=run,
        step_id=step_id,
        step_name=step_name,
        turn_id=turn_id,
        mcp_capability_name=capability,
        llm_model_used=model,
        llm_estimated_cost_usd=cost,
        guardrail_blocked=blocked,
        pii_detected=pii,
        argument_policy_denied=False,
        end_user_session_id="",
        http_method="POST",
        endpoint="/v1/proxy/llm/chat/completions",
        success=True,
    )


@pytest.fixture
def phased_run(db):
    """Two phases, three turns, seven actions — asserted throughout."""
    run = f"run-{uuid.uuid4().hex[:8]}"
    rows = [
        _action(run=run, seq=0, model="gpt-5.2", step_id="s1", step_name="research", turn_id="t1"),
        _action(run=run, seq=1, proxy_type="mcp", capability="crm_get_deal", step_id="s1", step_name="research", turn_id="t1", cost=0.0),
        _action(run=run, seq=2, model="gpt-5.2", step_id="s1", step_name="research", turn_id="t2", cost=0.02),
        _action(run=run, seq=3, proxy_type="mcp", capability="crm_list_pipeline", step_id="s1", step_name="research", turn_id="t2"),
        _action(run=run, seq=4, model="gpt-5.2", step_id="s2", step_name="settle", turn_id="t3", cost=0.05),
        _action(run=run, seq=5, proxy_type="mcp", capability="issue_payment", step_id="s2", step_name="settle", turn_id="t3"),
        _action(run=run, seq=6, model="gpt-5.2", step_id="s2", step_name="settle", turn_id="t3", cost=0.01),
    ]
    db.add_all(rows)
    db.commit()
    return run


@pytest.fixture
def unphased_run(db):
    """No step or turn assertions — the common case for an off-the-shelf agent."""
    run = f"run-{uuid.uuid4().hex[:8]}"
    rows = [
        _action(run=run, seq=0, model="gpt-5.2"),
        _action(run=run, seq=1, proxy_type="mcp", capability="crm_get_deal"),
        _action(run=run, seq=2, model="gpt-5.2"),
        _action(run=run, seq=3, proxy_type="mcp", capability="slack_post_message"),
        # An embedding is work *inside* a turn, never a turn of its own —
        # the rollup excludes it and so must the grouping.
        _action(run=run, seq=4, subtype="embeddings", model="text-embedding-3-small"),
    ]
    db.add_all(rows)
    db.commit()
    return run


# ── grouping ────────────────────────────────────────────────────────────


def test_turns_group_by_asserted_id(db, phased_run):
    turns = trajectory_search.turns_in_run(
        db, tenant_id=TENANT, root_task_id=phased_run
    )
    assert [t.turn_id for t in turns] == ["t1", "t2", "t3"]
    assert [t.action_count for t in turns] == [2, 2, 3]
    assert turns[0].tools == ["crm_get_deal"]
    assert turns[2].opened_by_model == "gpt-5.2"


def test_step_filter_narrows_to_one_phase(db, phased_run):
    turns = trajectory_search.turns_in_run(
        db, tenant_id=TENANT, root_task_id=phased_run, step_id="s2"
    )
    assert [t.turn_id for t in turns] == ["t3"]
    assert turns[0].tools == ["issue_payment"]


def test_unasserted_turns_are_inferred_like_the_rollup(db, unphased_run):
    """The property that keeps two surfaces from disagreeing.

    `run_ledger`'s rollup infers a turn per non-embedding model call. If this
    grouping instead lumped everything into one NULL turn, the drill-down
    would contradict `turn_count` on the very same row.
    """
    turns = trajectory_search.turns_in_run(
        db, tenant_id=TENANT, root_task_id=unphased_run
    )
    # Two model calls -> two turns; the embedding joins the second, not a third.
    assert len(turns) == 2
    assert all(t.turn_id is None for t in turns)
    assert [t.inferred_turn for t in turns] == [1, 2]
    assert turns[1].action_count == 3  # llm + tool + embedding


# ── phases ──────────────────────────────────────────────────────────────


def test_cost_by_step_splits_the_bill(db, phased_run):
    steps = trajectory_search.cost_by_step(
        db, tenant_id=TENANT, root_task_id=phased_run
    )
    assert [s["step_name"] for s in steps] == ["research", "settle"]
    assert steps[0]["turns"] == 2 and steps[0]["actions"] == 4
    assert steps[1]["cost_usd"] == pytest.approx(0.06)


def test_unphased_run_reports_no_steps_rather_than_one(db, unphased_run):
    """Absence of an assertion is not evidence of a single phase."""
    steps = trajectory_search.cost_by_step(
        db, tenant_id=TENANT, root_task_id=unphased_run
    )
    assert steps == []


# ── consequences ────────────────────────────────────────────────────────


def test_consequences_are_bounded_and_ordered(db):
    run = f"run-{uuid.uuid4().hex[:8]}"
    db.add_all(
        [
            _action(run=run, seq=0, model="gpt-5.2"),
            _action(run=run, seq=1, proxy_type="mcp", capability="lookup", pii=True),
            _action(run=run, seq=2, proxy_type="mcp", capability="export_rows"),
            _action(run=run, seq=3, proxy_type="mcp", capability="send_email"),
        ]
    )
    db.commit()

    pairs = trajectory_search.consequences_of_trips(
        db, tenant_id=TENANT, root_task_id=run, within=10
    )
    caps = [p["after_capability"] for p in pairs]
    assert caps == ["export_rows", "send_email"]
    assert [p["actions_apart"] for p in pairs] == [1, 2]


def test_within_bounds_the_lookahead(db):
    run = f"run-{uuid.uuid4().hex[:8]}"
    rows = [_action(run=run, seq=0, proxy_type="mcp", capability="lookup", blocked=True)]
    rows += [
        _action(run=run, seq=i, proxy_type="mcp", capability=f"tool_{i}")
        for i in range(1, 6)
    ]
    db.add_all(rows)
    db.commit()

    near = trajectory_search.consequences_of_trips(
        db, tenant_id=TENANT, root_task_id=run, within=2
    )
    assert [p["actions_apart"] for p in near] == [1, 2]


def test_a_trip_on_the_last_action_has_no_consequences(db):
    """Not a bug: nothing ran after it. The empty result is the honest answer."""
    run = f"run-{uuid.uuid4().hex[:8]}"
    db.add_all(
        [
            _action(run=run, seq=0, model="gpt-5.2"),
            _action(run=run, seq=1, proxy_type="mcp", capability="delete_rows", blocked=True),
        ]
    )
    db.commit()
    assert (
        trajectory_search.consequences_of_trips(
            db, tenant_id=TENANT, root_task_id=run, within=10
        )
        == []
    )


# ── fleet search ────────────────────────────────────────────────────────


def test_find_actions_filters_by_capability_and_phase(db, phased_run):
    hits = trajectory_search.find_actions(
        db, tenant_id=TENANT, capability="issue_payment", since_hours=8760
    )
    assert len(hits) == 1
    assert hits[0]["step_name"] == "settle"
    assert hits[0]["root_task_id"] == phased_run


def test_find_actions_only_blocked(db):
    run = f"run-{uuid.uuid4().hex[:8]}"
    db.add_all(
        [
            _action(run=run, seq=0, proxy_type="mcp", capability="ok_tool"),
            _action(run=run, seq=1, proxy_type="mcp", capability="bad_tool", blocked=True),
        ]
    )
    db.commit()
    hits = trajectory_search.find_actions(
        db, tenant_id=TENANT, only_blocked=True, since_hours=8760
    )
    assert {h["mcp_capability_name"] for h in hits} == {"bad_tool"}


def test_tenant_isolation(db, phased_run):
    assert (
        trajectory_search.turns_in_run(
            db, tenant_id="someone-else", root_task_id=phased_run
        )
        == []
    )

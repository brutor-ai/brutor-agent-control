# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
CP-8 — `GET /v1/admin/runs/unattributed` clusters in SQL.

The handler used to `.all()` every matching run and cluster in Python.
`period_hours=0` is a documented "all time" option and `limit` bounds
only the clusters *returned*, never the rows scanned — so on exactly the
estates this screen exists for (lots of traffic not yet registered as an
AI System) it materialised the whole unattributed history as ORM objects
in the process that also runs the retention sweeper and the audit export.

The behaviour must be identical after moving the aggregation into the
database, so both halves are pinned: the numbers, and the bound on what
Python is handed.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.ai_system_runs import create_ai_system_runs_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness

TENANT = "t1"


def _now() -> datetime:
    """The wall clock, read when used — never at import (tests/test_no_import_time_clock.py)."""
    return datetime.now(timezone.utc)


URL = "/v1/admin/runs/unattributed"


def _run(idx, *, tenant=TENANT, actor_type="agent", actor_id="a1",
         hours_ago=1, cost=0.5, ai_system_id=None, is_replay=False):
    return AISystemRun(
        id=f"run-{tenant}-{idx}",
        tenant_id=tenant,
        root_task_id=f"root-{tenant}-{idx}",
        started_at=_now() - timedelta(hours=hours_ago),
        actor_type=actor_type,
        actor_id=actor_id,
        total_cost_usd=cost,
        ai_system_id=ai_system_id,
        is_replay=is_replay,
        terminal_state="completed",
        finalized_by="explicit",
    )


@pytest.fixture()
def h():
    harness = build_admin_harness(
        [create_ai_system_runs_router(BrutorLogger("test-unattributed"))],
        logger_name="test-unattributed",
    )

    def seed(s):
        # An AI System is a resource group (Design Note 002); one run below is
        # attributed to it and must therefore NOT appear in this list.
        s.add(ResourceGroup(
            id="ais-1", tenant_id=TENANT, name="Registered System",
            hierarchy_level=0, inherit_resources=True, is_active=True,
            group_type=GroupType.AI_SYSTEM,
        ))
        s.flush()
        # Cluster A: 4 runs, the biggest.
        for i in range(4):
            s.add(_run(f"a{i}", actor_id="agent-alpha", hours_ago=i + 1, cost=1.0))
        # Cluster B: 2 runs, one of them well outside a 24h window.
        s.add(_run("b0", actor_id="agent-beta", hours_ago=2, cost=0.25))
        s.add(_run("b1", actor_id="agent-beta", hours_ago=500, cost=0.25))
        # Cluster C: one run with no actor at all — the 'unknown:-' key.
        s.add(_run("c0", actor_type=None, actor_id=None, hours_ago=3, cost=0.1))
        # Excluded: attributed to a system, and a replay.
        s.add(_run("x0", actor_id="agent-alpha", ai_system_id="ais-1", cost=99))
        s.add(_run("x1", actor_id="agent-alpha", is_replay=True, cost=99))
        # Excluded: another tenant entirely.
        s.add(_run("t2", tenant="t2", actor_id="agent-alpha", cost=99))

    harness.seed(seed)
    yield harness
    harness.close()


def test_clusters_by_actor_with_correct_totals(h):
    r = h.client.get(f"{URL}?period_hours=0", headers=h.headers(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()

    # 4 + 2 + 1 = 7; the attributed run, the replay and tenant t2 are out.
    assert body["total_unattributed_runs"] == 7
    assert body["total_cost_usd"] == pytest.approx(4.0 + 0.5 + 0.1)

    names = [g["resource_group_name"] for g in body["groups"]]
    assert names[0] == "agent:agent-alpha"  # ordered by run count, desc
    assert set(names) == {"agent:agent-alpha", "agent:agent-beta", "unknown:-"}

    alpha = next(g for g in body["groups"] if g["resource_group_name"] == "agent:agent-alpha")
    assert alpha["runs"] == 4
    assert alpha["total_cost_usd"] == pytest.approx(4.0)
    assert alpha["first_seen"] < alpha["last_seen"]
    assert len(alpha["sample_root_task_ids"]) == 4
    assert all(s.startswith("root-t1-a") for s in alpha["sample_root_task_ids"])


def test_samples_are_capped_at_five_per_cluster(h):
    """The sample list is a preview, not a page — the window function
    keeps five per cluster so a 100k-run cluster still costs five rows."""
    s = h.Factory()
    try:
        for i in range(20):
            s.add(_run(f"big{i}", actor_id="agent-big", hours_ago=i + 1))
        s.commit()
    finally:
        s.close()
    body = h.client.get(f"{URL}?period_hours=0", headers=h.headers(TENANT)).json()
    big = next(g for g in body["groups"] if g["resource_group_name"] == "agent:agent-big")
    assert big["runs"] == 20
    assert len(big["sample_root_task_ids"]) == 5


def test_period_hours_bounds_the_window(h):
    body = h.client.get(f"{URL}?period_hours=24", headers=h.headers(TENANT)).json()
    # The 500h-old beta run drops out; everything else is within a day.
    assert body["total_unattributed_runs"] == 6
    beta = next(g for g in body["groups"] if g["resource_group_name"] == "agent:agent-beta")
    assert beta["runs"] == 1


def test_limit_caps_groups_but_not_the_totals(h):
    """`limit` is a page size for clusters. The funnel number an operator
    reads must still count every unattributed run, or asking for fewer
    groups would appear to shrink the backlog."""
    body = h.client.get(f"{URL}?period_hours=0&limit=1", headers=h.headers(TENANT)).json()
    assert len(body["groups"]) == 1
    assert body["groups"][0]["resource_group_name"] == "agent:agent-alpha"
    assert body["total_unattributed_runs"] == 7
    assert body["total_cost_usd"] == pytest.approx(4.6)


def test_no_unattributed_runs_is_an_empty_answer_not_an_error(h):
    body = h.client.get(f"{URL}?period_hours=1", headers=h.headers(TENANT)).json()
    assert body["total_unattributed_runs"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["groups"] == []


def test_the_handler_never_materialises_runs(h):
    """The point of the fix.

    Counting emitted SQL would not catch a regression (the Python version
    also ran one query); what matters is that no query returns the run
    rows themselves. Asserting on the statements means a revert to
    `db.query(AISystemRun).…all()` fails here rather than in production
    on the one estate large enough to OOM.
    """
    from sqlalchemy import event

    statements = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(h.engine, "before_cursor_execute", _record)
    try:
        r = h.client.get(f"{URL}?period_hours=0", headers=h.headers(TENANT))
    finally:
        event.remove(h.engine, "before_cursor_execute", _record)
    assert r.status_code == 200

    run_reads = [s for s in statements if "ai_system_runs" in s]
    assert run_reads, "no query touched ai_system_runs — did the route change?"
    for stmt in run_reads:
        assert (
            "count(" in stmt.lower()
            or "row_number()" in stmt.lower()
        ), f"unbounded entity select against ai_system_runs: {stmt}"

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the A2A delegation-graph service.

Exercises the reconstruction logic against an in-memory SQLite DB so
the tests run without any external service. The service consumes
proxy_logs rows that the Rust proxy writes at runtime; here we seed
rows directly to simulate a variety of graph shapes:

  * single-step root (depth=0 only)
  * two-step linear chain (depth 0 → 1)
  * wider fan-out tree (depth 0 → {1a, 1b})
  * failure patterns (last step errors, middle step errors)

The endpoint layer (feature flag, RBAC, tenant scoping) is covered
by `test_a2a_delegation_graphs_endpoints.py`. Keeping the two test
modules separate lets us iterate on either layer without full-
harness setup overhead.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database import Base
from app.database.models.proxy_log import ProxyLog
from app.services.a2a.delegation_graph_service import (
    A2ADelegationGraphService,
    _percentile,
)


# -----------------------------------------------------------------------------
# Harness
# -----------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    # List summaries also name the AI Systems a graph's run
    # involved (ai_system_runs + ai_system_run_segments, names from
    # resource_groups).
    Base.metadata.create_all(bind=engine, tables=[
        ProxyLog.__table__,
        Base.metadata.tables["resource_groups"],
        Base.metadata.tables["ai_system_runs"],
        Base.metadata.tables["ai_system_run_segments"],
    ])
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def service():
    return A2ADelegationGraphService(BrutorLogger("test-a2a-graph"))


# -----------------------------------------------------------------------------
# Seed helpers
# -----------------------------------------------------------------------------


def _step(
    tenant_id: str,
    root_id: str,
    *,
    current_task_id: str,
    parent_task_id: str | None = None,
    depth: int = 0,
    direction: str = "outbound",
    timestamp: datetime,
    duration_ms: float = 100.0,
    success: bool = True,
    status: int = 200,
    card_id: str | None = None,
    endpoint: str = "/v1/proxy/a2a/outbound",
    error: str | None = None,
    correlation_id: str = "corr-1",
) -> ProxyLog:
    return ProxyLog(
        id=f"log-{ULID()}",
        tenant_id=tenant_id,
        timestamp=timestamp,
        proxy_type="a2a",
        proxy_subtype=direction,
        correlation_id=correlation_id,
        end_user_session_id=correlation_id,
        server_id=card_id,
        http_method="POST",
        endpoint=endpoint,
        http_status_code=status,
        success=success,
        error_message=error,
        duration_ms=duration_ms,
        root_task_id=root_id,
        delegation_depth=depth,
        current_task_id=current_task_id,
        parent_task_id=parent_task_id,
    )


# =============================================================================
# _percentile — pure function
# =============================================================================


class TestPercentile:
    def test_empty_returns_none(self):
        assert _percentile([], 0.5) is None

    def test_single_element_ignores_q(self):
        assert _percentile([42.0], 0.0) == 42.0
        assert _percentile([42.0], 0.95) == 42.0

    def test_even_length_median(self):
        # Linear interpolation: position 1.5 -> 2.5 (between 2 and 3).
        assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5

    def test_p95_on_20_samples(self):
        # Linear interpolation on [1..20] at q=0.95: pos 0.95*19 = 18.05
        # -> values[18] + 0.05 * (values[19] - values[18]) = 19 + 0.05 = 19.05
        vals = [float(i) for i in range(1, 21)]
        assert round(_percentile(vals, 0.95), 2) == 19.05

    def test_clamps_out_of_range(self):
        vals = [1.0, 2.0, 3.0]
        assert _percentile(vals, -0.1) == 1.0
        assert _percentile(vals, 1.5) == 3.0


# =============================================================================
# list_graphs — aggregation across steps
# =============================================================================


class TestListGraphs:
    def test_empty_returns_empty(self, session, service):
        graphs, total = service.list_graphs(session, tenant_id="t1")
        assert graphs == []
        assert total == 0

    def test_single_step_graph(self, session, service):
        ts = datetime.now(timezone.utc)
        root = str(ULID())
        session.add(_step(
            "t1", root,
            current_task_id=root, depth=0,
            direction="outbound",
            timestamp=ts, duration_ms=150.0,
        ))
        session.commit()

        graphs, total = service.list_graphs(session, tenant_id="t1")
        assert total == 1
        g = graphs[0]
        assert g["root_task_id"] == root
        assert g["total_steps"] == 1
        assert g["max_depth"] == 0
        assert g["outbound_steps"] == 1
        assert g["inbound_steps"] == 0
        assert g["success_steps"] == 1
        assert g["failure_steps"] == 0
        assert g["overall_success"] is True
        # Single-step duration falls back to the step's own duration_ms
        # because started_at == ended_at.
        assert g["duration_ms"] == 150.0

    def test_two_step_linear_chain_spans_both_timestamps(self, session, service):
        root = str(ULID())
        t0 = datetime.now(timezone.utc)
        t1 = t0 + timedelta(milliseconds=250)
        parent_id = str(ULID())
        child_id = str(ULID())

        session.add(_step(
            "t1", root,
            current_task_id=parent_id, depth=0,
            direction="outbound", timestamp=t0, duration_ms=50.0,
        ))
        session.add(_step(
            "t1", root,
            current_task_id=child_id, parent_task_id=parent_id, depth=1,
            direction="inbound", timestamp=t1, duration_ms=80.0,
        ))
        session.commit()

        graphs, total = service.list_graphs(session, tenant_id="t1")
        assert total == 1
        g = graphs[0]
        assert g["total_steps"] == 2
        assert g["max_depth"] == 1
        assert g["inbound_steps"] == 1
        assert g["outbound_steps"] == 1
        # Span the whole chain, not just one step's own duration.
        assert 240 < g["duration_ms"] < 260

    def test_fan_out_tree_counts_siblings(self, session, service):
        root = str(ULID())
        t0 = datetime.now(timezone.utc)
        parent_id = str(ULID())

        session.add(_step(
            "t1", root,
            current_task_id=parent_id, depth=0,
            direction="outbound", timestamp=t0,
        ))
        for i in range(3):
            session.add(_step(
                "t1", root,
                current_task_id=str(ULID()), parent_task_id=parent_id, depth=1,
                direction="inbound",
                timestamp=t0 + timedelta(milliseconds=100 * (i + 1)),
            ))
        session.commit()

        graphs, total = service.list_graphs(session, tenant_id="t1")
        assert total == 1
        assert graphs[0]["total_steps"] == 4
        assert graphs[0]["max_depth"] == 1
        assert graphs[0]["inbound_steps"] == 3

    def test_overall_success_false_when_any_step_fails(self, session, service):
        root = str(ULID())
        t0 = datetime.now(timezone.utc)
        session.add(_step(
            "t1", root,
            current_task_id=str(ULID()), depth=0,
            timestamp=t0,
        ))
        session.add(_step(
            "t1", root,
            current_task_id=str(ULID()), depth=1,
            timestamp=t0 + timedelta(milliseconds=50),
            success=False, status=502, error="remote down",
        ))
        session.commit()

        graphs, total = service.list_graphs(session, tenant_id="t1")
        assert graphs[0]["overall_success"] is False
        assert graphs[0]["success_steps"] == 1
        assert graphs[0]["failure_steps"] == 1

    def test_success_filter(self, session, service):
        t0 = datetime.now(timezone.utc)
        for success in (True, False, True):
            root = str(ULID())
            session.add(_step(
                "t1", root,
                current_task_id=root, depth=0,
                timestamp=t0 + timedelta(milliseconds=1),
                success=success,
            ))
        session.commit()

        ok, ok_total = service.list_graphs(session, tenant_id="t1", success=True)
        fail, fail_total = service.list_graphs(session, tenant_id="t1", success=False)
        assert ok_total == 2
        assert fail_total == 1

    def test_tenant_isolation(self, session, service):
        t0 = datetime.now(timezone.utc)
        for tid in ("t1", "t2"):
            root = str(ULID())
            session.add(_step(
                tid, root,
                current_task_id=root, depth=0,
                timestamp=t0,
            ))
        session.commit()

        g1, n1 = service.list_graphs(session, tenant_id="t1")
        g2, n2 = service.list_graphs(session, tenant_id="t2")
        assert n1 == 1 and g1[0]["tenant_id"] == "t1"
        assert n2 == 1 and g2[0]["tenant_id"] == "t2"

    def test_pagination_uses_deterministic_newest_first_order(self, session, service):
        t0 = datetime.now(timezone.utc)
        roots = []
        for i in range(5):
            root = str(ULID())
            roots.append(root)
            session.add(_step(
                "t1", root,
                current_task_id=root, depth=0,
                timestamp=t0 + timedelta(seconds=i),
            ))
        session.commit()

        page1, total = service.list_graphs(session, tenant_id="t1", limit=2, offset=0)
        page2, _ = service.list_graphs(session, tenant_id="t1", limit=2, offset=2)
        assert total == 5
        assert len(page1) == 2
        assert len(page2) == 2
        # Newest first: last root appended is newest.
        assert page1[0]["root_task_id"] == roots[-1]
        assert page1[1]["root_task_id"] == roots[-2]
        assert page2[0]["root_task_id"] == roots[-3]


# =============================================================================
# get_graph — full tree
# =============================================================================


class TestGetGraph:
    def test_missing_returns_none(self, session, service):
        assert service.get_graph(session, tenant_id="t1", root_task_id="nope") is None

    def test_returns_steps_ordered_by_depth_then_timestamp(self, session, service):
        root = str(ULID())
        t0 = datetime.now(timezone.utc)
        parent_id = str(ULID())
        child1 = str(ULID())
        child2 = str(ULID())

        session.add(_step(
            "t1", root, current_task_id=child2, parent_task_id=parent_id,
            depth=1, timestamp=t0 + timedelta(milliseconds=200),
        ))
        session.add(_step(
            "t1", root, current_task_id=parent_id, depth=0,
            timestamp=t0,
        ))
        session.add(_step(
            "t1", root, current_task_id=child1, parent_task_id=parent_id,
            depth=1, timestamp=t0 + timedelta(milliseconds=100),
        ))
        session.commit()

        g = service.get_graph(session, tenant_id="t1", root_task_id=root)
        assert g["total_steps"] == 3
        assert g["max_depth"] == 1
        # Depth 0 first, then depth-1 children in timestamp order.
        assert [h["depth"] for h in g["steps"]] == [0, 1, 1]
        assert g["steps"][0]["current_task_id"] == parent_id
        assert g["steps"][1]["current_task_id"] == child1
        assert g["steps"][2]["current_task_id"] == child2

    def test_tenant_isolation(self, session, service):
        t0 = datetime.now(timezone.utc)
        root = str(ULID())
        session.add(_step("t1", root, current_task_id=root, depth=0, timestamp=t0))
        session.commit()

        assert service.get_graph(session, tenant_id="t2", root_task_id=root) is None


# =============================================================================
# compute_stats
# =============================================================================


class TestComputeStats:
    def test_empty_window_returns_zero_stats(self, session, service):
        s = service.compute_stats(session, tenant_id="t1")
        assert s["total_steps"] == 0
        assert s["total_graphs"] == 0
        assert s["success_rate"] is None
        assert s["avg_steps_per_graph"] is None
        assert s["p50_duration_ms"] is None
        assert s["top_cards"] == []

    def test_counts_and_percentiles(self, session, service):
        t0 = datetime.now(timezone.utc)
        root = str(ULID())
        # Five steps in one graph.
        for i, dur in enumerate([10.0, 20.0, 30.0, 40.0, 50.0]):
            session.add(_step(
                "t1", root,
                current_task_id=str(ULID()), depth=i,
                timestamp=t0 + timedelta(milliseconds=i),
                duration_ms=dur, card_id="card-A",
                direction="outbound" if i % 2 == 0 else "inbound",
            ))
        session.commit()

        s = service.compute_stats(session, tenant_id="t1")
        assert s["total_steps"] == 5
        assert s["total_graphs"] == 1
        assert s["inbound_steps"] == 2
        assert s["outbound_steps"] == 3
        assert s["success_steps"] == 5
        assert s["failure_steps"] == 0
        assert s["success_rate"] == 1.0
        assert s["avg_steps_per_graph"] == 5.0
        assert s["max_depth_observed"] == 4
        assert s["avg_duration_ms"] == 30.0
        assert s["p50_duration_ms"] == 30.0
        # p95 on [10,20,30,40,50]: pos 0.95*4 = 3.8 → 40 + 0.8*10 = 48.0
        assert round(s["p95_duration_ms"], 1) == 48.0
        assert s["top_cards"] == [{"card_id": "card-A", "count": 5}]

    def test_top_cards_limit(self, session, service):
        t0 = datetime.now(timezone.utc)
        for card, n in [("A", 3), ("B", 2), ("C", 5), ("D", 1)]:
            for _ in range(n):
                root = str(ULID())
                session.add(_step(
                    "t1", root,
                    current_task_id=root, depth=0,
                    timestamp=t0,
                    card_id=f"card-{card}",
                ))
        session.commit()

        s = service.compute_stats(session, tenant_id="t1", top_n_cards=2)
        names = [c["card_id"] for c in s["top_cards"]]
        assert names == ["card-C", "card-A"]

    def test_stats_never_materialise_the_window(self, session, service):
        """CP-7 — the aggregation belongs in SQL.

        `since` and `until` both default to None at the route, so the
        *default* stats request has no window. The old implementation did
        `q.all()` on `proxy_logs` and clustered in Python, which on an
        estate with months of A2A traffic is a full-table scan into the
        control-plane heap.

        Counting queries would not catch a revert (the old version ran
        one). What distinguishes them is whether any statement selects
        the row bodies, so the statements themselves are inspected.
        """
        from sqlalchemy import event

        t0 = datetime.now(timezone.utc)
        for i in range(6):
            session.add(_step(
                "t1", str(ULID()), current_task_id=str(ULID()), depth=0,
                timestamp=t0, duration_ms=float(i), card_id="card-A",
            ))
        session.commit()

        statements = []

        def _record(conn, cursor, statement, params, context, executemany):
            statements.append(" ".join(statement.split()).lower())

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", _record)
        try:
            stats = service.compute_stats(session, tenant_id="t1")
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        assert stats["total_steps"] == 6
        reads = [s for s in statements if "proxy_logs" in s]
        assert reads, "no query touched proxy_logs — did the service change?"
        for stmt in reads:
            # Every read is an aggregate, a GROUP BY, or a bounded
            # percentile probe. None of them is a bare entity select.
            assert (
                "count(" in stmt
                or "group by" in stmt
                or " limit " in stmt
            ), f"unbounded entity select against proxy_logs: {stmt}"
            assert "proxy_logs.request_body_preview" not in stmt, (
                f"stats query is loading row bodies: {stmt}"
            )

    def test_window_filter(self, session, service):
        t_old = datetime.now(timezone.utc) - timedelta(days=30)
        t_new = datetime.now(timezone.utc)
        for ts in (t_old, t_new):
            session.add(_step(
                "t1", str(ULID()),
                current_task_id=str(ULID()), depth=0,
                timestamp=ts,
            ))
        session.commit()

        s = service.compute_stats(
            session,
            tenant_id="t1",
            since=datetime.now(timezone.utc) - timedelta(days=1),
        )
        assert s["total_steps"] == 1

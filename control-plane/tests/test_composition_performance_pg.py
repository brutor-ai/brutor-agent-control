# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Composition performance on a large tenant (Postgres only)

The composition index (`CompositionIndex`) is one tenant-wide pass that the
Composition endpoint, the AI Systems list and every health computation
read. On a demo tenant it is instant; this module measures it on a
realistic LARGE tenant and holds the list endpoint to a budget, so a
change that makes the pass scale with the tenant's history (rows pulled
into Python instead of aggregated in SQL, a per-system query inside a loop)
fails here rather than on a customer's fleet board.

## The fixture (seeded with set-based SQL, one scratch database per run)

  * 200 AI Systems, each with one child resource group, one member agent and
    one agent card;
  * 2,000 edges and grants: 1,000 declared `shared_service` dependencies
    (every system depends on the next five; the first is critical, forming
    critical chains of ten systems) and 1,000 `a2a_call` grants to the same
    cards;
  * 50,000 runs and 50,000 run segments in the window (one delegation per
    run, ~4 % unanswered);
  * 200,000 proxy_logs rows in the window — LLM, MCP and A2A steps, a
    quarter of them the delegate's (attributed through its child group);
  * a materialised health row per system.

## What is timed

  * `GET /v1/admin/ai-systems?limit=100` (the fleet board — the budget);
  * `GET /v1/admin/ai-systems/{id}/composition` (one system's tab);
  * `compute_health` for one root system.

Runs only with ``BRUTOR_PG_TESTS=1`` (``BRUTOR_PG_TEST_URL`` names the
server; a throwaway database is created and dropped). Timings print with
``-s``. The budget is deliberately generous against the measured time so
it fails on a structural regression (an order of magnitude), not on a
slow CI runner.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from tests.pg_test_url import pg_test_url

pytestmark = pytest.mark.skipif(
    os.environ.get("BRUTOR_PG_TESTS") != "1",
    reason="Postgres-only performance fixture; set BRUTOR_PG_TESTS=1",
)

from app.api.v1.endpoints.admin.ai_system_composition import (  # noqa: E402
    create_ai_system_composition_router,
)
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router  # noqa: E402
from app.core.logging_system import BrutorLogger  # noqa: E402
from tests.admin_endpoint_harness import build_admin_harness  # noqa: E402

TENANT = "t1"
N_SYSTEMS = 200
DEPS_PER_SYSTEM = 5  # 1,000 edges + 1,000 grants = 2,000
N_RUNS = 50_000  # = segments (one delegation per run)
N_LOGS = 200_000
WINDOW_DAYS = 7

#: Wall-time budgets on this fixture (seconds, best of three, warm).
#: Measured with this module on a loaded dev laptop, before →
#: after the fix (the before code also fails every row budget: 50,000 rows
#: from one statement): list 5.3 s → 1.0 s; composition 1.8 s → 0.9 s;
#: compute_health 2.0 s → 0.4 s. Unloaded, after: 0.6 / 0.55 / 0.23 s.
#: Generous against "after" so a slow runner does not flake; the statement
#: and row budgets below are the deterministic guards.
LIST_BUDGET_S = 2.0
COMPOSITION_BUDGET_S = 2.5
HEALTH_BUDGET_S = 1.5
#: No statement may return more rows than this — the fixture's history is
#: 50,000 segments and 200,000 steps, its aggregates a few thousand rows.
MAX_ROWS_PER_STATEMENT = 10_000
#: Statements per call, independent of the number of systems.
LIST_MAX_STATEMENTS = 45
COMPOSITION_MAX_STATEMENTS = 80
HEALTH_MAX_STATEMENTS = 90

SEED_SQL = [
    # --- AI Systems, their child groups, agents, cards ----------------------
    f"""
    INSERT INTO resource_groups (id, tenant_id, name, display_name, description, hierarchy_level,
        inherit_from_parent, is_active, is_system, group_type, system_kind, eu_ai_act_risk_tier)
    SELECT 'ps-sys-' || lpad(i::text, 3, '0'), '{TENANT}', 'ps-sys-' || i, 'Perf System ' || i, 'perf',
           0, true, true, false, 'AI_SYSTEM',
           CASE WHEN i % 4 = 0 THEN 'service' ELSE 'agent' END,
           CASE WHEN i % 10 = 0 THEN 'high' ELSE 'limited' END
    FROM generate_series(0, {N_SYSTEMS - 1}) i
    """,
    f"""
    INSERT INTO resource_groups (id, tenant_id, name, description, hierarchy_level, parent_group_id,
        inherit_from_parent, is_active, is_system, group_type)
    SELECT 'ps-grp-' || lpad(i::text, 3, '0'), '{TENANT}', 'ps-grp-' || i, 'perf child', 1,
           'ps-sys-' || lpad(i::text, 3, '0'), true, true, false, 'CUSTOM'
    FROM generate_series(0, {N_SYSTEMS - 1}) i
    """,
    f"""
    INSERT INTO agent_identities (id, tenant_id, name)
    SELECT 'ps-agent-' || lpad(i::text, 3, '0'), '{TENANT}', 'perf-agent-' || i
    FROM generate_series(0, {N_SYSTEMS - 1}) i
    """,
    f"""
    INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id)
    SELECT '{TENANT}', 'ps-agent-' || lpad(i::text, 3, '0'), 'ps-sys-' || lpad(i::text, 3, '0')
    FROM generate_series(0, {N_SYSTEMS - 1}) i
    """,
    f"""
    INSERT INTO agent_cards (id, tenant_id, name, card_json)
    SELECT 'ps-card-' || lpad(i::text, 3, '0'), '{TENANT}', 'perf-card-' || i, '{{}}'::json
    FROM generate_series(0, {N_SYSTEMS - 1}) i
    """,
    # --- 1,000 edges + 1,000 grants: system i depends on i+1 … i+5 ----------
    f"""
    INSERT INTO ai_system_dependencies (id, tenant_id, ai_system_id, role, depends_on_system_id,
        agent_card_id, criticality, fallback)
    SELECT 'ps-dep-' || i || '-' || k, '{TENANT}', 'ps-sys-' || lpad(i::text, 3, '0'), 'shared_service',
           'ps-sys-' || lpad(((i + k) % {N_SYSTEMS})::text, 3, '0'),
           'ps-card-' || lpad(((i + k) % {N_SYSTEMS})::text, 3, '0'),
           CASE WHEN k = 1 AND i % 10 <> 9 THEN 'critical' ELSE 'degradable' END,
           CASE WHEN k = 1 AND i % 10 <> 9 THEN NULL ELSE 'manual review' END
    FROM generate_series(0, {N_SYSTEMS - 1}) i, generate_series(1, {DEPS_PER_SYSTEM}) k
    """,
    f"""
    INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect)
    SELECT 'ps-grant-' || i || '-' || k, '{TENANT}', 'ps-agent-' || lpad(i::text, 3, '0'), 'a2a_call',
           'ps-card-' || lpad(((i + k) % {N_SYSTEMS})::text, 3, '0'), 'allow'
    FROM generate_series(0, {N_SYSTEMS - 1}) i, generate_series(1, {DEPS_PER_SYSTEM}) k
    """,
    # --- 50,000 runs, one segment each ---------------------------------------
    f"""
    INSERT INTO ai_system_runs (id, tenant_id, root_task_id, ai_system_id, started_at, ended_at,
        terminal_state, finalized_by, action_count, llm_call_count, a2a_call_count)
    SELECT 'ps-run-' || r, '{TENANT}', 'ps-root-' || r, 'ps-sys-' || lpad((r % {N_SYSTEMS})::text, 3, '0'),
           now() - (r::float / {N_RUNS} * interval '{WINDOW_DAYS - 1} days') - interval '1 minute',
           now() - (r::float / {N_RUNS} * interval '{WINDOW_DAYS - 1} days'),
           CASE WHEN r % 50 = 0 THEN 'errored' ELSE 'completed' END, 'explicit', 5, 3, 1
    FROM generate_series(0, {N_RUNS - 1}) r
    """,
    f"""
    INSERT INTO ai_system_run_segments (id, run_id, tenant_id, root_task_id, root_system_id,
        caller_system_id, segment_system_id, first_action_at, last_action_at, action_count, outcome)
    SELECT 'ps-seg-' || r, 'ps-run-' || r, '{TENANT}', 'ps-root-' || r,
           'ps-sys-' || lpad((r % {N_SYSTEMS})::text, 3, '0'),
           'ps-sys-' || lpad((r % {N_SYSTEMS})::text, 3, '0'),
           'ps-sys-' || lpad((((r % {N_SYSTEMS}) + 1 + (r / {N_SYSTEMS}) % {DEPS_PER_SYSTEM}) % {N_SYSTEMS})::text, 3, '0'),
           now() - (r::float / {N_RUNS} * interval '{WINDOW_DAYS - 1} days') - interval '30 seconds',
           now() - (r::float / {N_RUNS} * interval '{WINDOW_DAYS - 1} days') - interval '10 seconds',
           2,
           CASE WHEN r % 37 = 0 THEN 'timed_out' WHEN r % 53 = 0 THEN 'failed' ELSE 'answered' END
    FROM generate_series(0, {N_RUNS - 1}) r
    """,
    # --- 200,000 proxy_logs: 4 steps per run ---------------------------------
    #   step 0: the root's LLM call; step 1: the root's MCP call;
    #   step 2: the root's A2A call to the delegate's card;
    #   step 3: the delegate's LLM call (its child group).
    f"""
    INSERT INTO proxy_logs (id, tenant_id, timestamp, proxy_type, end_user_session_id, http_method,
        endpoint, success, http_status_code, duration_ms, resource_group_id, root_task_id, server_id)
    SELECT 'ps-log-' || l, '{TENANT}',
           now() - ((l / 4)::float / {N_RUNS} * interval '{WINDOW_DAYS - 1} days') - interval '20 seconds',
           CASE l % 4 WHEN 1 THEN 'mcp' WHEN 2 THEN 'a2a' ELSE 'llm' END,
           'ps-session', 'POST', '/v1/perf',
           NOT (l % 97 = 0), CASE WHEN l % 97 = 0 THEN 502 ELSE 200 END, 120 + l % 400,
           CASE WHEN l % 4 = 3
                THEN 'ps-grp-' || lpad(((((l / 4) % {N_SYSTEMS}) + 1 + ((l / 4) / {N_SYSTEMS}) % {DEPS_PER_SYSTEM}) % {N_SYSTEMS})::text, 3, '0')
                ELSE 'ps-grp-' || lpad(((l / 4) % {N_SYSTEMS})::text, 3, '0') END,
           'ps-root-' || (l / 4),
           CASE l % 4
                WHEN 1 THEN 'ps-mcp-' || (l % 7)
                WHEN 2 THEN 'ps-card-' || lpad(((((l / 4) % {N_SYSTEMS}) + 1 + ((l / 4) / {N_SYSTEMS}) % {DEPS_PER_SYSTEM}) % {N_SYSTEMS})::text, 3, '0')
                ELSE 'ps-model-' || (l % 5) END
    FROM generate_series(0, {N_LOGS - 1}) l
    """,
    f"""
    INSERT INTO ai_system_health (id, tenant_id, ai_system_id, health_state, health_score, reasons)
    SELECT 'ps-health-' || i, '{TENANT}', 'ps-sys-' || lpad(i::text, 3, '0'),
           CASE WHEN i % 17 = 0 THEN 'degraded' ELSE 'healthy' END, 0.9, '[]'::jsonb
    FROM generate_series(0, {N_SYSTEMS - 1}) i
    """,
    "ANALYZE",
]


@pytest.fixture(scope="module")
def database_url():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    admin = make_url(pg_test_url())
    name = f"brutor_perf_{uuid.uuid4().hex[:10]}"
    server = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    with server.connect() as c:
        # From template0: the shared server's template1 can carry an older schema.
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    try:
        yield admin.set(database=name).render_as_string(hide_password=False)
    finally:
        with server.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        server.dispose()


@pytest.fixture(scope="module")
def h(database_url):
    from sqlalchemy import text

    log = BrutorLogger("composition-perf")
    harness = build_admin_harness(
        [create_ai_systems_router(log), create_ai_system_composition_router(log)],
        logger_name="composition-perf",
        database_url=database_url,
    )
    started = time.perf_counter()
    with harness.engine.begin() as c:
        for stmt in SEED_SQL:
            c.execute(text(stmt))
    with harness.engine.connect() as c:
        counts = {
            t: c.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id = :t"), {"t": TENANT}).scalar()
            for t in ("ai_system_dependencies", "agent_grants", "ai_system_runs",
                      "ai_system_run_segments", "proxy_logs")
        }
    print(f"\n[perf] seeded in {time.perf_counter() - started:.1f}s: {counts}")
    assert counts == {
        "ai_system_dependencies": N_SYSTEMS * DEPS_PER_SYSTEM,
        "agent_grants": N_SYSTEMS * DEPS_PER_SYSTEM,
        "ai_system_runs": N_RUNS,
        "ai_system_run_segments": N_RUNS,
        "proxy_logs": N_LOGS,
    }
    harness.headers(TENANT)  # log in before timing anything
    yield harness
    harness.close()


class _Sql:
    """Statements issued and the most rows any one of them returned."""

    def __init__(self, engine):
        self.engine = engine
        self.statements = 0
        self.max_rows = 0
        self.widest = ""

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self.engine, "after_cursor_execute", self._after)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self.engine, "after_cursor_execute", self._after)

    def _after(self, conn, cursor, statement, params, context, executemany):
        self.statements += 1
        if statement.lstrip().upper().startswith(("SELECT", "WITH")) and cursor.rowcount > self.max_rows:
            self.max_rows = cursor.rowcount
            self.widest = " ".join(statement.split())[:200]


def _under_coverage() -> bool:
    try:
        import coverage
    except ImportError:
        return False
    return coverage.Coverage.current() is not None


def _run(h, name: str, fn, budget_s: float, max_statements: int):
    """Time `fn` (best of three, warm) and hold it to its budgets:

      * wall time — generous, catches an order-of-magnitude regression;
      * statements — independent of the tenant's size, so a per-system
        query in a loop (an N+1 over 200 systems) fails deterministically;
      * rows — no statement may return more than MAX_ROWS_PER_STATEMENT,
        so pulling the window's history into Python (50,000 segments,
        200,000 steps) instead of aggregating it in SQL fails
        deterministically, however fast the machine.
    """
    fn()  # warm
    times, out = [], None
    with _Sql(h.engine) as sql:
        for _ in range(3):
            t0 = time.perf_counter()
            out = fn()
            times.append(time.perf_counter() - t0)
    best = min(times)
    per_call = sql.statements // 3
    print(f"[perf] {name}: best {best * 1000:.0f} ms (runs {', '.join(f'{t * 1000:.0f}' for t in times)} ms), "
          f"{per_call} statements, max {sql.max_rows} rows/statement")
    assert sql.max_rows <= MAX_ROWS_PER_STATEMENT, (
        f"{name}: a statement returned {sql.max_rows} rows — aggregate in SQL. {sql.widest}"
    )
    assert per_call <= max_statements, f"{name}: {per_call} statements (at most {max_statements})"
    # A coverage tracer slows Python several-fold, so wall time is not this
    # code's time. The statement and row budgets above still hold.
    if not _under_coverage():
        assert best < budget_s, f"{name} took {best:.2f}s (budget {budget_s}s)"
    return out


def _list(h):
    r = h.client.get(
        f"/v1/admin/ai-systems?limit=100&window_hours={WINDOW_DAYS * 24}", headers=h.headers(TENANT)
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_list_endpoint_within_budget(h):
    body = _run(h, "list (limit=100)", lambda: _list(h), LIST_BUDGET_S, LIST_MAX_STATEMENTS)
    assert body["total"] == N_SYSTEMS
    row = next(s for s in body["systems"] if s["id"] == "ps-sys-000")
    assert row["composition"]["depends_on"] == DEPS_PER_SYSTEM


def test_composition_endpoint_within_budget(h):
    def call():
        r = h.client.get(
            f"/v1/admin/ai-systems/ps-sys-000/composition?window_hours={WINDOW_DAYS * 24}",
            headers=h.headers(TENANT),
        )
        assert r.status_code == 200, r.text
        return r.json()

    body = _run(h, "composition (one system)", call, COMPOSITION_BUDGET_S, COMPOSITION_MAX_STATEMENTS)
    assert len([d for d in body["dependencies"] if d["declared"]]) == DEPS_PER_SYSTEM


def test_one_health_computation_within_budget(h):
    from app.database.models.resource_group import ResourceGroup
    from app.services.ai_system_assurance import compute_health

    s = h.Factory()
    try:
        system = s.get(ResourceGroup, "ps-sys-001")
        out = _run(h, "compute_health (one root)", lambda: compute_health(s, system),
                   HEALTH_BUDGET_S, HEALTH_MAX_STATEMENTS)
    finally:
        s.close()
    assert out["dependency_health"], "the root's dependencies were judged"

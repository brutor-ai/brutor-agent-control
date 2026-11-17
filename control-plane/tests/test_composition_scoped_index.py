# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The scoped dependency-health index answers exactly like the tenant-wide one

The health scorer computes dependency health once per system per tick.
Building the tenant-wide `CompositionIndex` for each made a tick
O(systems × tenant); `dependency_health` now builds an index scoped to the
system's dependency closure (its direct dependencies and everything
reachable from them along CRITICAL shared-service edges — what the failing
propagation walks). The bug class this guards: a scope that misses a system
the verdict reads, so a transitive failure (A → B → C, C failing) or an
outage two hops away silently reads healthy.

  * **Parity** — on a graph with transitive critical failures, a degradable
    edge whose own critical dependency is down, a caller outside the scope,
    a cycle and an external card, every system's dependency health from the
    scoped index equals the tenant-wide one, entry for entry.
  * **Scope** — the scoped index loads observations only inside the
    closure, and refuses the tenant-wide questions (callers, role,
    findings) instead of answering them from partial data.
"""

from __future__ import annotations

import itertools
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.ai_system_dependency import AISystemDependency
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.ai_system_composition import CompositionIndex
from app.services.ai_system_dependency_health import dependency_health
from tests.admin_endpoint_harness import build_admin_harness

T = "t1"
SYSTEMS = ("A", "B", "C", "D", "E", "F", "G", "H", "K")
#: (caller, target, criticality) — shared-service edges.
EDGES = [
    ("A", "B", "critical"),   # A → B → C: C fails on its own → B, then A, capped
    ("B", "C", "critical"),
    ("A", "D", "degradable"),  # D's critical dependency E is down (3 unanswered)
    ("D", "E", "critical"),
    ("H", "A", "critical"),   # H is outside A's closure: A must not read it
    ("F", "G", "critical"),   # a cycle, one unanswered call among F's latest
    ("G", "F", "critical"),
    ("K", "F", "degradable"),
]
#: (caller, target, outcomes oldest → newest)
CALLS = [
    ("A", "B", ["answered", "answered", "answered"]),
    ("B", "C", ["answered"]),
    ("A", "D", ["answered", "failed", "answered"]),
    ("D", "E", ["timed_out", "failed", "timed_out"]),
    ("H", "A", ["timed_out", "timed_out", "timed_out"]),
    ("F", "G", ["answered", "timed_out", "answered"]),
    ("G", "F", ["answered"]),
    ("K", "F", ["answered", "answered"]),
]
EXT_CARD = "card-ext"
_n = itertools.count(1)


def _seed(s):
    now = datetime.now(timezone.utc)
    for sid in SYSTEMS:
        s.add(ResourceGroup(
            id=sid, tenant_id=T, name=sid, description=sid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, system_kind="agent",
        ))
    s.add(AgentCard(id=EXT_CARD, tenant_id=T, name="External", version="1.0.0",
                    card_json={"name": "ext", "capabilities": []}))
    s.flush()
    for caller, target, crit in EDGES:
        s.add(AISystemDependency(
            id=f"dep-{caller}-{target}", tenant_id=T, ai_system_id=caller, role="shared_service",
            depends_on_system_id=target, criticality=crit,
            fallback="manual review" if crit == "degradable" else None,
        ))
    s.add(AISystemDependency(
        id="dep-A-ext", tenant_id=T, ai_system_id="A", role="external", agent_card_id=EXT_CARD,
        supplier_legal_entity="Supplier Ltd", assumptions={"retention_at_delegate": "30 days"},
    ))
    # C fails on its own evidence (enforced reliability below the threshold).
    s.add(AISystemHealth(id="hlt-C", tenant_id=T, ai_system_id="C", health_state="degraded",
                         reliability_score=0.3))
    s.flush()
    for caller, target, outcomes in CALLS:
        for i, outcome in enumerate(outcomes):
            n = next(_n)
            at = now - timedelta(hours=10 - i)
            s.add(AISystemRun(
                id=f"run-{n}", tenant_id=T, root_task_id=f"root-{n}", ai_system_id=caller,
                started_at=at, ended_at=at, terminal_state="completed", finalized_by="explicit",
                is_replay=False, action_count=2,
            ))
            s.flush()
            s.add(AISystemRunSegment(
                id=f"seg-{n:03d}", run_id=f"run-{n}", tenant_id=T, root_task_id=f"root-{n}",
                root_system_id=caller, caller_system_id=caller, segment_system_id=target,
                first_action_at=at, last_action_at=at + timedelta(seconds=5),
                action_count=1, outcome=outcome,
            ))
    for i, status in enumerate((200, 502, 200)):
        s.add(ProxyLog(
            id=f"log-ext-{i}", tenant_id=T, timestamp=now - timedelta(minutes=30 + i),
            proxy_type="a2a", end_user_session_id="s", http_method="POST", endpoint="/a2a",
            success=status < 400, http_status_code=status, duration_ms=100 * (i + 1),
            resource_group_id="A", server_id=EXT_CARD,
        ))
        # The same card called by another system: never A's boundary.
        s.add(ProxyLog(
            id=f"log-ext-h-{i}", tenant_id=T, timestamp=now - timedelta(minutes=30 + i),
            proxy_type="a2a", end_user_session_id="s", http_method="POST", endpoint="/a2a",
            success=False, http_status_code=500, duration_ms=5000,
            resource_group_id="H", server_id=EXT_CARD,
        ))


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness([create_ai_systems_router(BrutorLogger("scoped"))],
                                  logger_name="scoped-index")
    harness.seed(_seed)
    yield harness
    harness.close()


@pytest.fixture
def db(h):
    s = h.Factory()
    yield s
    s.close()


@pytest.mark.parametrize("sid", SYSTEMS)
def test_scoped_dependency_health_equals_the_tenant_wide_one(db, sid):
    system = db.get(ResourceGroup, sid)
    full = CompositionIndex(db, T)
    assert dependency_health(db, system) == dependency_health(db, system, index=full)


def test_the_fixture_exercises_what_the_scope_must_reach(db):
    """Guard the guard: the parity above is only meaningful if the graph
    has a transitive failure, a two-hop outage and a boundary to read."""
    by = {d["system_id"] or d["edge_id"]: d for d in dependency_health(db, db.get(ResourceGroup, "A"))}
    assert (by["B"]["status"], by["B"]["failing_signal"], by["B"]["via_system_id"]) == (
        "degraded", "dependency", "C")
    assert (by["D"]["status"], by["D"]["failing_signal"]) == ("degraded", "reliability")
    assert by["dep-A-ext"]["boundary"]["calls"] == 3
    assert by["dep-A-ext"]["boundary"]["errors"] == 1
    d_entries = {d["system_id"]: d for d in dependency_health(db, db.get(ResourceGroup, "D"))}
    assert (d_entries["E"]["status"], d_entries["E"]["failing_signal"]) == ("degraded", "liveness")
    k = {d["system_id"]: d for d in dependency_health(db, db.get(ResourceGroup, "K"))}
    # F's critical dependency G is on a cycle with it; F's own call to G
    # had one unanswered among its latest → F is not healthy, K reads it.
    assert k["F"]["status"] == "degraded"


def test_the_scope_is_the_critical_closure(db):
    index = CompositionIndex(db, T, scope_root="A")
    # A, its direct dependencies (B, D, the external card has no system),
    # and the critical closure from them (C via B, E via D). Not H (calls
    # A), not F/G/K (unrelated).
    assert index.scope == {"A", "B", "C", "D", "E"}
    assert {caller for caller, _ in index.pairs} <= index.scope
    assert {caller for caller, _ in index.a2a_calls} == {"A"}


@pytest.mark.parametrize("question", [
    lambda i: i.observed_callers("A"),
    lambda i: i.refused_callers("A"),
    lambda i: i.role("A"),
    lambda i: i.summary("A"),
    lambda i: i.composition_finding_count("A"),
    lambda i: i.a2a_calls_from("A"),
    lambda i: i.observed_targets("H"),
])
def test_a_scoped_index_refuses_tenant_wide_questions(db, question):
    with pytest.raises(RuntimeError, match="tenant-wide"):
        question(CompositionIndex(db, T, scope_root="A"))

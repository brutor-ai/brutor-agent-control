# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# One liveness verdict (one fact, one computation)

The fraud screener in the Brutor Demo System read "silent" on the health
card and "not silent" on the liveness panel: the scorer called any system
with no OWN runs silent, the panel applied the detector's rule, and a shared
service (called only inside its callers' runs) has no runs of its own.

Two guards, both class-level:

  * **Static**: no module in `app/` other than the liveness function may
    compute liveness — read an expectation's silence rule
    (`alerts_on_silence`, `deadline_seconds`, `SILENCE_ALERTING_MODES`), or
    pass `is_silent=` / a `"liveness"` score that is not read off the
    function's result. And the three surfaces must call it.
  * **Parity**: for every mode × state the panel, the list and the scorer
    report the same verdict. idle / unstarted / not_configured are never
    silent.
"""

from __future__ import annotations

import ast
import os
import pathlib

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.ai_system_liveness import create_ai_system_liveness_router
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
from app.core.logging_system import BrutorLogger
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.ai_system_liveness_state import liveness_state
from tests.admin_endpoint_harness import build_admin_harness
from tests.source_trees import python_sources

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
LIVENESS_MODULE = APP / "services" / "ai_system_liveness_state.py"
#: The expectation model defines the rule's inputs; the Pydantic response
#: model declares the same names as fields. Neither computes a verdict.
ALLOWED = {
    LIVENESS_MODULE,
    APP / "database" / "models" / "ai_system_liveness.py",
}
RULE_ATTRS = {"alerts_on_silence", "deadline_seconds"}
RULE_NAMES = {"SILENCE_ALERTING_MODES"}

#: The surfaces that report liveness, and the function each must call.
SURFACES = {
    APP / "api" / "v1" / "endpoints" / "admin" / "ai_system_liveness.py": "liveness_state",
    APP / "services" / "ai_system_assurance.py": "liveness_state",
    APP / "api" / "v1" / "endpoints" / "admin" / "ai_systems.py": "liveness_states",
}


def _violations(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text(), str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in RULE_ATTRS and isinstance(
            node.ctx, ast.Load
        ):
            out.append(f"{path.name}:{node.lineno} reads .{node.attr}")
        if isinstance(node, ast.Name) and node.id in RULE_NAMES:
            out.append(f"{path.name}:{node.lineno} uses {node.id}")
        if isinstance(node, ast.keyword) and node.arg == "is_silent":
            if not _reads_result(node.value, "is_silent"):
                out.append(f"{path.name}:{node.lineno} computes is_silent itself")
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "liveness"
                    and isinstance(v, (ast.IfExp, ast.Compare, ast.BoolOp, ast.BinOp))
                ):
                    out.append(f"{path.name}:{node.lineno} computes a liveness score itself")
    return out


def _reads_result(expr: ast.AST, attr: str) -> bool:
    """An `is_silent=` value must be the function's `.is_silent` (optionally
    gated with `and`), or a literal False."""
    if isinstance(expr, ast.Constant) and expr.value is False:
        return True
    if isinstance(expr, ast.Attribute) and expr.attr == attr:
        return True
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        return any(isinstance(v, ast.Attribute) and v.attr == attr for v in expr.values) and all(
            isinstance(v, (ast.Name, ast.Attribute)) for v in expr.values
        )
    return False


class TestStaticGuard:
    def test_no_surface_computes_liveness_itself(self):
        found = []
        for path in python_sources(APP):
            if path in ALLOWED:
                continue
            found.extend(_violations(path))
        assert not found, (
            "Liveness is computed in one place (app/services/ai_system_liveness_state.py). "
            "Read its result instead:\n  " + "\n  ".join(found)
        )

    @pytest.mark.parametrize("path,fn", sorted(SURFACES.items()))
    def test_every_surface_calls_the_function(self, path, fn):
        tree = ast.parse(path.read_text())
        calls = {
            n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert fn in calls, f"{path.name} reports liveness but never calls {fn}()"

    def test_the_guard_catches_the_old_scorer_rule(self, tmp_path):
        """The guard must fail on the exact shape it replaced."""
        bad = tmp_path / "bad.py"
        bad.write_text(
            "def f(total, exp, d):\n"
            "    s = {'liveness': 1.0 if total > 0 else 0.0}\n"
            "    g(is_silent=(total == 0) or exp.alerts_on_silence)\n"
        )
        v = _violations(bad)
        assert any("liveness score" in x for x in v)
        assert any("is_silent" in x for x in v)
        assert any(".alerts_on_silence" in x for x in v)


# ---------------------------------------------------------------------------
# Parity: panel vs list vs scorer, every mode × state
# ---------------------------------------------------------------------------

TENANT = "t1"

#: (system id, mode | None (no stored expectation), enabled, own-run ages
#: (hours), segment (age hours, outcome) list, expected state)
CASES = [
    ("lv-cont-alive", "continuous", True, [0.02], [], "alive"),
    ("lv-cont-silent", "continuous", True, [2.0], [], "silent"),
    ("lv-cont-unstarted", "continuous", True, [], [], "unstarted"),
    ("lv-cont-service-alive", "continuous", True, [], [(0.02, "answered")], "alive"),
    ("lv-sched-alive", "scheduled", True, [0.5], [], "alive"),
    ("lv-sched-silent", "scheduled", True, [5.0], [], "silent"),
    ("lv-sched-unstarted", "scheduled", True, [], [], "unstarted"),
    ("lv-od-alive", "on_demand", True, [],
     [(5.0, "timed_out"), (3.0, "answered"), (2.0, "answered"), (1.0, "answered")], "alive"),
    # THE down rule (ai_system_down_rule): silent = the latest
    # 3 inbound calls ALL unanswered; one unanswered among them = alive but
    # degraded (score 0.5).
    ("lv-od-silent", "on_demand", True, [],
     [(4.0, "answered"), (3.0, "timed_out"), (2.0, "failed"), (1.0, "timed_out")], "silent"),
    ("lv-od-degraded", "on_demand", True, [], [(3.0, "answered"), (1.0, "timed_out")], "alive"),
    ("lv-od-one-failed", "on_demand", True, [], [(1.0, "failed")], "alive"),
    ("lv-od-two-of-three", "on_demand", True, [],
     [(3.0, "failed"), (2.0, "answered"), (1.0, "timed_out")], "alive"),
    ("lv-od-rejected-is-an-answer", "on_demand", True, [], [(1.0, "rejected")], "alive"),
    ("lv-od-idle", "on_demand", True, [], [], "idle"),
    ("lv-od-own-runs", "on_demand", True, [4.0], [], "alive"),
    ("lv-spor-alive", "sporadic", True, [10.0], [], "alive"),
    ("lv-spor-idle", "sporadic", True, [900.0], [], "idle"),
    ("lv-spor-unstarted", "sporadic", True, [], [], "unstarted"),
    ("lv-noexp-ran", None, True, [0.1], [], "not_configured"),
    ("lv-noexp-quiet", None, True, [900.0], [], "not_configured"),
    ("lv-noexp-never", None, True, [], [], "unstarted"),
    ("lv-disabled", "continuous", False, [5.0], [], "not_configured"),
]
CALLER = "lv-caller"


def _seed(s):
    # Taken at SEED time, not import time: the full suite collects this module
    # an hour before it runs it, and a run "30 minutes ago" at import is 90
    # minutes old by then — past a 65-minute deadline.
    now = datetime.now(timezone.utc)
    s.add(ResourceGroup(
        id=CALLER, tenant_id=TENANT, name=CALLER, description="c", hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM, system_kind="agent",
    ))
    for sid, *_ in CASES:
        s.add(ResourceGroup(
            id=sid, tenant_id=TENANT, name=sid, description=sid, hierarchy_level=0,
            inherit_resources=True, is_active=True, is_system=False,
            group_type=GroupType.AI_SYSTEM, system_kind="agent",
        ))
    s.flush()
    n = 0
    for sid, mode, enabled, run_ages, segments, _ in CASES:
        if mode is not None:
            s.add(AISystemLivenessExpectation(
                id=f"live-{sid}", tenant_id=TENANT, ai_system_id=sid, mode=mode,
                expected_min_runs=1, window_seconds=3600, grace_seconds=300, enabled=enabled,
            ))
        for age in run_ages:
            n += 1
            s.add(AISystemRun(
                id=f"run-{n}", tenant_id=TENANT, root_task_id=f"root-{n}", ai_system_id=sid,
                started_at=now - timedelta(hours=age), ended_at=now - timedelta(hours=age),
                terminal_state="completed", finalized_by="explicit", is_replay=False,
                action_count=1, llm_call_count=1,
            ))
        for age, outcome in segments:
            n += 1
            s.add(AISystemRun(
                id=f"run-{n}", tenant_id=TENANT, root_task_id=f"root-{n}", ai_system_id=CALLER,
                started_at=now - timedelta(hours=age), ended_at=now - timedelta(hours=age),
                terminal_state="completed", finalized_by="explicit", is_replay=False,
                action_count=2, llm_call_count=2,
            ))
            s.flush()
            s.add(AISystemRunSegment(
                id=f"seg-{n}", run_id=f"run-{n}", tenant_id=TENANT, root_task_id=f"root-{n}",
                root_system_id=CALLER, caller_system_id=CALLER, segment_system_id=sid,
                first_action_at=now - timedelta(hours=age),
                last_action_at=now - timedelta(hours=age),
                action_count=1, outcome=outcome,
            ))


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("liveness-parity")
    harness = build_admin_harness(
        [create_ai_system_liveness_router(log), create_ai_systems_router(log)],
        logger_name="liveness-parity",
    )
    harness.seed(_seed)
    yield harness
    harness.close()


@pytest.fixture(scope="module")
def listing(h):
    r = h.client.get("/v1/admin/ai-systems?limit=500", headers=h.headers(TENANT))
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["systems"]}


@pytest.mark.parametrize("sid,mode,enabled,runs,segments,expected", CASES, ids=[c[0] for c in CASES])
def test_panel_list_and_scorer_agree(h, listing, sid, mode, enabled, runs, segments, expected):
    from app.services.ai_system_assurance import compute_health

    panel = h.client.get(f"/v1/admin/ai-systems/{sid}/liveness", headers=h.headers(TENANT))
    assert panel.status_code == 200, panel.text
    body = panel.json()

    s = h.Factory()
    try:
        system = s.query(ResourceGroup).filter_by(id=sid).one()
        verdict = liveness_state(s, system)
        scored = compute_health(s, system)
    finally:
        s.close()

    assert verdict.state == expected
    assert body["state"] == expected, body["reason"]
    assert body["is_silent"] is (expected == "silent")
    assert listing[sid]["liveness"]["state"] == expected
    # The scorer reads the SAME verdict: its liveness component is the
    # function's score, and it is silent exactly when the function says so.
    assert scored["liveness_score"] == verdict.score
    assert (scored["health_state"] == "silent") is (expected == "silent")


@pytest.mark.parametrize("state", ["idle", "unstarted", "not_configured"])
def test_quiet_states_never_score_silent(h, state):
    from app.services.ai_system_assurance import compute_health

    ids = [c[0] for c in CASES if c[5] == state]
    assert ids, f"no parity case covers {state}"
    s = h.Factory()
    try:
        for sid in ids:
            scored = compute_health(s, s.query(ResourceGroup).filter_by(id=sid).one())
            assert scored["health_state"] != "silent", sid
            assert scored["liveness_score"] != 0.0, sid
    finally:
        s.close()


def test_every_mode_and_state_is_covered():
    covered = {(c[1], c[5]) for c in CASES}
    for need in [
        ("continuous", "alive"), ("continuous", "silent"), ("continuous", "unstarted"),
        ("scheduled", "alive"), ("scheduled", "silent"), ("scheduled", "unstarted"),
        ("on_demand", "alive"), ("on_demand", "silent"), ("on_demand", "idle"),
        ("sporadic", "alive"), ("sporadic", "idle"), ("sporadic", "unstarted"),
        (None, "not_configured"), (None, "unstarted"), ("continuous", "not_configured"),
    ]:
        assert need in covered, need


#: sid → the liveness score THE down rule gives it (1.0 ok, 0.5 degraded, 0.0 down).
ON_DEMAND_SCORES = {
    "lv-od-alive": 1.0,
    "lv-od-silent": 0.0,
    "lv-od-degraded": 0.5,
    "lv-od-one-failed": 0.5,
    "lv-od-two-of-three": 0.5,
    "lv-od-rejected-is-an-answer": 1.0,
}


@pytest.mark.parametrize("sid,score", sorted(ON_DEMAND_SCORES.items()))
def test_on_demand_follows_the_one_down_rule(h, sid, score):
    """One unanswered call among the latest degrades (the scorer reads
    degraded, never silent); three in a row is silent — the same rule the
    dependency node and the connection line apply."""
    from app.services.ai_system_assurance import compute_health

    s = h.Factory()
    try:
        system = s.query(ResourceGroup).filter_by(id=sid).one()
        verdict = liveness_state(s, system)
        scored = compute_health(s, system)
    finally:
        s.close()
    assert verdict.score == score, verdict.reason
    if score == 0.5:
        assert verdict.state == "alive" and "degraded" in verdict.reason
        # Below the scorer's degraded threshold (0.8), never silent.
        assert scored["liveness_score"] == 0.5
        assert scored["health_state"] != "silent", scored["reasons"]
    if score == 0.0:
        assert scored["health_state"] == "silent"


def test_a_service_with_only_segments_is_not_silent(h):
    """The fraud-screener shape: on_demand, zero own runs, answering its
    caller — alive everywhere, never silent."""
    s = h.Factory()
    try:
        system = s.query(ResourceGroup).filter_by(id="lv-od-alive").one()
        assert s.query(AISystemRun).filter_by(ai_system_id=system.id).count() == 0
        assert liveness_state(s, system).state == "alive"
    finally:
        s.close()

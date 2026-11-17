# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# An unapproved release is judged by when it ran

A release is judged against the declaration in force when it ran, not the
CURRENT one: replacing an agent's approved_versions ['0.1.0'] with ['0.2.0']
must not fire `agent.unapproved_release` on 0.1.0, a release that was
approved the whole time it ran.

Covered here, class-level:

  * the lookup: half-open periods, the boundary instant, empty = observe
    only, wildcards, a run before every period, no history at all;
  * the check over real rows: the demo case both ways (0.1.0 is not a
    finding; 0.2.0 before its approval IS one), the boundary run, the
    finding clearing once its unapproved runs leave the 7-day window;
  * the one writer: every change opens a period, a re-sent value does not,
    create records the first period, a first change of an identity with no
    history records the value it held until then;
  * a static guard: nothing in `app/` but the writer assigns
    `approved_versions`;
  * the a193_approval_window_epochs backfill from contract closures and the live declaration.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from app.api.v1.endpoints.admin.agent_identities import create_agent_identities_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_identity import AgentIdentity, ResourceGroupAgentMember
from app.database.models.agent_identity_approval_history import AgentIdentityApprovalHistory
from app.database.models.agent_implementation_release import AgentImplementationRelease
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.agent_implementation import approved_versions_at
from tests.admin_endpoint_harness import build_admin_harness
from tests.source_trees import python_sources, source_label

T1 = "t1"
_n = count(1)
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def h():
    harness = build_admin_harness(
        [create_agent_identities_router(BrutorLogger("aah-tests"))], logger_name="aah-tests"
    )
    yield harness
    harness.close()


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
DEMO = [
    (T0 - timedelta(days=6), T0, ["0.1.0"]),   # approved until setup ran
    (T0, None, ["0.2.0"]),                     # setup at T0 (14:17 live)
]


class TestLookup:
    @pytest.mark.parametrize("at, expected", [
        (T0 - timedelta(days=1), ["0.1.0"]),
        (T0 - timedelta(microseconds=1), ["0.1.0"]),
        (T0, ["0.2.0"]),  # half-open: the instant a declaration takes effect is its own
        (T0 + timedelta(days=1), ["0.2.0"]),
    ])
    def test_half_open_periods(self, at, expected):
        assert approved_versions_at(DEMO, at, ["live-ignored"]) == expected

    def test_a_run_before_every_period_is_judged_by_the_earliest(self):
        assert approved_versions_at(DEMO, T0 - timedelta(days=30), ["0.2.0"]) == ["0.1.0"]

    def test_no_history_falls_back_to_the_live_declaration(self):
        assert approved_versions_at(None, T0, ["1.*"]) == ["1.*"]
        assert approved_versions_at([], T0, None) == []

    def test_a_zero_length_period_never_answers(self):
        tl = [(T0 - timedelta(hours=1), T0, ["a"]), (T0, T0, ["b"]), (T0, None, ["c"])]
        assert approved_versions_at(tl, T0, None) == ["c"]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def system(h, *, tier="high"):
    sid = f"aah-sys-{next(_n)}"
    h.seed(lambda s: s.add(ResourceGroup(
        id=sid, tenant_id=T1, name=sid, description=sid, hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM, system_kind="agent", owner="team",
        intended_use="x", lifecycle_stage="active", eu_ai_act_risk_tier=tier,
    )))
    return sid


def agent(h, sid, *, live, periods=()):
    """An identity with an explicit approval history `periods` =
    [(valid_from, valid_to, versions)]."""
    aid = f"aah-agent-{next(_n)}"

    def fn(s):
        s.add(AgentIdentity(
            id=aid, tenant_id=T1, name=aid, enabled=True, status="active",
            default_deny=True, enforcement_mode="enforce", approved_versions=list(live),
        ))
        s.flush()
        s.add(ResourceGroupAgentMember(tenant_id=T1, agent_id=aid, group_id=sid,
                                       role="component",
                                       joined_at=_now() - timedelta(days=30)))
        for start, end, versions in periods:
            s.add(AgentIdentityApprovalHistory(
                id=f"aah-{next(_n)}", tenant_id=T1, agent_id=aid, approved_versions=versions,
                valid_from=start, valid_to=end, source="declaration",
            ))
    h.seed(fn)
    return aid


def release(h, aid, version, name="demo"):
    h.seed(lambda s: s.add(AgentImplementationRelease(
        id=f"rel-{next(_n):06d}", tenant_id=T1, agent_id=aid, name=name, version=version,
        build="", trust="declared", first_seen_at=_now() - timedelta(days=3),
        last_seen_at=_now(), run_count=1, fingerprints=["sha256:" + "a" * 64],
    )))


def run(h, sid, aid, version, at, name="demo"):
    rid = f"run-aah-{next(_n):06d}"
    h.seed(lambda s: s.add(AISystemRun(
        id=rid, tenant_id=T1, root_task_id=f"root-{rid}", ai_system_id=sid,
        actor_type="agent_identity", actor_id=aid, agent_id=aid, started_at=at, ended_at=at,
        terminal_state="completed", finalized_by="explicit", is_replay=False,
        action_count=2, llm_call_count=1, chain_integrity="intact",
        agent_impl_name=name, agent_impl_version=version,
    )))


def unapproved(h, aid, *, now=None):
    from app.services.assurance_checks.agent import (
        FINDING_UNAPPROVED_RELEASE,
        agent_findings,
        load_agent_facts,
    )

    s = h.Factory()
    try:
        facts = load_agent_facts(s, T1, now=now)
        return {
            f["release"]["version"]: f
            for f in agent_findings(s, facts)
            if f["agent_id"] == aid and f["code"] == FINDING_UNAPPROVED_RELEASE
        }
    finally:
        s.close()


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


class TestJudgedByWhenItRan:
    def _demo(self, h):
        """The live case: 0.1.0 approved until setup approved 0.2.0 at `cut`;
        0.2.0 started running shortly before that."""
        cut = _now() - timedelta(hours=2)
        sid = system(h)
        aid = agent(h, sid, live=["0.2.0"], periods=[
            (cut - timedelta(days=5), cut, ["0.1.0"]),
            (cut, None, ["0.2.0"]),
        ])
        release(h, aid, "0.1.0")
        release(h, aid, "0.2.0")
        return sid, aid, cut

    def test_a_release_approved_the_whole_time_it_ran_is_not_a_finding(self, h):
        sid, aid, cut = self._demo(h)
        for hours in (48, 24, 3):
            run(h, sid, aid, "0.1.0", cut - timedelta(hours=hours))
        run(h, sid, aid, "0.2.0", cut + timedelta(minutes=5))
        assert unapproved(h, aid) == {}

    def test_a_release_that_ran_before_its_approval_is_a_finding(self, h):
        sid, aid, cut = self._demo(h)
        run(h, sid, aid, "0.1.0", cut - timedelta(hours=3))
        first = cut - timedelta(minutes=20)
        run(h, sid, aid, "0.2.0", first)
        run(h, sid, aid, "0.2.0", cut - timedelta(minutes=10))
        run(h, sid, aid, "0.2.0", cut + timedelta(minutes=10))  # approved by then
        found = unapproved(h, aid)
        assert set(found) == {"0.2.0"}
        ev = found["0.2.0"]["evidence"]
        assert ev["unapproved_runs"] == 2
        assert ev["approved_versions_when_run"] == ["0.1.0"]
        assert datetime.fromisoformat(ev["first_unapproved_run_at"]) == first
        assert found["0.2.0"]["systems"][sid] == "high"

    def test_the_boundary_instant_belongs_to_the_new_declaration(self, h):
        sid, aid, cut = self._demo(h)
        run(h, sid, aid, "0.2.0", cut)   # approved at that instant
        run(h, sid, aid, "0.1.0", cut)   # no longer approved at that instant
        assert set(unapproved(h, aid)) == {"0.1.0"}

    def test_a_release_still_run_after_it_was_withdrawn_is_a_finding(self, h):
        sid, aid, cut = self._demo(h)
        run(h, sid, aid, "0.1.0", cut + timedelta(minutes=30))
        assert set(unapproved(h, aid)) == {"0.1.0"}

    def test_it_clears_once_its_unapproved_runs_leave_the_window(self, h):
        from app.services.assurance_checks.agent import UNAPPROVED_RELEASE_WINDOW_DAYS

        sid, aid, cut = self._demo(h)
        run(h, sid, aid, "0.2.0", cut - timedelta(minutes=20))
        run(h, sid, aid, "0.2.0", cut + timedelta(minutes=20))
        assert set(unapproved(h, aid)) == {"0.2.0"}
        later = cut + timedelta(days=UNAPPROVED_RELEASE_WINDOW_DAYS, minutes=1)
        assert unapproved(h, aid, now=later) == {}

    def test_an_empty_declaration_constrains_nothing(self, h):
        sid = system(h)
        t = _now() - timedelta(hours=1)
        aid = agent(h, sid, live=["2.*"], periods=[
            (t - timedelta(days=2), t, []),
            (t, None, ["2.*"]),
        ])
        release(h, aid, "1.0.0")
        run(h, sid, aid, "1.0.0", t - timedelta(minutes=5))  # observe only then
        assert unapproved(h, aid) == {}
        run(h, sid, aid, "1.0.0", t + timedelta(minutes=5))
        assert set(unapproved(h, aid)) == {"1.0.0"}

    def test_wildcards_are_judged_per_period(self, h):
        sid = system(h)
        t = _now() - timedelta(hours=1)
        aid = agent(h, sid, live=["2.*"], periods=[
            (t - timedelta(days=2), t, ["1.*"]),
            (t, None, ["2.*"]),
        ])
        for v in ("1.4.2", "2.0.1"):
            release(h, aid, v)
        run(h, sid, aid, "1.4.2", t - timedelta(minutes=5))  # 1.* in effect
        run(h, sid, aid, "2.0.1", t + timedelta(minutes=5))  # 2.* in effect
        assert unapproved(h, aid) == {}
        run(h, sid, aid, "2.0.1", t - timedelta(minutes=4))  # before 2.* was approved
        assert set(unapproved(h, aid)) == {"2.0.1"}


# ---------------------------------------------------------------------------
# The one writer
# ---------------------------------------------------------------------------


def _history(h, aid):
    s = h.Factory()
    try:
        rows = (
            s.query(AgentIdentityApprovalHistory)
            .filter_by(agent_id=aid)
            .order_by(AgentIdentityApprovalHistory.valid_from, AgentIdentityApprovalHistory.id)
            .all()
        )
        s.expunge_all()
        return rows
    finally:
        s.close()


class TestWriter:
    def _create(self, h, **body):
        r = h.client.post("/v1/admin/agent-identities",
                          json={"name": f"w-{next(_n)}", **body}, headers=h.headers(T1))
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def _patch(self, h, aid, **body):
        r = h.client.patch(f"/v1/admin/agent-identities/{aid}", json=body, headers=h.headers(T1))
        assert r.status_code == 200, r.text

    def test_create_records_the_first_period(self, h):
        aid = self._create(h, approved_versions=["1.0.0"])
        (row,) = _history(h, aid)
        assert (row.approved_versions, row.valid_to, row.source) == (["1.0.0"], None, "declaration")
        assert row.changed_by

    def test_each_change_closes_the_current_period_and_opens_one(self, h):
        aid = self._create(h, approved_versions=["0.1.0"])
        self._patch(h, aid, approved_versions=["0.2.0"])
        self._patch(h, aid, approved_versions=["0.2.0"])        # re-sent: no new period
        self._patch(h, aid, description="unrelated edit")       # not a declaration change
        rows = _history(h, aid)
        assert [r.approved_versions for r in rows] == [["0.1.0"], ["0.2.0"]]
        assert rows[0].valid_to is not None and rows[1].valid_to is None
        assert rows[0].valid_to == rows[1].valid_from

    def test_an_invalid_declaration_writes_no_period(self, h):
        aid = self._create(h, approved_versions=["1.0.0"])
        r = h.client.patch(f"/v1/admin/agent-identities/{aid}",
                           json={"approved_versions": ["1.*.0"]}, headers=h.headers(T1))
        assert r.status_code == 422
        assert len(_history(h, aid)) == 1

    def test_a_first_change_without_history_keeps_the_value_it_replaces(self, h):
        # An identity no declaration ever touched (seeded, or made by the
        # policy engine) has held its value since it was created.
        sid = system(h)
        aid = agent(h, sid, live=["0.1.0"])
        self._patch(h, aid, approved_versions=["0.2.0"])
        rows = _history(h, aid)
        assert [(r.approved_versions, r.source) for r in rows] == [
            (["0.1.0"], "backfill_live"), (["0.2.0"], "declaration"),
        ]
        assert rows[0].valid_to == rows[1].valid_from


def test_only_the_writer_assigns_approved_versions():
    """Class guard: a value set anywhere but `record_approved_versions` has
    no period, so every earlier run would be judged by it. Attribute
    assignment, `setattr` and an `AgentIdentity(approved_versions=…)`
    construction are all refused outside the writer."""
    allowed = {"services/agent_implementation.py"}
    offenders = []
    for path in python_sources(APP):
        rel = source_label(path)
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "approved_versions":
                    offenders.append(f"{rel}:{node.lineno} assigns .approved_versions")
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name == "setattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                        and node.args[1].value == "approved_versions":
                    offenders.append(f"{rel}:{node.lineno} setattr approved_versions")
                if name == "AgentIdentity" and any(k.arg == "approved_versions" for k in node.keywords):
                    offenders.append(f"{rel}:{node.lineno} AgentIdentity(approved_versions=…)")
    assert offenders == []


# ---------------------------------------------------------------------------
# The a193_approval_window_epochs backfill
# ---------------------------------------------------------------------------

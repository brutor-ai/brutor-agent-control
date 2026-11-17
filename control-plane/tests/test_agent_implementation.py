# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Agent implementation identity — control plane behavioural spec

Real app, real auth, full-schema SQLite (the shared admin harness). One class
per rule, each guarding the CLASS of bug:

  * Declaration — implementation_name / approved_versions /
    require_release round-trip; every invalid version pattern is refused;
    approval is DERIVED at read time (approving a version changes every
    release's `approved` with no release row written).
  * Releases — the identity's timeline, most recently seen first,
    with the current release, trust and fingerprints count; the Composition
    view carries each member agent's CURRENT release.
  * Contract — the declaration is in the closure only when declared
    (a system that declares nothing keeps its canonical JSON), sorted, and
    changing it drifts the contract; the diff classifies each direction.
  * Client labels — a system's member agent is never its client, but
    its runs still count as observed traffic.
  * Checks — each code fires exactly once for its condition and clears
    as specified; undeclared_change needs the minimum run count and stays
    cleared once a human acknowledges it.
  * Lifecycle — one agent.new_release per release per member system, once.
  * Responses — an unapproved release on a high-risk system moves
    it to approval_required through a `drift_class: agent` policy; it is not
    the system's own drift.
  * Report — the composition section lists each member agent's current
    release, trust and approval, and validates against the 1.7 schema.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

import re
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest

from app.api.v1.endpoints.admin.agent_identities import create_agent_identities_router
from app.api.v1.endpoints.admin.ai_system_composition import (
    create_ai_system_composition_router,
)
from app.api.v1.endpoints.admin.ai_system_contracts import create_ai_system_contracts_router
from app.api.v1.endpoints.admin.ai_systems import create_ai_systems_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_identity import AgentIdentity, ResourceGroupAgentMember
from app.database.models.agent_implementation_release import AgentImplementationRelease
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_lifecycle_event import AISystemLifecycleEvent
from app.database.models.ai_system_response_policy import AISystemResponsePolicy
from app.database.models.ai_system_run import AISystemRun
from app.database.models.assurance_inbox import AssuranceInboxItem
from app.database.models.resource_group import GroupType, ResourceGroup
from tests.admin_endpoint_harness import build_admin_harness
from tests.source_trees import python_sources, source_label

T1 = "t1"
_n = count(1)


def _now():
    """Seed-time now — never import time."""
    return datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def h():
    log = BrutorLogger("agent-implementation-tests")
    harness = build_admin_harness(
        [
            create_agent_identities_router(log),
            create_ai_system_composition_router(log),
            create_ai_system_contracts_router(log),
            create_ai_systems_router(log),
        ],
        logger_name="agent-implementation-tests",
    )
    yield harness
    harness.close()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def system(h, gid, *, tier=None, clients=None):
    h.seed(lambda s: s.add(ResourceGroup(
        id=gid, tenant_id=T1, name=gid, description=gid, hierarchy_level=0,
        inherit_resources=True, is_active=True, is_system=False,
        group_type=GroupType.AI_SYSTEM, system_kind="agent", owner="team",
        intended_use="x", lifecycle_stage="active", eu_ai_act_risk_tier=tier,
        intended_client_labels=clients,
    )))
    return gid


def agent(h, aid, *, member_of=(), name=None, approved=(), impl=None, require=False,
          joined_minutes_ago=24 * 60 * 30):
    def fn(s):
        s.add(AgentIdentity(
            id=aid, tenant_id=T1, name=name or aid, enabled=True, status="active",
            default_deny=True, enforcement_mode="enforce", implementation_name=impl,
            approved_versions=sorted(approved), require_release=require,
        ))
        s.flush()
        for sid in member_of:
            s.add(ResourceGroupAgentMember(
                tenant_id=T1, agent_id=aid, group_id=sid, role="component",
                joined_at=_now() - timedelta(minutes=joined_minutes_ago),
            ))
    h.seed(fn)
    return aid


def release(h, aid, name, version, *, build="", trust="declared", runs=1,
            first_days_ago=1.0, last_days_ago=0.0, fingerprints=("sha256:" + "a" * 64,)):
    rid = f"rel-{next(_n):06d}"
    h.seed(lambda s: s.add(AgentImplementationRelease(
        id=rid, tenant_id=T1, agent_id=aid, name=name, version=version, build=build,
        trust=trust, first_seen_at=_now() - timedelta(days=first_days_ago),
        last_seen_at=_now() - timedelta(days=last_days_ago), run_count=runs,
        fingerprints=list(fingerprints),
    )))
    return rid


def run(h, sid, *, aid=None, name=None, version=None, build=None, trust=None, fp=None,
        client=None, minutes_ago=60.0):
    rid = f"run-{next(_n):06d}"
    at = _now() - timedelta(minutes=minutes_ago)
    h.seed(lambda s: s.add(AISystemRun(
        id=rid, tenant_id=T1, root_task_id=f"root-{rid}", ai_system_id=sid,
        actor_type="agent_identity" if aid else "api_key", actor_id=aid or "key-1",
        agent_id=aid, started_at=at, ended_at=at, terminal_state="completed",
        finalized_by="explicit", is_replay=False, action_count=2, llm_call_count=1,
        chain_integrity="intact", client_label=client,
        agent_impl_name=name, agent_impl_version=version, agent_impl_build=build,
        agent_impl_trust=trust, implementation_fingerprint=fp,
    )))
    return rid


def session(h):
    return h.Factory()


def patch_agent(h, aid, **body):
    r = h.client.patch(f"/v1/admin/agent-identities/{aid}", json=body, headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def releases(h, aid):
    r = h.client.get(f"/v1/admin/agent-identities/{aid}/releases", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def composition(h, sid):
    r = h.client.get(f"/v1/admin/ai-systems/{sid}/composition", headers=h.headers(T1))
    assert r.status_code == 200, r.text
    return r.json()


def checks(h, *, now=None):
    from app.services.assurance_checks.agent import run_agent_checks

    s = session(h)
    try:
        stats = run_agent_checks(s, T1, now=now)
        s.commit()
        return stats
    finally:
        s.close()


def items(h, aid, code=None):
    s = session(h)
    try:
        q = s.query(AssuranceInboxItem).filter(
            AssuranceInboxItem.tenant_id == T1,
            AssuranceInboxItem.category == "agent",
            AssuranceInboxItem.fingerprint.like(f"agent:{aid}:%"),
        )
        rows = q.all()
        s.expunge_all()
    finally:
        s.close()
    return [i for i in rows if code is None or f":{code}:" in i.fingerprint]


def open_items(h, aid, code=None):
    return [i for i in items(h, aid, code) if i.state != "resolved"]


def drift(h, sid, *, open_only=True):
    s = session(h)
    try:
        rows = s.query(AISystemDriftEvent).filter_by(
            tenant_id=T1, ai_system_id=sid, drift_class="agent").all()
        s.expunge_all()
    finally:
        s.close()
    return [e for e in rows if not open_only or e.status in ("active", "acknowledged")]


FP_A = "sha256:" + "a" * 64
FP_B = "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


class TestDeclaration:
    def test_create_and_patch_round_trip_normalised_and_sorted(self, h):
        r = h.client.post("/v1/admin/agent-identities", headers=h.headers(T1), json={
            "name": "decl-agent", "implementation_name": "  Screening-Agent ",
            "approved_versions": ["1.4.*", "1.2.0", "1.4.*"], "require_release": True,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["implementation_name"] == "screening-agent"
        assert body["approved_versions"] == ["1.2.0", "1.4.*"]
        assert body["require_release"] is True
        aid = body["id"]

        # Only what is sent changes; null in a PATCH is "not sent".
        body = patch_agent(h, aid, approved_versions=None, description="d")
        assert body["approved_versions"] == ["1.2.0", "1.4.*"]
        body = patch_agent(h, aid, approved_versions=[], implementation_name=None,
                           require_release=False)
        assert (body["approved_versions"], body["implementation_name"], body["require_release"]) == (
            [], None, False)
        got = h.client.get(f"/v1/admin/agent-identities/{aid}", headers=h.headers(T1)).json()
        assert got["approved_versions"] == [] and got["implementation_name"] is None

    @pytest.mark.parametrize("bad", [
        "*", ".*", "1.*.2", "1.4*", "", "1 4", "1.4.**", "a/b", "x" * 65,
    ])
    def test_every_invalid_version_pattern_is_refused(self, h, bad):
        aid = agent(h, f"bad-{next(_n)}")
        r = h.client.patch(f"/v1/admin/agent-identities/{aid}", headers=h.headers(T1),
                           json={"approved_versions": ["1.0.0", bad]})
        assert r.status_code == 422, (bad, r.text)
        s = session(h)
        try:
            assert s.get(AgentIdentity, aid).approved_versions == []  # nothing half-applied
        finally:
            s.close()

    @pytest.mark.parametrize("bad", ["Has Space", "a" * 65, "émoji", "a/b"])
    def test_an_implementation_name_outside_the_core_bounds_is_refused(self, h, bad):
        aid = agent(h, f"badname-{next(_n)}")
        r = h.client.patch(f"/v1/admin/agent-identities/{aid}", headers=h.headers(T1),
                           json={"implementation_name": bad})
        assert r.status_code == 422, r.text

    @pytest.mark.parametrize("version,pattern,match", [
        ("1.4.2", "1.4.2", True),
        ("1.4.2", "1.4.*", True),
        ("1.4.2.1", "1.4.*", True),
        ("1.40", "1.4.*", False),
        ("1.4", "1.4.*", False),
        ("1.4.2", "1.4.3", False),
        ("1.4.2-rc1", "1.4.*", True),
        (None, "1.4.*", False),
    ])
    def test_version_matching(self, version, pattern, match):
        from app.services.agent_implementation import version_matches

        assert version_matches(version, pattern) is match

    def test_approval_is_observe_only_when_nothing_is_approved(self):
        from app.services.agent_implementation import approval_of

        assert approval_of("9.9.9", []) is None
        assert approval_of("9.9.9", ["1.*"]) is False
        assert approval_of("1.2", ["1.*"]) is True


# ---------------------------------------------------------------------------
# Releases and the composition view
# ---------------------------------------------------------------------------


class TestReleases:
    def test_timeline_is_latest_first_with_derived_approval(self, h):
        aid = agent(h, "tl-agent", impl="screening", approved=["1.4.*"])
        old = release(h, aid, "screening", "1.3.0", runs=40, first_days_ago=9, last_days_ago=3)
        cur = release(h, aid, "screening", "1.4.1", build="sha256:" + "c" * 64,
                      trust="verified", runs=5, first_days_ago=2, last_days_ago=0,
                      fingerprints=(FP_A, FP_B))
        body = releases(h, aid)
        assert body["current_release_id"] == cur and body["total"] == 2
        assert [r["id"] for r in body["releases"]] == [cur, old]
        first, second = body["releases"]
        assert first["current"] is True and second["current"] is False
        assert (first["approved"], second["approved"]) == (True, False)
        assert first["trust"] == "verified" and first["fingerprint_count"] == 2
        assert first["build"] == "sha256:" + "c" * 64 and second["build"] is None
        assert first["label"] == f"screening@1.4.1#sha256:{'c' * 64}"
        assert first["name_matches"] is True

        # Approving a version changes `approved` everywhere at once — no
        # release row is written (approved is never stored).
        patch_agent(h, aid, approved_versions=["1.3.0", "1.4.*"])
        assert [r["approved"] for r in releases(h, aid)["releases"]] == [True, True]
        patch_agent(h, aid, approved_versions=[])
        assert [r["approved"] for r in releases(h, aid)["releases"]] == [None, None]

    def test_unknown_identity_is_404(self, h):
        r = h.client.get("/v1/admin/agent-identities/nope/releases", headers=h.headers(T1))
        assert r.status_code == 404

    def test_composition_carries_each_member_agents_current_release(self, h):
        sid = system(h, "cv-sys")
        a = agent(h, "cv-a", member_of=[sid], approved=["2.*"], impl="alpha")
        b = agent(h, "cv-b", member_of=[sid])
        release(h, a, "alpha", "1.9.0", last_days_ago=2)
        release(h, a, "alpha", "2.0.1", trust="verified", last_days_ago=0)
        by_id = {x["id"]: x for x in composition(h, sid)["agents"]}
        assert by_id[a]["release"]["version"] == "2.0.1"
        assert by_id[a]["release"]["trust"] == "verified"
        assert by_id[a]["release"]["approved"] is True
        assert by_id[a]["approved_versions"] == ["2.*"]
        assert by_id[a]["implementation_name"] == "alpha"
        assert by_id[b]["release"] is None  # never declared one


# ---------------------------------------------------------------------------
# Contract closure
# ---------------------------------------------------------------------------


def _closure(h, sid):
    from app.services.ai_system_closure import resolve_closure

    s = session(h)
    try:
        return resolve_closure(s, s.get(ResourceGroup, sid))
    finally:
        s.close()


class TestContract:
    def test_the_declaration_is_conditional_sorted_and_drifts_the_contract(self, h):
        from app.database.models.ai_system_contract import hash_closure

        sid = system(h, "cc-sys")
        aid = agent(h, "cc-agent", member_of=[sid])
        (entry,) = _closure(h, sid)["composition"]["agent_identities"]
        # Nothing declared → the pre-RFC entry exactly (no fleet-wide rehash).
        assert set(entry) == {"id", "name", "role", "enforcement_mode", "autonomy_level"}
        before = hash_closure(_closure(h, sid))

        patch_agent(h, aid, approved_versions=["2.0.0", "1.4.*"], implementation_name="cc",
                    require_release=True)
        (entry,) = _closure(h, sid)["composition"]["agent_identities"]
        assert entry["approved_versions"] == ["1.4.*", "2.0.0"]
        assert entry["implementation_name"] == "cc" and entry["require_release"] is True
        declared = hash_closure(_closure(h, sid))
        assert declared != before

        patch_agent(h, aid, approved_versions=["1.4.*", "2.0.0", "2.1.0"])
        assert hash_closure(_closure(h, sid)) != declared  # approving drifts it

    def test_changing_approved_versions_drifts_the_active_contract(self, h):
        from app.services.ai_system_contract_service import (
            approve,
            is_config_drifted,
            mint,
            promote,
        )

        sid = system(h, "cd-sys")
        aid = agent(h, "cd-agent", member_of=[sid], approved=["1.0.0"])
        s = session(h)
        try:
            c, _ = mint(s, s.get(ResourceGroup, sid), created_by="t")
            approve(s, c, approved_by="approver")
            promote(s, c, lifecycle_stage="active")
            s.commit()
            assert is_config_drifted(s, s.get(ResourceGroup, sid)) is False
        finally:
            s.close()
        patch_agent(h, aid, approved_versions=["1.0.0", "1.1.0"])
        s = session(h)
        try:
            assert is_config_drifted(s, s.get(ResourceGroup, sid)) is True
        finally:
            s.close()

    @pytest.mark.parametrize("old,new,widened,narrowed", [
        ({"approved_versions": ["1.0"]}, {"approved_versions": ["1.0", "1.1"]}, ["1.1"], []),
        ({"approved_versions": ["1.0", "1.1"]}, {"approved_versions": ["1.0"]}, [], ["1.1"]),
        ({}, {"approved_versions": ["1.0"]}, [], ["approved_versions"]),
        ({"approved_versions": ["1.0"]}, {}, ["approved_versions"], []),
        ({}, {"require_release": True}, [], ["require_release"]),
        ({"require_release": True}, {}, ["require_release"], []),
        ({}, {"implementation_name": "x"}, [], ["implementation_name"]),
        ({"implementation_name": "x"}, {}, ["implementation_name"], []),
        ({"implementation_name": "x"}, {"implementation_name": "y"}, ["implementation_name"], []),
    ])
    def test_the_diff_classifies_each_direction(self, old, new, widened, narrowed):
        from app.services.ai_system_contract_service import diff_closures

        def closure(extra):
            return {"composition": {"agent_identities": [{"id": "a1", "name": "A", **extra}]}}

        d = diff_closures(closure(old), closure(new))

        def keys(entries):
            return [e.get("version") or e["field"] for e in entries
                    if e["kind"] == "agent_implementation"]

        assert keys(d["widened"]) == widened
        assert keys(d["narrowed"]) == narrowed


# ---------------------------------------------------------------------------
# Client labels
# ---------------------------------------------------------------------------


class TestClientLabels:
    def test_a_member_agent_is_never_its_systems_client(self, h):
        from app.services import ai_system_conformance as conformance

        sid = system(h, "cl-sys", clients=["portal"])
        aid = agent(h, "cl-agent", member_of=[sid])
        outsider = agent(h, "cl-outsider")
        for _ in range(3):
            run(h, sid, aid=aid, client="python-httpx")
        run(h, sid, aid=outsider, client="python-httpx")
        run(h, sid, client="portal")
        s = session(h)
        try:
            report = conformance.for_system(s, s.get(ResourceGroup, sid), period_hours=24)
            observed = conformance.observed_clients(s, T1, sid, 24)
        finally:
            s.close()
        # The member agent's three runs are not judged as a client …
        assert observed == {"python-httpx": 1, "portal": 1}
        assert report.undeclared_clients == ["python-httpx"]  # … the outsider's run is
        # … but they are the system at work: counted as observed traffic.
        assert report.member_agent_runs == 3
        assert report.observed_call_count == 5
        assert report.to_dict()["member_agent_runs"] == 3
        # The Composition tab reads the same computation.
        labels = {c["label"]: c["observed_calls"] for c in composition(h, sid)["client_labels"]}
        assert labels == {"portal": 1, "python-httpx": 1}

    def test_a_system_called_only_by_its_own_agent_is_measured_not_nonconformant(self, h):
        from app.services import ai_system_conformance as conformance

        sid = system(h, "cl-own", clients=["portal"])
        aid = agent(h, "cl-own-agent", member_of=[sid])
        for _ in range(25):
            run(h, sid, aid=aid, client="python-httpx")
        s = session(h)
        try:
            report = conformance.for_system(s, s.get(ResourceGroup, sid), period_hours=24)
        finally:
            s.close()
        assert report.undeclared_clients == [] and report.score is not None
        assert report.observed_call_count == 25


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


class TestUnapprovedRelease:
    def test_fires_once_at_the_systems_severity_and_clears_when_the_runs_age_out(self, h):
        high = system(h, "ur-high", tier="high")
        low = system(h, "ur-low", tier="minimal")
        aid = agent(h, "ur-agent", member_of=[high, low], approved=["1.0.*"])
        rel = release(h, aid, "ur", "1.1.0")
        release(h, aid, "ur", "1.0.3", last_days_ago=1)  # approved: no finding
        run(h, high, aid=aid, name="ur", version="1.1.0", minutes_ago=60)
        run(h, high, aid=aid, name="ur", version="1.0.3", minutes_ago=24 * 60)
        checks(h)
        checks(h)
        (item,) = open_items(h, aid, "agent.unapproved_release")
        assert item.fingerprint == f"agent:{aid}:agent.unapproved_release:ur@1.1.0"
        assert (item.severity, item.ai_system_id, item.occurrence_count) == ("critical", high, 2)
        assert item.evidence["release"]["id"] == rel
        ((ev_high,), (ev_low,)) = drift(h, high), drift(h, low)
        assert (ev_high.severity, ev_low.severity) == ("high", "medium")
        assert ev_high.metric_key == f"agent.unapproved_release:{aid}"
        assert ev_high.attributed_cause == "agent_release"

        # A release is judged by when it ran: approving 1.1.0 now does not
        # rewrite history — the run an hour ago was unapproved when it ran.
        # Runs after the approval are not, so no new evidence accrues …
        patch_agent(h, aid, approved_versions=["1.0.*", "1.1.0"])
        run(h, high, aid=aid, name="ur", version="1.1.0", minutes_ago=-1)
        checks(h)
        (item,) = open_items(h, aid, "agent.unapproved_release")
        assert item.evidence["unapproved_runs"] == 1
        # … and the finding clears once that run leaves the 7-day window.
        stats = checks(h, now=_now() + timedelta(days=7, hours=1))
        assert open_items(h, aid) == [] and stats["resolved"] == 1
        (item,) = items(h, aid, "agent.unapproved_release")
        assert (item.disposition, item.resolved_by) == ("fixed", "system:agent-checks")
        assert drift(h, high) == [] and drift(h, low) == []

    def test_clears_when_not_seen_for_seven_days(self, h):
        sid = system(h, "ur7-sys")
        aid = agent(h, "ur7-agent", member_of=[sid], approved=["2.0.0"])
        release(h, aid, "ur7", "1.0.0", first_days_ago=20, last_days_ago=6.5)
        run(h, sid, aid=aid, name="ur7", version="1.0.0", minutes_ago=6.5 * 24 * 60)
        checks(h)
        assert len(open_items(h, aid, "agent.unapproved_release")) == 1
        checks(h, now=_now() + timedelta(days=1))
        assert open_items(h, aid) == []

    def test_observe_only_identities_never_fire(self, h):
        sid = system(h, "uro-sys")
        aid = agent(h, "uro-agent", member_of=[sid])
        release(h, aid, "uro", "9.9.9")
        run(h, sid, aid=aid, name="uro", version="9.9.9")
        checks(h)
        assert items(h, aid) == [] and drift(h, sid) == []


class TestNameMismatch:
    def test_fires_on_the_current_release_and_clears_when_it_matches(self, h):
        sid = system(h, "nm-sys")
        aid = agent(h, "nm-agent", member_of=[sid], impl="screening")
        release(h, aid, "screening", "1.0.0", last_days_ago=2)
        release(h, aid, "python-httpx", "0.28.1", last_days_ago=0)
        checks(h)
        (item,) = open_items(h, aid, "agent.name_mismatch")
        assert item.severity == "critical"
        assert item.fingerprint.endswith(":python-httpx@0.28.1")
        release(h, aid, "screening", "1.0.1", first_days_ago=0.01, last_days_ago=-0.01)
        checks(h)
        assert open_items(h, aid) == []


class TestReleaseMissing:
    def test_fires_on_a_run_without_a_release_and_clears_when_runs_carry_it(self, h):
        sid = system(h, "rm-sys")
        aid = agent(h, "rm-agent", member_of=[sid], require=True)
        run(h, sid, aid=aid, name="rm", version="1.0.0", minutes_ago=120)
        run(h, sid, aid=aid, name="rm", version=None, minutes_ago=60)
        checks(h)
        (item,) = open_items(h, aid, "agent.release_missing")
        assert item.fingerprint == f"agent:{aid}:agent.release_missing:-"
        assert item.severity == "warning"
        run(h, sid, aid=aid, name="rm", version="1.0.0", minutes_ago=1)
        checks(h)
        assert open_items(h, aid) == []

    def test_not_required_never_fires(self, h):
        sid = system(h, "rmn-sys")
        aid = agent(h, "rmn-agent", member_of=[sid])
        run(h, sid, aid=aid)
        checks(h)
        assert items(h, aid) == []


class TestUndeclaredChange:
    def _seed(self, h, tag, established_runs):
        sid = system(h, f"uc-{tag}")
        aid = agent(h, f"uc-{tag}-agent", member_of=[sid])
        release(h, aid, "uc", "1.0.0", runs=established_runs + 1, fingerprints=(FP_A, FP_B))
        for i in range(established_runs):
            run(h, sid, aid=aid, name="uc", version="1.0.0", fp=FP_A, minutes_ago=500 - i)
        run(h, sid, aid=aid, name="uc", version="1.0.0", fp=FP_B, minutes_ago=10)
        return sid, aid

    def test_a_new_fingerprint_after_the_minimum_is_a_change(self, h):
        from app.services.assurance_checks.agent import MIN_RUNS_FOR_UNDECLARED_CHANGE

        sid, aid = self._seed(h, "fire", MIN_RUNS_FOR_UNDECLARED_CHANGE)
        checks(h)
        (item,) = open_items(h, aid, "agent.undeclared_change")
        assert item.severity == "critical"
        assert item.evidence["no_version_change"] is True
        assert item.evidence["new_fingerprint"] == FP_B
        (ev,) = drift(h, sid)
        assert ev.severity == "high"

    def test_below_the_minimum_the_release_is_still_settling(self, h):
        from app.services.assurance_checks.agent import MIN_RUNS_FOR_UNDECLARED_CHANGE

        _sid, aid = self._seed(h, "settle", MIN_RUNS_FOR_UNDECLARED_CHANGE - 1)
        checks(h)
        assert items(h, aid) == []

    def test_acknowledged_stays_cleared_and_a_new_version_clears_it(self, h):
        from app.services.assurance_inbox import AssuranceInboxService

        sid, aid = self._seed(h, "ack", 20)
        checks(h)
        (item,) = open_items(h, aid, "agent.undeclared_change")
        s = session(h)
        try:
            AssuranceInboxService().acknowledge(s, s.get(AssuranceInboxItem, item.id), admin="owner")
            s.commit()
        finally:
            s.close()
        checks(h)
        (item,) = items(h, aid, "agent.undeclared_change")
        assert item.state == "acknowledged"  # not re-raised, not auto-resolved
        assert drift(h, sid) == []  # the response finding is cleared

        sid2, aid2 = self._seed(h, "newver", 20)
        checks(h)
        assert len(open_items(h, aid2, "agent.undeclared_change")) == 1
        release(h, aid2, "uc", "1.1.0", first_days_ago=0.01, last_days_ago=-0.01)
        checks(h)
        assert open_items(h, aid2) == [] and drift(h, sid2) == []


# ---------------------------------------------------------------------------
# Lifecycle (agent.new_release)
# ---------------------------------------------------------------------------


class TestNewReleaseEvents:
    def test_one_event_per_release_per_member_system_exactly_once(self, h):
        a_sys = system(h, "nr-a")
        b_sys = system(h, "nr-b")
        aid = agent(h, "nr-agent", member_of=[a_sys, b_sys], approved=["1.*"])
        r1 = release(h, aid, "nr", "1.0.0", first_days_ago=3, last_days_ago=2)
        r2 = release(h, aid, "nr", "1.1.0", first_days_ago=1, last_days_ago=0)
        assert checks(h)["release_events"] == 4
        assert checks(h)["release_events"] == 0  # idempotent
        s = session(h)
        try:
            rows = s.query(AISystemLifecycleEvent).filter_by(
                tenant_id=T1, event_type="agent.new_release").all()
            got = sorted((e.ai_system_id, e.subject_id) for e in rows
                         if e.subject_id in (r1, r2))
            ev = next(e for e in rows if e.subject_id == r2 and e.ai_system_id == a_sys)
            assert ev.to_stage is None and ev.recorded_by == "system:agent-releases"
            assert ev.details["version"] == "1.1.0" and ev.details["approved"] is True
        finally:
            s.close()
        assert got == sorted([(a_sys, r1), (a_sys, r2), (b_sys, r1), (b_sys, r2)])

        body = h.client.get(f"/v1/admin/ai-systems/{a_sys}/lifecycle", headers=h.headers(T1)).json()
        assert [e["subject_id"] for e in body["events"]] == [r2, r1]  # newest first
        assert all(e["event_type"] == "agent.new_release" for e in body["events"])
        assert all(e["event_type"] == "stage_transition" for e in body["history"])

    def test_releases_before_the_identity_joined_are_not_new_to_the_system(self, h):
        sid = system(h, "nr-late")
        aid = agent(h, "nr-late-agent", member_of=[sid], joined_minutes_ago=60)
        old = release(h, aid, "late", "0.9.0", first_days_ago=5, last_days_ago=0)
        checks(h)
        s = session(h)
        try:
            assert s.query(AISystemLifecycleEvent).filter_by(subject_id=old).count() == 0
        finally:
            s.close()

    def test_the_database_refuses_a_duplicate(self, h):
        from sqlalchemy.exc import IntegrityError

        sid = system(h, "nr-dup")
        aid = agent(h, "nr-dup-agent", member_of=[sid])
        rel = release(h, aid, "dup", "1.0.0")
        checks(h)
        s = session(h)
        try:
            s.add(AISystemLifecycleEvent(
                id="lce-dup", tenant_id=T1, ai_system_id=sid, event_type="agent.new_release",
                subject_id=rel, recorded_by="x",
            ))
            with pytest.raises(IntegrityError):
                s.flush()
        finally:
            s.rollback()
            s.close()

    def test_a_stage_transition_still_needs_a_stage(self, h):
        from sqlalchemy.exc import IntegrityError

        sid = system(h, "nr-stage")
        s = session(h)
        try:
            s.add(AISystemLifecycleEvent(id="lce-nostage", tenant_id=T1, ai_system_id=sid,
                                         recorded_by="x"))
            with pytest.raises(IntegrityError):
                s.flush()
        finally:
            s.rollback()
            s.close()


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class TestResponses:
    def test_an_unapproved_release_on_a_high_risk_system_moves_it_to_approval_required(self, h):
        from app.services.ai_system_assurance import upsert_health
        from app.services.ai_system_response.actuator import apply_plan, plan_for_drift

        high = system(h, "rs-high", tier="high")
        low = system(h, "rs-low", tier="limited")
        aid = agent(h, "rs-agent", member_of=[high, low], approved=["1.0.0"])
        release(h, aid, "rs", "2.0.0")
        run(h, high, aid=aid, name="rs", version="2.0.0")
        for sid in (high, low):
            h.seed(lambda s, sid=sid: s.add(AISystemResponsePolicy(
                id=f"pol-{sid}", tenant_id=T1, ai_system_id=sid, name="unapproved release",
                signal_kind="drift", drift_class="agent", severity_at_least="high",
                actions=[{"kind": "set_autonomy", "level": "approval_required"}],
            )))
        checks(h)
        s = session(h)
        try:
            (ev_high,) = [e for e in s.query(AISystemDriftEvent).filter_by(
                ai_system_id=high, drift_class="agent")]
            plan = plan_for_drift(s, ev_high)
            assert [a.to_dict()["params"] for a in plan.actions] == [{"level": "approval_required"}]
            apply_plan(s, ev_high, plan)
            s.commit()
            assert s.get(ResourceGroup, high).autonomy_level == "approval_required"

            # Medium on a limited-risk system: below the policy's floor.
            (ev_low,) = [e for e in s.query(AISystemDriftEvent).filter_by(
                ai_system_id=low, drift_class="agent")]
            assert ev_low.severity == "medium" and not plan_for_drift(s, ev_low).will_act

            # A governance finding, not the system behaving differently.
            row = upsert_health(s, s.get(ResourceGroup, high))
            s.commit()
            assert row.open_drift_events == 0
        finally:
            s.close()

    def test_the_closed_loop_does_not_double_raise_agent_findings(self):
        from app.services.ai_system_response.scheduler import SELF_RAISED_DRIFT_CLASSES

        assert "agent" in SELF_RAISED_DRIFT_CLASSES and "contract" in SELF_RAISED_DRIFT_CLASSES


# ---------------------------------------------------------------------------
# Assurance report
# ---------------------------------------------------------------------------


class TestReport:
    def test_composition_lists_current_release_trust_and_approval(self, h):
        import jsonschema

        from app.services.ai_system_assurance import build_report
        from app.standards import REPORT_SPEC, REPORT_SPEC_VERSION, load_schema

        sid = system(h, "rp-sys", tier="high")
        aid = agent(h, "rp-agent", member_of=[sid], approved=["1.*"])
        agent(h, "rp-silent", member_of=[sid])
        release(h, aid, "rp", "2.0.0", trust="verified")
        run(h, sid, aid=aid, name="rp", version="2.0.0")
        s = session(h)
        try:
            report = build_report(s, s.get(ResourceGroup, sid))
        finally:
            s.close()
        assert REPORT_SPEC_VERSION == "1.10"  # judgement_exclusions (1.10: excluded approval requests)
        agents = {a["id"]: a for a in report["composition"]["agents"]}
        assert agents[aid]["release"]["version"] == "2.0.0"
        assert agents[aid]["release"]["trust"] == "verified"
        assert agents[aid]["release"]["approved"] is False
        assert agents["rp-silent"]["release"] is None
        (f,) = report["composition"]["agent_findings"]
        assert (f["code"], f["severity"], f["agent_id"]) == ("agent.unapproved_release", "high", aid)
        jsonschema.validate(report, load_schema(REPORT_SPEC))


# ---------------------------------------------------------------------------
# Class-level guards
# ---------------------------------------------------------------------------

APP = Path(__file__).resolve().parents[1] / "app"


def test_agent_findings_are_constructed_in_one_module():
    """One fact, one computation: the finding codes are built only by the
    check library (and named in the wire constants' home). A second module
    constructing one is a second computation that will disagree."""
    home = APP / "services" / "assurance_checks" / "agent.py"
    offenders = [
        source_label(p) for p in python_sources(APP)
        if p != home and re.search(
            r"[\"']agent\.(unapproved_release|name_mismatch|release_missing|undeclared_change)[\"']",
            p.read_text())
    ]
    assert offenders == []


#: The only modules that may read what an agent DECLARES about itself. Every
#: one of them describes (records, compares, reports); none decides access.
_DESCRIPTIVE = {
    "constants/agent_implementation.py",
    "database/models/__init__.py",
    "database/models/agent_identity.py",
    "database/models/agent_identity_approval_history.py",
    "database/models/agent_implementation_release.py",
    "database/models/ai_system_run.py",
    "database/models/ai_system_run_segment.py",
    "database/models/proxy_log.py",
    "database/models/ai_system_lifecycle_event.py",
    "services/agent_implementation.py",
    "services/assurance_checks/agent.py",
    "services/ai_system_closure.py",
    "services/ai_system_contract_service.py",
    "services/ai_system_composition.py",
    "services/ai_system_composite_compliance.py",
    "api/v1/endpoints/admin/agent_identities.py",
    "api/v1/models/admin/agent_identity.py",
    "api/v1/models/admin/ai_system_composition.py",
    "api/v1/models/admin/ai_system_run.py",
    "api/v1/endpoints/admin/ai_system_runs.py",
    # a188_audit_chain_v3 — the audit-chain reference verifier names the columns only to
    # HASH them (the canonical row format); it decides nothing.
    "services/audit_chain_canonical.py",
    # The tier-A check vocabulary (RUN_FACT_KEYS / RUN_FACT_GUIDE): checks
    # raise findings about a run's release — they describe, never admit.
    "constants/assurance.py",
    # The AI Systems gallery card shows the latest reported release (from
    # runs, else from served-call segments) as text on the card; it is
    # read-only display and gates nothing.
    "services/ai_system_card_facts.py",
}


def test_declared_implementation_values_never_reach_an_access_path():
    """Identity decides; declarations describe. Any module
    outside the descriptive allowlist that reads a declared release (or the
    approval of one) fails here, so wiring one into an authorization,
    access or routing path is a deliberate, reviewed act."""
    pattern = re.compile(
        r"\bagent_impl_(name|version|build|source|trust)\b|\bimplementation_fingerprint\b"
        r"|\bapproved_versions\b|\bimplementation_name\b|\brequire_release\b"
        r"|AgentImplementationRelease\b"
    )
    offenders = sorted(
        source_label(p) for p in python_sources(APP)
        if source_label(p) not in _DESCRIPTIVE and pattern.search(p.read_text())
    )
    assert offenders == []

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The lifecycle gate's identity floor: `workload_identity_enforced`.

Promoting an AI System to `active` (from `approved`, re-promoting it, or
bringing it back from `deprecated`) requires that machine callers
authenticate with workload identity: the policy at `active` requires it,
and no call of the system's subtree in the last 7 days arrived below that
level with a credential that can still authenticate. It is a floor: no
tenant gate policy removes it.

What is worth defending:

  * **The floor survives every tenant policy**, on all three transitions.
  * **Live credentials block; retired ones do not.** A revoked, expired or
    deleted key clears the requirement at once; a disabled key (which can be
    re-enabled) still blocks.
  * **Only machine callers below the level count**: refused calls, people's
    sessions, the platform's own credential, anonymous calls, calls outside
    the window or outside the subtree do not.
  * **The refusal names the credentials**, newest first, with a total.
  * **A high-risk system needs proof of possession**, so a workload token
    below it blocks.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.endpoints.admin.ai_system_contracts import create_ai_system_contracts_router
from app.core.logging_system import BrutorLogger
from app.database.models.agent_identity import AgentIdentity
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.api_key import APIKey
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services import ai_system_lifecycle_gate as gate
from tests.admin_endpoint_harness import build_admin_harness, throwaway_pg_database

LABEL = (
    "Machine callers authenticate with workload identity: the identity policy requires it, "
    "and no call in the last 7 days arrived below that level"
)

PROMOTIONS = ("approved->active", "active->active", "deprecated->active")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _harness(database_url=None):
    log = BrutorLogger("test-identity-floor")
    return build_admin_harness([create_ai_system_contracts_router(log)], logger_name="test-identity-floor",
                               database_url=database_url)


@pytest.fixture(params=["sqlite", "postgres"])
def h(request):
    if request.param == "sqlite":
        harness = _harness()
        yield harness
        harness.close()
        return
    if os.environ.get("BRUTOR_PG_TESTS") != "1":
        pytest.skip("needs Postgres; set BRUTOR_PG_TESTS=1 (and BRUTOR_PG_TEST_URL for a non-default server)")
    database = throwaway_pg_database("r32_gate")
    url = next(database)
    harness = _harness(url)
    try:
        yield harness
    finally:
        harness.close()
        next(database, None)


def _system(h, sid, *, stage="approved", tier=None, parent=None, ai_system=True):
    def fn(s):
        s.add(ResourceGroup(
            id=sid, tenant_id="t1", name=f"name-{sid}", parent_group_id=parent,
            hierarchy_level=0 if parent is None else 1, inherit_resources=True, is_active=True,
            is_system=False, group_type=GroupType.AI_SYSTEM if ai_system else GroupType.TEAM,
            system_kind="agent" if ai_system else None, lifecycle_stage=stage, eu_ai_act_risk_tier=tier,
        ))

    h.seed(fn)
    return sid


def _key(h, kid, *, revoked=False, expires_in_days=None, enabled=True, scope="global"):
    def fn(s):
        s.add(APIKey(
            id=kid, tenant_id="t1", name=kid, key_hash=uuid.uuid4().hex, key_prefix="sk_x",
            scope=scope, enabled=enabled, revoked=revoked,
            expires_at=None if expires_in_days is None else _now() + timedelta(days=expires_in_days),
        ))

    h.seed(fn)
    return kid


def _agent(h, aid, *, revoked=False):
    def fn(s):
        s.add(AgentIdentity(id=aid, tenant_id="t1", name=aid,
                            revoked_at=_now() if revoked else None))

    h.seed(fn)
    return aid


def _call(h, group, *, kind="api_key", level="key", ref=None, outcome="below_policy", days_ago=1.0,
          api_key_id=None, agent=None, tenant="t1"):
    def fn(s):
        s.add(ProxyLog(
            id=str(uuid.uuid4()), tenant_id=tenant, timestamp=_now() - timedelta(days=days_ago),
            proxy_type="llm", request_id=str(uuid.uuid4()), end_user_session_id="sess-1",
            http_method="POST", endpoint="/v1/chat/completions", success=outcome != "refused",
            resource_group_id=group, credential_kind=kind, credential_assurance=level,
            credential_ref=ref or (f"api_key:{api_key_id}" if api_key_id else None),
            credential_policy_outcome=outcome, api_key_id=api_key_id, actor_agent_id=agent,
        ))

    h.seed(fn)


def _evaluate(h, sid, to_stage="active", tenant_policy=None):
    s = h.Factory()
    try:
        system = s.query(ResourceGroup).filter(ResourceGroup.id == sid).one()
        return gate.evaluate(s, system, to_stage, tenant_policy=tenant_policy)
    finally:
        s.close()


def _requirement(decision):
    found = [r for r in decision.requirements if r.key == gate.REQ_WORKLOAD_IDENTITY]
    assert len(found) == 1, [r.key for r in decision.requirements]
    return found[0]


# ---------------------------------------------------------------------------
# The requirement and where it applies
# ---------------------------------------------------------------------------


class TestPlacement:
    def test_the_requirement_and_its_label(self):
        assert gate.REQ_WORKLOAD_IDENTITY == "workload_identity_enforced"
        assert gate.REQUIREMENT_LABELS[gate.REQ_WORKLOAD_IDENTITY] == LABEL
        assert gate.IDENTITY_LOOKBACK_DAYS == 7

    def test_it_is_a_default_of_the_three_promotions(self):
        for key in PROMOTIONS:
            assert gate.REQ_WORKLOAD_IDENTITY in gate.DEFAULT_REQUIREMENTS[key], key
        assert "deprecated->active" not in gate.ALWAYS_ALLOWED

    @pytest.mark.parametrize("transition", PROMOTIONS)
    def test_no_tenant_policy_removes_it(self, transition):
        from_stage, to_stage = transition.split("->")
        system = ResourceGroup(id="s", tenant_id="t1", lifecycle_stage=from_stage, eu_ai_act_risk_tier=None)
        assert gate.REQ_WORKLOAD_IDENTITY in gate.required_keys(system, to_stage, tenant_policy={transition: []})
        assert gate.REQ_WORKLOAD_IDENTITY in gate.required_keys(system, to_stage)

    def test_it_is_not_required_off_the_promotions(self):
        for from_stage, to_stage in (("proposed", "approved"), ("active", "deprecated"), ("deprecated", "retired"),
                                     ("active", "approved")):
            system = ResourceGroup(id="s", tenant_id="t1", lifecycle_stage=from_stage)
            assert gate.REQ_WORKLOAD_IDENTITY not in gate.required_keys(system, to_stage)

    def test_a_tenant_policy_may_name_it(self):
        assert gate.validate_tenant_policy({"approved->active": [gate.REQ_WORKLOAD_IDENTITY]}) == []
        assert gate.validate_tenant_policy({"deprecated->active": []}) == []


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class TestEvaluation:
    def test_no_traffic_is_satisfied(self, h):
        sid = _system(h, "sys-quiet")
        req = _requirement(_evaluate(h, sid))
        assert req.satisfied, req.detail
        assert req.offending_credentials is None and req.offending_total is None

    def test_a_live_key_used_in_the_window_blocks(self, h):
        sid = _system(h, "sys-k")
        child = _system(h, "rg-k-child", parent=sid, ai_system=False, stage=None)
        _key(h, "k-live")
        _call(h, sid, api_key_id="k-live", outcome="met", days_ago=3)  # made while approved: counts
        _call(h, child, api_key_id="k-live", days_ago=1)
        req = _requirement(_evaluate(h, sid))
        assert not req.satisfied
        assert req.offending_total == 1
        (only,) = req.offending_credentials
        assert only["credential_ref"] == "api_key:k-live"
        assert only["credential_kind"] == "api_key"
        assert only["credential_assurance"] == "key"
        assert only["calls"] == 2
        assert only["last_seen"]
        assert req.detail.startswith("1 credential(s) reached this system below workload in the last 7 days: ")
        assert "api_key:k-live (2 calls, last " in req.detail

    def test_a_disabled_key_still_blocks(self, h):
        sid = _system(h, "sys-dis")
        _key(h, "k-off", enabled=False)
        _call(h, sid, api_key_id="k-off")
        assert not _requirement(_evaluate(h, sid)).satisfied

    @pytest.mark.parametrize("retired", ["revoked", "expired", "deleted"])
    def test_a_retired_key_does_not_block(self, h, retired):
        sid = _system(h, f"sys-{retired}")
        if retired == "revoked":
            _key(h, "k-r", revoked=True)
        elif retired == "expired":
            _key(h, "k-r", expires_in_days=-1)
        _call(h, sid, api_key_id="k-r")
        req = _requirement(_evaluate(h, sid))
        assert req.satisfied, req.detail

    def test_what_does_not_count(self, h):
        sid = _system(h, "sys-nc")
        other = _system(h, "sys-other")
        _key(h, "k1")
        _call(h, sid, api_key_id="k1", outcome="refused")  # refused
        _call(h, sid, api_key_id="k1", days_ago=8)  # outside the window
        _call(h, other, api_key_id="k1")  # another system
        _call(h, sid, kind="human_session", level=None, ref="session:admin:1", outcome="exempt")
        _call(h, sid, kind="platform_service", level=None, ref="api_key:svc", outcome="exempt")
        _call(h, sid, kind="anonymous", level=None, ref=None, outcome="refused")
        _call(h, sid, kind="workload_token", level="workload", ref="token:idp-1:spiffe://x/a", outcome="met",
              agent=_agent(h, "agent-ok"))
        req = _requirement(_evaluate(h, sid))
        assert req.satisfied, req.detail

    def test_the_list_is_newest_first_capped_and_totalled(self, h):
        sid = _system(h, "sys-many")
        for i in range(25):
            _key(h, f"k{i:02d}")
            _call(h, sid, api_key_id=f"k{i:02d}", days_ago=6 - i * 0.2)
        req = _requirement(_evaluate(h, sid))
        assert req.offending_total == 25
        assert len(req.offending_credentials) == 20
        refs = [c["credential_ref"] for c in req.offending_credentials]
        assert refs[0] == "api_key:k24"
        assert req.detail.startswith("25 credential(s) reached this system below workload in the last 7 days: ")
        assert req.detail.count(" calls, last ") == 3

    def test_a_high_risk_system_needs_proof_of_possession(self, h):
        sid = _system(h, "sys-hr", tier="high")
        _call(h, sid, kind="workload_token", level="workload", ref="token:idp-1:spiffe://x/a", outcome="met",
              agent=_agent(h, "agent-live"))
        req = _requirement(_evaluate(h, sid))
        assert not req.satisfied
        assert "below workload_pop" in req.detail
        assert req.offending_credentials[0]["credential_kind"] == "workload_token"

    def test_a_revoked_agents_token_does_not_block(self, h):
        sid = _system(h, "sys-hr2", tier="high")
        _call(h, sid, kind="workload_token", level="workload", ref="token:idp-1:spiffe://x/b", outcome="met",
              agent=_agent(h, "agent-gone", revoked=True))
        assert _requirement(_evaluate(h, sid)).satisfied

    def test_a_personal_key_counts(self, h):
        sid = _system(h, "sys-pk")
        _key(h, "k-user", scope="user")
        _call(h, sid, api_key_id="k-user")
        assert not _requirement(_evaluate(h, sid)).satisfied

    def test_another_tenants_rows_do_not_count(self, h):
        sid = _system(h, "sys-t")
        _call(h, sid, api_key_id="k-elsewhere", tenant="t2")
        assert _requirement(_evaluate(h, sid)).satisfied

    def test_deprecated_back_to_active_is_gated(self, h):
        sid = _system(h, "sys-dep", stage="deprecated")
        _key(h, "k-dep")
        _call(h, sid, api_key_id="k-dep")
        decision = _evaluate(h, sid)
        assert not decision.allowed
        assert gate.REQ_WORKLOAD_IDENTITY in [r.key for r in decision.unmet]


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


class TestHttp:
    def test_the_refusal_and_the_options_carry_the_credentials(self, h):
        sid = _system(h, "sys-http")
        _key(h, "k-http")
        _call(h, sid, api_key_id="k-http")
        h.seed(lambda s: s.add(AISystemLivenessExpectation(id="live-1", tenant_id="t1", ai_system_id=sid, enabled=False)))

        options = h.client.get(f"/v1/admin/ai-systems/{sid}/lifecycle", headers=h.headers())
        assert options.status_code == 200, options.text
        to_active = [t for t in options.json()["transitions"] if t["to_stage"] == "active"][0]
        unmet = [r for r in to_active["unmet"] if r["key"] == gate.REQ_WORKLOAD_IDENTITY][0]
        assert unmet["offending_total"] == 1
        assert unmet["offending_credentials"][0]["credential_ref"] == "api_key:k-http"
        met = [r for r in to_active["requirements"] if r["key"] != gate.REQ_WORKLOAD_IDENTITY]
        assert all(r["offending_credentials"] is None for r in met)

        r = h.client.post(f"/v1/admin/ai-systems/{sid}/lifecycle", json={"to_stage": "active"}, headers=h.headers())
        assert r.status_code == 409, r.text
        reqs = r.json()["detail"]["decision"]["requirements"]
        identity = [q for q in reqs if q["key"] == gate.REQ_WORKLOAD_IDENTITY][0]
        assert identity["satisfied"] is False
        assert identity["offending_credentials"][0]["calls"] == 1

    def test_the_vocabulary_states_both_floors(self, h):
        body = h.client.get("/v1/admin/ai-systems/gate-policy/vocabulary", headers=h.headers()).json()
        assert "immutable_floor" not in body
        floors = body["immutable_floors"]
        assert floors[0]["condition"] == "eu_ai_act_risk_tier == 'high'"
        assert floors[0]["always_required"] == ["human_approval"]
        assert floors[1]["condition"] == "to_stage == 'active'"
        assert floors[1]["transitions"] == list(PROMOTIONS)
        assert floors[1]["always_required"] == [gate.REQ_WORKLOAD_IDENTITY]
        assert floors[1]["note"] == "Re-added by the gate after any tenant override."
        transitions = {t["key"]: t for t in body["transitions"]}
        assert transitions["deprecated->active"]["default_requirements"] == [gate.REQ_WORKLOAD_IDENTITY]

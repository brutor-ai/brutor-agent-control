# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Declared human approval on the Composition view, and the closure terms it reads.

Each case below was checked against what the core actually does
(`agent_authz`, `llm_preflight`, `usage::CapabilityAccess`,
`skill_orchestrator`): a mark on the diagram claims a person is in the loop, so
what the core REFUSES instead of holding must never be drawn as a gate.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("ENCRYPTION_KEY", "a" * 44 + "=")

from datetime import timedelta
from itertools import count

import pytest

from app.api.v1.models.admin.ai_system_composition import CompositionResponse
from app.database.models.agent_identity import AgentGrant, AgentIdentity, ResourceGroupAgentMember
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.argument_policy import ArgumentPolicy
from app.database.models.llm_model import LLModel  # noqa: F401 — mapper registration
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_mcp_capabilities,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.services.ai_system_closure import resolve_and_hash
from app.services.ai_system_contract_service import diff_closures
from tests.test_ai_system_composition import T1, _now, card, composition, edge, h, system  # noqa: F401

_n = count(1)


def _id(prefix: str) -> str:
    return f"{prefix}-{next(_n)}"


def bind_mcp(h, sid, *tools):
    srv = _id("srv")

    def fn(s):
        s.add(MCPServer(id=srv, tenant_id=T1, name=srv, base_url=f"http://{srv}.test", enabled=True,
                        capabilities={"tools": [{"name": t} for t in tools]}))
        s.flush()
        s.add(ServerConfig(id=f"{srv}-cfg", tenant_id=T1, mcp_server_id=srv, name=srv, enabled=True))
        s.flush()
        s.execute(resource_group_server_configs.insert().values(
            id=_id("b"), resource_group_id=sid, server_config_id=f"{srv}-cfg"))
    h.seed(fn)
    return f"{srv}-cfg"


def bind_skill(h, sid, *, metadata=None, governance=None):
    skill = _id("skill")

    def fn(s):
        s.add(AgentSkill(id=skill, tenant_id=T1, name=skill, description="x",
                         skill_md_content="# skill", skill_md_hash="h", skill_metadata=metadata or {}))
        s.flush()
        s.execute(resource_group_agent_skills.insert().values(
            id=_id("b"), resource_group_id=sid, agent_skill_id=skill, governance=governance or {}))
    h.seed(fn)
    return skill


def grant(h, sid, action, target, effect, *, expires_at=None):
    agent = _id("agent")

    def fn(s):
        s.add(AgentIdentity(id=agent, tenant_id=T1, name=agent, enabled=True, status="active",
                            default_deny=True, enforcement_mode="enforce"))
        s.add(ResourceGroupAgentMember(tenant_id=T1, agent_id=agent, group_id=sid, role="component"))
        s.flush()
        s.add(AgentGrant(id=_id("grant"), tenant_id=T1, agent_id=agent, action_type=action,
                         target=target, effect=effect, enabled=True, expires_at=expires_at))
    h.seed(fn)
    return agent


def policy(h, sid, *, surface="mcp_input", target="submit", severity="deny", rules=None, enabled=True):
    pid = _id("pol")

    def fn(s):
        p = ArgumentPolicy(id=pid, tenant_id=T1, name=pid, surface=surface, target=target,
                           argument_key="*", analyzer="json", rules=rules or [], enabled=enabled,
                           severity=severity)
        s.add(p)
        s.flush()
        s.get(ResourceGroup, sid).argument_policies.append(p)
    h.seed(fn)
    return pid


def capability(h, sid, cfg, name, access, *, timeout=None):
    h.seed(lambda s: s.execute(resource_group_mcp_capabilities.insert().values(
        id=_id("cap"), tenant_id=T1, resource_group_id=sid, server_config_id=cfg,
        capability_type="tool", capability_name=name, access=access,
        approval_timeout_seconds=timeout)))


def gates(h, sid):
    body = composition(h, sid)
    CompositionResponse.model_validate(body)
    return body["human_approval"]["gates"]


def closure_of(h, sid):
    out = {}

    def fn(s):
        out["closure"], out["hash"] = resolve_and_hash(s, s.get(ResourceGroup, sid))
    h.seed(fn)
    return out["closure"], out["hash"]


class TestWhatIsAGate:
    def test_a_system_with_no_gate_declares_nothing(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "search")
        body = composition(h, sid)
        assert body["human_approval"] == {
            "autonomy_requires_approval": False,
            "approval_window_seconds": None,
            "gates": [],
        }

    def test_an_mcp_grant_is_placed_on_the_server_that_offers_the_tool(self, h):
        sid = system(h, _id("sys"))
        offers, other = bind_mcp(h, sid, "submit", "read"), bind_mcp(h, sid, "search")
        grant(h, sid, "mcp_tool", "submit", "approval_required")
        [g] = gates(h, sid)
        assert g["source"] == "agent_grant"
        assert g["scope"] == "always"
        assert g["lane"] == "mcp"
        assert g["resource_ids"] == [offers]
        assert other not in g["resource_ids"]

    def test_a_glob_reaches_every_server_with_a_match(self, h):
        sid = system(h, _id("sys"))
        a, b = bind_mcp(h, sid, "write_a"), bind_mcp(h, sid, "write_b")
        c = bind_mcp(h, sid, "read_c")
        grant(h, sid, "mcp_tool", "write_*", "approval_required")
        assert gates(h, sid)[0]["resource_ids"] == sorted([a, b])
        assert c not in gates(h, sid)[0]["resource_ids"]

    def test_a_tool_no_server_reports_is_kept_rather_than_dropped(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "search")
        grant(h, sid, "mcp_tool", "ghost_tool", "approval_required")
        [g] = gates(h, sid)
        assert g["target"] == "ghost_tool"
        assert g["resource_ids"] == []

    def test_an_allow_or_deny_grant_is_no_gate(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "submit")
        grant(h, sid, "mcp_tool", "submit", "allow")
        grant(h, sid, "mcp_tool", "submit", "deny")
        assert gates(h, sid) == []

    def test_an_llm_grant_is_no_gate_because_inference_refuses_instead_of_holding(self, h):
        # llm_preflight: `outcome.decision != Allow` is a refusal. Drawing a
        # person in the loop here would claim a control that does not exist.
        sid = system(h, _id("sys"))
        grant(h, sid, "llm_call", "*", "approval_required")
        assert gates(h, sid) == []

    def test_an_expired_grant_is_a_contract_term_but_not_a_gate(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "submit")
        grant(h, sid, "mcp_tool", "submit", "approval_required",
              expires_at=_now() - timedelta(minutes=1))
        assert gates(h, sid) == []

    def test_an_unexpired_grant_is_a_gate(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "submit")
        grant(h, sid, "mcp_tool", "submit", "approval_required",
              expires_at=_now() + timedelta(days=1))
        assert len(gates(h, sid)) == 1

    def test_an_a2a_gate_is_placed_on_the_delegate(self, h):
        sid, svc = system(h, _id("sys")), system(h, _id("svc"))
        cid = card(h, _id("card"))
        edge(h, sid, depends_on_system_id=svc, agent_card_id=cid, role="shared_service",
             criticality="critical", contributes_to_decision=True)
        grant(h, sid, "a2a_call", cid, "approval_required")
        [g] = gates(h, sid)
        assert g["lane"] == "a2a"
        assert g["resource_ids"] == [cid]


class TestConditionalGates:
    def test_a_policy_band_is_conditional_and_kept_apart_from_an_always_gate(self, h):
        sid = system(h, _id("sys"))
        cfg = bind_mcp(h, sid, "submit")
        policy(h, sid, rules=[{"severity": "approval_required", "field_gt": ["amount", 100]}])
        grant(h, sid, "mcp_tool", "submit", "approval_required")
        found = {g["source"]: g for g in gates(h, sid)}
        assert found["argument_policy"]["scope"] == "conditional"
        assert found["agent_grant"]["scope"] == "always"
        assert found["argument_policy"]["resource_ids"] == [cfg]

    def test_a_whole_policy_severity_counts_too(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "submit")
        policy(h, sid, severity="approval_required")
        assert [g["scope"] for g in gates(h, sid)] == ["conditional"]

    def test_a_policy_that_only_denies_is_no_gate(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "submit")
        policy(h, sid, rules=[{"severity": "deny"}])
        assert gates(h, sid) == []

    def test_a_disabled_policy_is_no_gate(self, h):
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "submit")
        policy(h, sid, severity="approval_required", enabled=False)
        assert gates(h, sid) == []

    def test_the_inference_surface_fails_safe_to_deny_so_it_is_no_gate(self, h):
        # llm_preflight: an approval band on llm_output_tool_calls "fails SAFE
        # to the deny path": there is no person to wait for on that surface.
        sid = system(h, _id("sys"))
        bind_mcp(h, sid, "submit")
        policy(h, sid, surface="llm_output_tool_calls", severity="approval_required")
        assert gates(h, sid) == []

    def test_a_policy_inherited_from_a_parent_applies(self, h):
        parent = system(h, _id("parent"))
        sid = system(h, _id("sys"), parent=parent)
        bind_mcp(h, sid, "submit")
        policy(h, parent, severity="approval_required")
        assert [g["source"] for g in gates(h, sid)] == ["argument_policy"]


class TestCapabilityGates:
    def test_a_capability_gate_is_on_its_server(self, h):
        sid = system(h, _id("sys"))
        cfg = bind_mcp(h, sid, "submit")
        capability(h, sid, cfg, "submit", "approval_required", timeout=600)
        [g] = gates(h, sid)
        assert (g["source"], g["scope"], g["resource_ids"]) == ("mcp_capability", "always", [cfg])

    def test_strictest_wins_up_the_chain(self, h):
        # A parent's `disabled` beats the child's `approval_required`: the core
        # refuses that call, it does not hold it.
        parent = system(h, _id("parent"))
        sid = system(h, _id("sys"), parent=parent)
        cfg = bind_mcp(h, sid, "submit")
        capability(h, sid, cfg, "submit", "approval_required")
        capability(h, parent, cfg, "submit", "disabled")
        assert gates(h, sid) == []

    def test_a_parents_gate_binds_the_child(self, h):
        parent = system(h, _id("parent"))
        sid = system(h, _id("sys"), parent=parent)
        cfg = bind_mcp(h, sid, "submit")
        capability(h, sid, cfg, "submit", "enabled")
        capability(h, parent, cfg, "submit", "approval_required")
        assert [g["source"] for g in gates(h, sid)] == ["mcp_capability"]


class TestSkillGates:
    def test_the_three_ways_a_skill_waits_for_a_person(self, h):
        # `determine_approval_required || per_skill_mode == approval_required`.
        sid = system(h, _id("sys"))
        by_default = bind_skill(h, sid, metadata={"approval_required": True})
        by_binding = bind_skill(h, sid, governance={"execution_mode": "approval_required"})
        free = bind_skill(h, sid)
        found = {g["source"]: g["resource_ids"] for g in gates(h, sid)}
        assert found["skill_default"] == [by_default]
        assert found["skill_execution_mode"] == [by_binding]
        assert free not in found["skill_default"] + found["skill_execution_mode"]

    def test_the_group_mode_gates_every_skill(self, h):
        sid = system(h, _id("sys"))
        a, b = bind_skill(h, sid), bind_skill(h, sid)
        h.seed(lambda s: setattr(s.get(ResourceGroup, sid), "skill_global_governance",
                                 {"execution_mode": "approval_required"}))
        [g] = gates(h, sid)
        assert g["resource_ids"] == sorted([a, b])

    def test_a_skill_grant_is_placed_on_the_skill(self, h):
        sid = system(h, _id("sys"))
        skill = bind_skill(h, sid)
        grant(h, sid, "skill_exec", skill, "approval_required")
        assert gates(h, sid)[0]["resource_ids"] == [skill]


class TestSystemWide:
    def test_autonomy_and_the_window_are_stated(self, h):
        sid = system(h, _id("sys"))

        def fn(s):
            g = s.get(ResourceGroup, sid)
            g.autonomy_level = "approval_required"
            g.approval_window_seconds = 900
        h.seed(fn)
        body = composition(h, sid)["human_approval"]
        assert body["autonomy_requires_approval"] is True
        assert body["approval_window_seconds"] == 900

    @pytest.mark.parametrize("level", ["autonomous", "restricted", "suspended"])
    def test_other_autonomy_levels_do_not_wait_for_a_person(self, h, level):
        sid = system(h, _id("sys"))
        h.seed(lambda s: setattr(s.get(ResourceGroup, sid), "autonomy_level", level))
        assert composition(h, sid)["human_approval"]["autonomy_requires_approval"] is False


class TestTheContractCoversTheGates:
    def test_a_system_without_gates_keeps_its_canonical_json(self, h):
        # Conditional keys: nothing may drift the fleet on upgrade day.
        sid = system(h, _id("sys"))
        cfg = bind_mcp(h, sid, "submit")
        bind_skill(h, sid)
        capability(h, sid, cfg, "submit", "enabled")
        closure, _ = closure_of(h, sid)
        assert "mcp_capability_gates" not in closure
        assert all("approval_required" not in s for s in closure["composition"]["skills"])

    def test_relaxing_a_capability_gate_changes_the_hash(self, h):
        # The bug this closes: removing a human from the loop drifted nothing.
        sid = system(h, _id("sys"))
        cfg = bind_mcp(h, sid, "submit")
        capability(h, sid, cfg, "submit", "approval_required", timeout=600)
        before, before_hash = closure_of(h, sid)
        assert before["mcp_capability_gates"] == [{
            "server_config_id": cfg, "capability_type": "tool", "capability_name": "submit",
            "approval_timeout_seconds": 600,
        }]
        h.seed(lambda s: s.execute(
            resource_group_mcp_capabilities.update()
            .where(resource_group_mcp_capabilities.c.resource_group_id == sid)
            .values(access="enabled")))
        after, after_hash = closure_of(h, sid)
        assert after_hash != before_hash

        diff = diff_closures(before, after)
        assert [w["kind"] for w in diff["widened"]] == ["mcp_capability_gate"]
        assert "no longer needs human approval" in diff["widened"][0]["detail"]

    def test_adding_a_capability_gate_is_a_narrowing(self):
        gate = {"server_config_id": "c", "capability_type": "tool", "capability_name": "t"}
        diff = diff_closures({}, {"mcp_capability_gates": [gate]})
        assert [n["kind"] for n in diff["narrowed"]] == ["mcp_capability_gate"]
        assert diff["widened"] == []

    def test_dropping_a_skills_approval_default_is_a_widening(self):
        def closure(flag):
            skill = {"id": "s", "name": "S", **({"approval_required": True} if flag else {})}
            return {"composition": {"skills": [skill]}}

        widened = diff_closures(closure(True), closure(False))["widened"]
        assert any(w["kind"] == "skill_approval_default" for w in widened)
        narrowed = diff_closures(closure(False), closure(True))["narrowed"]
        assert any(n["kind"] == "skill_approval_default" for n in narrowed)

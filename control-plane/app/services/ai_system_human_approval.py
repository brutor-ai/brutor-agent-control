# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Where this AI System is DECLARED to wait for a person

The Composition diagram showed holds only after they happened (`held` on a
connection line, ⏸ on a run overlay), so a system with an approval gate that
had not fired yet looked fully autonomous. This derives the declared gates
from the closure, the same contract text an approver signed; the Oversight
signal (`ai_system_oversight`) scores whether the holds that DID happen worked.

A gate here is something the core actually holds a call for. Three look like
gates and are not, and must never be drawn as a person in the loop:

  * an `llm_call` grant with effect `approval_required`: approval is for
    actions, and the inference surface refuses the call instead;
  * an argument policy band on `llm_output_tool_calls`: fails safe to a deny,
    for the same reason;
  * a grant past its `expires_at`: the closure lists it as a contract term,
    but it is no longer in force.

`always` and `conditional` are kept apart: a gate that fires only when the
arguments match a rule is a different claim from one that always fires.

No gate does not mean "no oversight": nothing here reads that as a fault,
because it may be the owner's deliberate choice. That is a contract question.
"""

from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

SCOPE_ALWAYS = "always"
SCOPE_CONDITIONAL = "conditional"

LANE_MCP = "mcp"
LANE_A2A = "a2a"
LANE_SKILLS = "skills"

SEVERITY_APPROVAL = "approval_required"

#: Grant action type → lane. `llm_call` is absent on purpose (see the module
#: docstring).
_GRANT_LANES = {"mcp_tool": LANE_MCP, "a2a_call": LANE_A2A, "skill_exec": LANE_SKILLS}
#: Argument policy surface → lane. `llm_output_tool_calls` absent on purpose.
_POLICY_LANES = {"mcp_input": LANE_MCP, "skill_input": LANE_SKILLS}


def _gate(source: str, scope: str, lane: str, target: str, resource_ids: Iterable[str],
          detail: str) -> Dict[str, Any]:
    return {
        "source": source,
        "scope": scope,
        "lane": lane,
        "target": target,
        "resource_ids": sorted(set(resource_ids)),
        "detail": detail,
    }


def _in_force(grant: Dict[str, Any], now: datetime) -> bool:
    raw = grant.get("expires_at")
    if not raw:
        return True
    expires = datetime.fromisoformat(raw)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > now


def _has_approval_band(policy: Any) -> bool:
    if policy.severity == SEVERITY_APPROVAL:
        return True
    rules = policy.rules if isinstance(policy.rules, list) else []
    return any(isinstance(r, dict) and r.get("severity") == SEVERITY_APPROVAL for r in rules)


def _server_tools(db: Session, tenant_id: str, mcp_resources: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """`{server_config id: tool names}` from `mcp_servers.capabilities`, the
    runtime's own authority on what a server offers."""
    from app.database.models.mcp_server import MCPServer

    server_ids = {r.get("mcp_server_id") for r in mcp_resources if r.get("mcp_server_id")}
    if not server_ids:
        return {}
    caps = dict(
        db.query(MCPServer.id, MCPServer.capabilities)
        .filter(MCPServer.tenant_id == tenant_id, MCPServer.id.in_(server_ids))
        .all()
    )
    out: Dict[str, List[str]] = {}
    for r in mcp_resources:
        capabilities = caps.get(r.get("mcp_server_id"))
        tools = capabilities.get("tools") if isinstance(capabilities, dict) else None
        out[r["id"]] = [
            t["name"] for t in (tools or []) if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]
    return out


def declared_human_approval(
    db: Session,
    tenant_id: str,
    closure: Dict[str, Any],
    resources: Dict[str, List[Dict[str, Any]]],
    dependencies: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The declared gates, placed on the resources the diagram draws.

    `resource_ids` empty means the gate is real but cannot be placed on one
    resource (a tool name no bound server reports): it stays on the record and
    the view shows it on the lane. It is never dropped, because a gate the
    diagram cannot place is still a gate.
    """
    from app.database.models.argument_policy import ArgumentPolicy

    now = now or datetime.now(timezone.utc)
    mcp = resources.get("mcp_servers") or []
    skills = resources.get("skills") or []
    tools = _server_tools(db, tenant_id, mcp)
    cards = sorted({d["agent_card_id"] for d in dependencies if d.get("agent_card_id")})

    def mcp_matches(pattern: str) -> List[str]:
        return [
            r["id"] for r in mcp
            if pattern == "*" or any(fnmatchcase(t, pattern) for t in tools.get(r["id"], []))
        ]

    def skill_matches(pattern: str) -> List[str]:
        return [
            r["id"] for r in skills
            if pattern == "*" or fnmatchcase(str(r["id"]), pattern)
            or fnmatchcase(str(r.get("name") or ""), pattern)
        ]

    def placed(lane: str, pattern: str) -> List[str]:
        if lane == LANE_MCP:
            return mcp_matches(pattern)
        if lane == LANE_SKILLS:
            return skill_matches(pattern)
        return [c for c in cards if pattern == "*" or fnmatchcase(c, pattern)]

    gates: List[Dict[str, Any]] = []
    for grant in closure.get("agent_grants") or []:
        lane = _GRANT_LANES.get(grant.get("action_type"))
        if lane is None or grant.get("effect") != SEVERITY_APPROVAL or not _in_force(grant, now):
            continue
        target = grant.get("target") or "*"
        who = grant.get("agent_name") or grant.get("agent_id")
        gates.append(_gate(
            "agent_grant", SCOPE_ALWAYS, lane, target, placed(lane, target),
            f"{who} needs approval for {target}",
        ))

    for gate in closure.get("mcp_capability_gates") or []:
        name = gate["capability_name"]
        gates.append(_gate(
            "mcp_capability", SCOPE_ALWAYS, LANE_MCP, name, [gate["server_config_id"]],
            f"{gate['capability_type']} {name} needs approval",
        ))

    policy_ids = [
        p["id"]
        for p in (closure.get("composition") or {}).get("argument_policies") or []
        if p.get("enabled", True)
    ]
    if policy_ids:
        policies = (
            db.query(ArgumentPolicy)
            .filter(ArgumentPolicy.tenant_id == tenant_id, ArgumentPolicy.id.in_(policy_ids))
            .order_by(ArgumentPolicy.id)
            .all()
        )
        for policy in policies:
            lane = _POLICY_LANES.get(policy.surface)
            if lane is None or not policy.enabled or not _has_approval_band(policy):
                continue
            gates.append(_gate(
                "argument_policy", SCOPE_CONDITIONAL, lane, policy.target,
                placed(lane, policy.target),
                f"{policy.name}: needs approval when the arguments match",
            ))

    skill_group_mode = ((closure.get("governance") or {}).get("skill") or {}).get("execution_mode")
    if skill_group_mode == SEVERITY_APPROVAL:
        gates.append(_gate(
            "skill_execution_mode", SCOPE_ALWAYS, LANE_SKILLS, "*", placed(LANE_SKILLS, "*"),
            "Every skill execution needs approval",
        ))
    for skill in (closure.get("composition") or {}).get("skills") or []:
        name = skill.get("name") or skill.get("id")
        if (skill.get("governance") or {}).get("execution_mode") == SEVERITY_APPROVAL:
            gates.append(_gate(
                "skill_execution_mode", SCOPE_ALWAYS, LANE_SKILLS, str(skill.get("id")),
                [skill.get("id")], f"Executing {name} needs approval",
            ))
        if skill.get("approval_required"):
            gates.append(_gate(
                "skill_default", SCOPE_ALWAYS, LANE_SKILLS, str(skill.get("id")),
                [skill.get("id")], f"{name} needs approval by default",
            ))

    gates.sort(key=lambda g: (g["lane"], g["scope"], g["source"], g["target"], g["detail"]))
    return {
        # Side-effecting calls by agents wait for a person (`autonomy::decide`);
        # read-only calls and non-agent traffic do not.
        "autonomy_requires_approval": closure.get("autonomy_level") == SEVERITY_APPROVAL,
        "approval_window_seconds": closure.get("approval_window_seconds"),
        "gates": gates,
    }

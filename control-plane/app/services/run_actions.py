# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# A run's actions — THE projection (leaf actions, run segments)

`GET /v1/admin/runs/{run_id}` serves `RunDetail.actions` from this module
alone. Three console surfaces read it: the Runs tab's action timeline, Run
details, and the Composition tab's run overlay (numbered badges on the
Simple graph). Every fact they show about an action — its number, its
outcome, what it reached, which AI System took it and which delegation it
ran inside — is computed HERE, once, so the overlay and Run details can
never disagree about a run.

## What each action carries (metadata only)

Identifiers, names, outcome and timing. Never a request or response body,
never a tool argument, never a prompt. The one value read from a stored
body — a skill run's script — is emitted only when it names a script the
skill's own catalog registers (`agent_skill_scripts.filename`), so what
reaches the console is a catalog identifier, never argument content.

## Order and numbering

The gateway's order: `(timestamp, id)`, the order the trajectory rollup and
the run's tool signature use. An action whose `parent_task_id` is the
`current_task_id` of an A2A call in the run ran INSIDE that delegation —
the same link the run ledger cuts segments by (core
`run_segments::CALLS_SQL`: several rows can share one task id, and the LAST
is what the caller received, so that row is the entering call). Such an
action is numbered under its entering call — `5a`, `5b` under `5`; a
delegation inside a delegation continues `5a.1`. Every other action is
top-level and numbered 1, 2, 3 … in order. An A2A call is logged when it
returns, so the delegate's actions carry EARLIER timestamps than the call
they ran inside; nesting by the task link (never by position) is what
keeps them under it.

## Outcome — ONE classification

`ai_system_connection_status.blocked_expr` / `error_expr` / `held_expr`,
the predicates the Composition tab's connector lines count with:

  * ``held``     — held for a human approval decision;
  * ``blocked``  — the gateway refused it (guardrail, argument policy,
                   authz deny, a refusal subtype, a limit's 429);
  * ``error``    — not blocked, and unsuccessful or HTTP >= 400;
  * ``answered`` — anything else.

## Target — ONE mapping

`ai_system_connection_status.connection_keys`, the row → connector mapping
the lines use. The primary target is the skill for a skill run (the Agent
Skills server is gateway plumbing), else the model / MCP server / agent
card; knowledge bases a model call retrieved from are listed beside it.

## Which system took it

THE own-rows rule (`ai_system_conformance.own_rows`): an action belongs to
the run's root unless its resource group resolves to a DIFFERENT AI System
(`ai_system_composition.nearest_ai_system_map`, the ledger's attribution).
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import case, func, literal_column
from sqlalchemy.orm import Session

from app.services.ai_system_connection_status import (
    KIND_A2A,
    KIND_KB,
    KIND_MCP,
    KIND_MODEL,
    KIND_SKILL,
    blocked_expr,
    connection_keys,
    error_expr,
    held_expr,
)
from app.services.trajectory_search import OPENS_TURN_SQL

#: The most actions one run detail carries. `action_count` on the run is
#: authoritative for totals; the console says when the list is capped.
ACTION_CAP = 500

OUTCOME_ANSWERED = "answered"
OUTCOME_ERROR = "error"
OUTCOME_BLOCKED = "blocked"
OUTCOME_HELD = "held"
ACTION_OUTCOMES = (OUTCOME_ANSWERED, OUTCOME_ERROR, OUTCOME_BLOCKED, OUTCOME_HELD)

#: Target kinds on the wire — the connection kinds, one name each.
TARGET_KINDS = (KIND_MODEL, KIND_MCP, KIND_SKILL, KIND_A2A, KIND_KB)

#: Nesting deeper than this is numbered top-level: a guard against a task
#: link that loops, never a limit a real delegation chain reaches.
MAX_NESTING = 16

_SCRIPT_RE = re.compile(r'"script"\s*:\s*"([^"\\]{1,255})"')


def _letters(n: int) -> str:
    """1 → a, 26 → z, 27 → aa (bijective base 26)."""
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("a") + r) + out
    return out


def sub_number(parent: str, index: int) -> str:
    """The number of the `index`-th (1-based) action inside the delegation
    numbered `parent`: `5` → `5a`, `5a` → `5a.1`, `5a.1` → `5a.1.1`."""
    return f"{parent}{_letters(index)}" if parent.isdigit() else f"{parent}.{index}"


def classify_outcome(held: bool, blocked: bool, error: bool) -> str:
    if held:
        return OUTCOME_HELD
    if blocked:
        return OUTCOME_BLOCKED
    if error:
        return OUTCOME_ERROR
    return OUTCOME_ANSWERED


def primary_target(keys: Iterable[Tuple[str, str]]) -> Tuple[Optional[str], Optional[str], List[str]]:
    """`(kind, id, kb_ids)`: the skill for a skill run, else the first
    model / MCP / A2A key; knowledge bases are listed beside it."""
    keys = list(keys)
    kbs = [k for kind, k in keys if kind == KIND_KB]
    for want in (KIND_SKILL, KIND_MODEL, KIND_MCP, KIND_A2A):
        for kind, k in keys:
            if kind == want:
                return kind, k, kbs
    if kbs:
        return KIND_KB, kbs[0], kbs
    return None, None, []


def number_actions(rows: List[Dict[str, Any]]) -> None:
    """Set `number`, `entered_by` and `nesting` on every row (in place).

    `rows` are in the gateway's order and carry `id`, `proxy_type`,
    `current_task_id` and `parent_task_id`.
    """
    # The entering call per task id: the LAST a2a row carrying it.
    call_by_task: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r["proxy_type"] == "a2a" and r.get("current_task_id"):
            call_by_task[r["current_task_id"]] = r

    parent_of: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        call = call_by_task.get(r.get("parent_task_id") or "")
        if call is not None and call["id"] != r["id"]:
            parent_of[r["id"]] = call

    # A link that loops (or runs absurdly deep) is not a delegation chain:
    # its rows are numbered top-level rather than never.
    def depth(r: Dict[str, Any]) -> Optional[int]:
        seen: Set[str] = {r["id"]}
        d, cur = 0, r
        while cur["id"] in parent_of:
            cur = parent_of[cur["id"]]
            if cur["id"] in seen or d >= MAX_NESTING:
                return None
            seen.add(cur["id"])
            d += 1
        return d

    for r in rows:
        if depth(r) is None:
            parent_of.pop(r["id"], None)

    children: Dict[str, List[Dict[str, Any]]] = {}
    top: List[Dict[str, Any]] = []
    for r in rows:
        parent = parent_of.get(r["id"])
        if parent is None:
            top.append(r)
        else:
            children.setdefault(parent["id"], []).append(r)

    def assign(r: Dict[str, Any], number: str, entered_by: Optional[str], nesting: int) -> None:
        r["number"], r["entered_by"], r["nesting"] = number, entered_by, nesting
        for i, child in enumerate(children.get(r["id"], []), start=1):
            assign(child, sub_number(number, i), number, nesting + 1)

    for i, r in enumerate(top, start=1):
        assign(r, str(i), None, 0)


def _script_from_body(body: Optional[str]) -> Optional[str]:
    if not body:
        return None
    try:
        doc = json.loads(body)
        args = ((doc.get("params") or {}).get("arguments") or {}) if isinstance(doc, dict) else {}
        value = args.get("script") if isinstance(args, dict) else None
        if isinstance(value, str) and value:
            return value
    except (ValueError, AttributeError):
        pass
    # A stored preview can be cut mid-document.
    m = _SCRIPT_RE.search(body)
    return m.group(1) if m else None


def _catalog_script(named: Optional[str], registered: Set[str]) -> Optional[str]:
    """The registered filename `named` refers to, or None: only a catalog
    identifier ever leaves this module."""
    if not named:
        return None
    for filename in sorted(registered):
        if named in (filename, posixpath.basename(filename)) or filename == f"scripts/{named}":
            return filename
    return None


def project_run_actions(db: Session, tenant_id: str, run: Any) -> List[Dict[str, Any]]:
    """The run's actions, in the gateway's order, as `RunAction` dicts."""
    from app.database.models.agent_card import AgentCard
    from app.database.models.agent_skill import AgentSkill
    from app.database.models.agent_skill_script import AgentSkillScript
    from app.database.models.ai_system_dependency import AISystemDependency
    from app.database.models.llm_model import LLModel
    from app.database.models.mcp_server import MCPServer
    from app.database.models.proxy_log import ProxyLog
    from app.database.models.resource_group import ResourceGroup
    from app.services.ai_system_composition import nearest_ai_system_map

    blocked = blocked_expr()
    order = (ProxyLog.timestamp.asc(), ProxyLog.id.asc())
    rows = (
        db.query(
            ProxyLog.id,
            ProxyLog.timestamp,
            ProxyLog.proxy_type,
            ProxyLog.proxy_subtype,
            ProxyLog.mcp_capability_name,
            ProxyLog.llm_model_used,
            ProxyLog.http_status_code,
            ProxyLog.success,
            ProxyLog.duration_ms,
            ProxyLog.llm_estimated_cost_usd,
            ProxyLog.llm_total_tokens,
            ProxyLog.trace_continuity,
            ProxyLog.delegation_depth,
            ProxyLog.guardrail_blocked,
            ProxyLog.error_message,
            ProxyLog.correlation_id,
            ProxyLog.step_id,
            ProxyLog.step_name,
            ProxyLog.turn_id,
            ProxyLog.turn_seq,
            ProxyLog.server_id,
            ProxyLog.skill_id,
            ProxyLog.rag_collection_ids,
            ProxyLog.resource_group_id,
            ProxyLog.current_task_id,
            ProxyLog.parent_task_id,
            ProxyLog.a2a_task_state,
            case((held_expr(), True), else_=False).label("is_held"),
            case((blocked, True), else_=False).label("is_blocked"),
            case((error_expr(blocked), True), else_=False).label("is_error"),
            # THE turn inference, over the WHOLE run (the window is computed
            # before the cap applies).
            func.sum(literal_column(OPENS_TURN_SQL)).over(order_by=order).label("inferred_turn"),
        )
        .filter(ProxyLog.tenant_id == tenant_id, ProxyLog.root_task_id == run.root_task_id)
        .order_by(*order)
        .limit(ACTION_CAP)
        .all()
    )
    if not rows:
        return []

    system_of = nearest_ai_system_map(db, tenant_id)
    root_system = run.ai_system_id

    out: List[Dict[str, Any]] = []
    for r in rows:
        keys = connection_keys(
            r.proxy_type, r.proxy_subtype, r.server_id, r.skill_id,
            r.rag_collection_ids, r.mcp_capability_name,
        )
        kind, target_id, kb_ids = primary_target(keys)
        system_id = system_of.get(r.resource_group_id) if r.resource_group_id else None
        out.append({
            "id": r.id,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "proxy_type": r.proxy_type,
            "proxy_subtype": r.proxy_subtype,
            "capability": r.mcp_capability_name,
            "model": r.llm_model_used,
            "status_code": r.http_status_code,
            "success": r.success,
            "duration_ms": r.duration_ms,
            "cost_usd": r.llm_estimated_cost_usd,
            "total_tokens": r.llm_total_tokens,
            "trace_continuity": r.trace_continuity,
            "delegation_depth": r.delegation_depth,
            "guardrail_blocked": r.guardrail_blocked,
            "error_message": (r.error_message or None) and r.error_message[:200],
            "correlation_id": r.correlation_id,
            "step_id": r.step_id,
            "step_name": r.step_name,
            "turn_id": r.turn_id,
            "turn_seq": r.turn_seq,
            "inferred_turn": int(r.inferred_turn) if r.inferred_turn is not None else None,
            "outcome": classify_outcome(bool(r.is_held), bool(r.is_blocked), bool(r.is_error)),
            "target_kind": kind,
            "target_id": target_id,
            "target_name": None,
            "kb_ids": kb_ids,
            "skill_script": None,
            "system_id": system_id,
            "system_name": None,
            # THE own-rows rule: the root's unless another AI System took it.
            "own": system_id is None or system_id == root_system,
            "delegate_system_id": None,
            "delegate_system_name": None,
            "current_task_id": r.current_task_id,
            "parent_task_id": r.parent_task_id,
            "a2a_task_state": r.a2a_task_state,
        })

    number_actions(out)

    # The system each A2A call entered: the nearest AI System of the steps
    # that ran under it (other than the caller's own), else the caller's
    # DECLARED edge on the card — the ledger's segment attribution.
    entered: Dict[str, List[Dict[str, Any]]] = {}
    number_to_row = {a["number"]: a for a in out}
    for a in out:
        if a["entered_by"] is not None:
            entered.setdefault(number_to_row[a["entered_by"]]["id"], []).append(a)
    card_edges: Dict[Tuple[str, str], str] = {}
    cards = {a["target_id"] for a in out if a["target_kind"] == KIND_A2A and a["target_id"]}
    callers = {a["system_id"] or root_system for a in out if a["target_kind"] == KIND_A2A}
    if cards and callers:
        for sid, card, target in db.query(
            AISystemDependency.ai_system_id,
            AISystemDependency.agent_card_id,
            AISystemDependency.depends_on_system_id,
        ).filter(
            AISystemDependency.tenant_id == tenant_id,
            AISystemDependency.agent_card_id.in_(cards),
            AISystemDependency.ai_system_id.in_({c for c in callers if c}),
        ):
            if target:
                card_edges[(sid, card)] = target
    for a in out:
        if a["proxy_type"] != "a2a":
            continue
        caller = a["system_id"] or root_system
        delegate = next(
            (c["system_id"] for c in entered.get(a["id"], [])
             if c["system_id"] and c["system_id"] != caller),
            None,
        )
        if delegate is None and a["target_kind"] == KIND_A2A:
            delegate = card_edges.get((caller, a["target_id"]))
        a["delegate_system_id"] = delegate

    # Names — one query per kind, over the distinct ids this run touched.
    def ids(kind: str) -> Set[str]:
        return {a["target_id"] for a in out if a["target_kind"] == kind and a["target_id"]}

    names: Dict[Tuple[str, str], str] = {}
    for kind, model, col in (
        (KIND_MODEL, LLModel, LLModel.name),
        (KIND_MCP, MCPServer, MCPServer.name),
        (KIND_SKILL, AgentSkill, AgentSkill.name),
        (KIND_A2A, AgentCard, AgentCard.name),
    ):
        wanted = ids(kind)
        if wanted:
            for rid, name in db.query(model.id, col).filter(model.id.in_(wanted)):
                if name:
                    names[(kind, rid)] = name
    systems = {a["system_id"] for a in out if a["system_id"]} | {
        a["delegate_system_id"] for a in out if a["delegate_system_id"]
    }
    system_names: Dict[str, str] = {}
    if systems:
        for gid, display, name in db.query(
            ResourceGroup.id, ResourceGroup.display_name, ResourceGroup.name
        ).filter(ResourceGroup.tenant_id == tenant_id, ResourceGroup.id.in_(systems)):
            system_names[gid] = display or name

    # A skill run's script: only a filename the skill's catalog registers.
    skill_rows = [a for a in out if a["target_kind"] == KIND_SKILL and a["target_id"]]
    scripts: Dict[str, Set[str]] = {}
    bodies: Dict[str, Optional[str]] = {}
    if skill_rows:
        for sid, filename in db.query(AgentSkillScript.skill_id, AgentSkillScript.filename).filter(
            AgentSkillScript.skill_id.in_({a["target_id"] for a in skill_rows})
        ):
            scripts.setdefault(sid, set()).add(filename)
        with_scripts = [a["id"] for a in skill_rows if scripts.get(a["target_id"])]
        if with_scripts:
            bodies = dict(
                db.query(ProxyLog.id, ProxyLog.request_body_preview).filter(
                    ProxyLog.tenant_id == tenant_id, ProxyLog.id.in_(with_scripts)
                )
            )

    for a in out:
        if a["target_kind"] and a["target_kind"] != KIND_KB:
            a["target_name"] = names.get((a["target_kind"], a["target_id"]))
        a["system_name"] = system_names.get(a["system_id"]) if a["system_id"] else None
        if a["delegate_system_id"]:
            a["delegate_system_name"] = system_names.get(a["delegate_system_id"])
        if a["target_kind"] == KIND_SKILL:
            a["skill_script"] = _catalog_script(
                _script_from_body(bodies.get(a["id"])), scripts.get(a["target_id"], set())
            )
        # Task ids are the nesting's input, not something the console shows.
        a.pop("current_task_id", None)
        a.pop("parent_task_id", None)
    return out


__all__ = [
    "ACTION_CAP",
    "ACTION_OUTCOMES",
    "OUTCOME_ANSWERED",
    "OUTCOME_BLOCKED",
    "OUTCOME_ERROR",
    "OUTCOME_HELD",
    "TARGET_KINDS",
    "classify_outcome",
    "number_actions",
    "primary_target",
    "project_run_actions",
    "sub_number",
]

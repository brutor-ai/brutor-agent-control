# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Trajectory search

Answers questions about *what happened inside a run*, at the four levels the
platform now names consistently:

    Run  >  Step (phase)  >  Turn (loop pass)  >  Action (governed call)

## Why this is SQL and not a graph traversal

Every action row carries its **whole ancestry** — `root_task_id`, `step_id`,
`turn_id`, `turn_seq`, and a total order (`timestamp, id`). Because the lineage
is denormalised onto the leaf, reconstructing the hierarchy is a `GROUP BY`
and "what happened N actions later" is a window function. There is no
`parent_id` chain of arbitrary depth, so the recursive-CTE cost that pushes
observability products onto graph stores does not arise here: depth is fixed at
four and known at write time.

That is also what keeps the fleet-wide aggregations — cost per completed run,
spend by team — a single indexed scan, which is most of what this product does.

## Honesty rules carried from the ledger

* A run with no asserted steps is reported as **unphased**, never as one
  implicit phase. `step_id IS NULL` is a fact about what the client told us.
* Consequence search returns *co-occurrence ordered in time*. It does not
  claim causation, and the field names say so (`actions_apart`, not `caused`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

#: proxy_type values that count as a tool-ish action.
TOOL_TYPES = ("mcp", "virtual-mcp")

#: Hard ceiling on rows returned by any single search, so a pathological run
#: (a 400-action loop) cannot turn one operator click into a slow query.
MAX_ROWS = 2000


@dataclass
class TurnGroup:
    """One turn, with the actions that belong to it."""

    turn_id: Optional[str]
    turn_seq: Optional[int]
    #: Gateway-inferred ordinal, used when the client asserted no turn id.
    #: Reported alongside `turn_id` rather than substituted for it, so a
    #: reader can always tell which one they are looking at.
    inferred_turn: Optional[int]
    step_id: Optional[str]
    step_name: Optional[str]
    action_count: int = 0
    tool_action_count: int = 0
    opened_by_model: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    cost_usd: float = 0.0
    first_at: Optional[str] = None
    blocked: bool = False


#: THE turn-inference rule, as SQL over one proxy_logs row: a non-embedding
#: model call opens a pass of the loop (the run ledger's rollup counts
#: `turn_count` the same way). A running SUM of it in the run's order is the
#: row's inferred turn ordinal. The trajectory rollup below and the run's
#: action projection (`run_actions`) both read THIS string, so the Trajectory
#: tab, Run details and the Composition run overlay cannot number turns
#: differently.
OPENS_TURN_SQL = (
    "CASE WHEN proxy_type = 'llm' "
    "AND COALESCE(proxy_subtype, '') <> 'embeddings' THEN 1 ELSE 0 END"
)


# ── 1. Step → Turn → Action rollup ──────────────────────────────────────
_STEP_TURNS_SQL = text(
    """
    WITH ordered AS (
      SELECT *,
             -- Infer a turn ordinal the SAME way run_ledger's rollup does
             -- (OPENS_TURN_SQL). Without this the grouping would lump every
             -- action of an unasserted run into one NULL turn while
             -- `turn_count` reported the inferred number — two surfaces
             -- disagreeing about the same run.
             SUM(__OPENS_TURN__)
               OVER (ORDER BY "timestamp", id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                                                             AS inferred_turn
        FROM proxy_logs
       WHERE tenant_id = :tenant AND root_task_id = :run
    )
    SELECT a.turn_id,
           MIN(a.turn_seq)                                   AS turn_seq,
           MIN(a.inferred_turn)                              AS inferred_turn,
           MIN(a.step_id)                                    AS step_id,
           MAX(a.step_name)                                  AS step_name,
           COUNT(*)                                          AS action_count,
           COUNT(*) FILTER (WHERE a.proxy_type = ANY(:tool_types))
                                                             AS tool_action_count,
           MAX(a.llm_model_used) FILTER (WHERE a.proxy_type = 'llm')
                                                             AS opened_by_model,
           ARRAY_REMOVE(ARRAY_AGG(a.mcp_capability_name
                        ORDER BY a."timestamp", a.id), NULL)  AS tools,
           COALESCE(SUM(a.llm_estimated_cost_usd), 0)         AS cost_usd,
           MIN(a."timestamp")                                 AS first_at,
           BOOL_OR(a.guardrail_blocked OR a.argument_policy_denied)
                                                             AS blocked
      FROM ordered a
     WHERE (:step IS NULL OR a.step_id = :step)
     -- Group by the asserted id when there is one, else by the inferred
     -- ordinal. COALESCE on a text cast keeps both in one grouping key.
     GROUP BY COALESCE(a.turn_id, 'inferred:' || a.inferred_turn::text), a.turn_id
     ORDER BY MIN(a."timestamp"), MIN(a.id)
     LIMIT :limit
    """.replace("__OPENS_TURN__", OPENS_TURN_SQL)
)


def turns_in_run(
    db: Session,
    *,
    tenant_id: str,
    root_task_id: str,
    step_id: Optional[str] = None,
    limit: int = MAX_ROWS,
) -> List[TurnGroup]:
    """*"All tool calls from Step X inside Run Y, with their parent turns."*

    One indexed scan plus a grouping — see the module docstring. Passing
    `step_id=None` returns the whole run, grouped by turn.
    """
    rows = db.execute(
        _STEP_TURNS_SQL,
        {
            "tenant": tenant_id,
            "run": root_task_id,
            "step": step_id,
            "tool_types": list(TOOL_TYPES),
            "limit": min(limit, MAX_ROWS),
        },
    ).mappings()
    return [
        TurnGroup(
            turn_id=r["turn_id"],
            turn_seq=r["turn_seq"],
            inferred_turn=r["inferred_turn"],
            step_id=r["step_id"],
            step_name=r["step_name"],
            action_count=int(r["action_count"] or 0),
            tool_action_count=int(r["tool_action_count"] or 0),
            opened_by_model=r["opened_by_model"],
            tools=list(r["tools"] or []),
            cost_usd=float(r["cost_usd"] or 0.0),
            first_at=r["first_at"].isoformat() if r["first_at"] else None,
            blocked=bool(r["blocked"]),
        )
        for r in rows
    ]


# ── 2. Consequence search ───────────────────────────────────────────────
# "The turn where a guardrail tripped, and the tool actions that followed."
#
# `within` bounds how far ahead we look. Unbounded, a 400-action run yields a
# quadratic result set that tells an operator nothing they can read.
_CONSEQUENCE_SQL = text(
    """
    WITH traj AS (
      SELECT id, "timestamp", turn_id, turn_seq, step_id, step_name,
             proxy_type, mcp_capability_name, llm_model_used,
             guardrail_blocked, pii_detected, argument_policy_denied,
             guardrail_check, server_id,
             ROW_NUMBER() OVER (ORDER BY "timestamp", id) AS action_no
        FROM proxy_logs
       WHERE tenant_id = :tenant AND root_task_id = :run
    ),
    trips AS (
      SELECT * FROM traj
       WHERE guardrail_blocked
          OR pii_detected
          OR argument_policy_denied
    )
    SELECT t.action_no                       AS trip_action_no,
           t.turn_id                         AS trip_turn_id,
           t.step_id                         AS trip_step_id,
           t.step_name                       AS trip_step_name,
           t.guardrail_check                 AS trip_check,
           t.guardrail_blocked               AS trip_blocked,
           t.pii_detected                    AS trip_pii,
           t.argument_policy_denied          AS trip_policy_denied,
           t."timestamp"                     AS trip_at,
           l.action_no                       AS after_action_no,
           l.turn_id                         AS after_turn_id,
           l.step_id                         AS after_step_id,
           l.step_name                       AS after_step_name,
           l.proxy_type                      AS after_type,
           l.mcp_capability_name             AS after_capability,
           l.server_id                       AS after_server_id,
           l."timestamp"                     AS after_at,
           l.action_no - t.action_no         AS actions_apart
      FROM trips t
      JOIN traj  l
        ON l.action_no > t.action_no
       AND l.action_no <= t.action_no + :within
     WHERE l.proxy_type = ANY(:tool_types)
     ORDER BY t.action_no, l.action_no
     LIMIT :limit
    """
)


def consequences_of_trips(
    db: Session,
    *,
    tenant_id: str,
    root_task_id: str,
    within: int = 10,
    limit: int = MAX_ROWS,
) -> List[Dict[str, Any]]:
    """*"Find the turn where a check tripped, and what ran shortly after."*

    Returns co-occurrence **ordered in time**, not causation — hence
    `actions_apart` rather than anything implying a causal claim. An operator
    reads it as "these tool calls happened within N actions of a trip"; the
    judgement stays theirs.
    """
    rows = db.execute(
        _CONSEQUENCE_SQL,
        {
            "tenant": tenant_id,
            "run": root_task_id,
            "within": max(1, within),
            "tool_types": list(TOOL_TYPES),
            "limit": min(limit, MAX_ROWS),
        },
    ).mappings()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for k in ("trip_at", "after_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        out.append(d)
    return out


# ── 3. Cost by step ─────────────────────────────────────────────────────
_STEP_COST_SQL = text(
    """
    SELECT a.step_id,
           MAX(a.step_name)                                  AS step_name,
           COUNT(DISTINCT a.turn_id)                         AS turns,
           COUNT(*)                                          AS actions,
           COALESCE(SUM(a.llm_estimated_cost_usd), 0)        AS cost_usd,
           COALESCE(SUM(a.llm_total_tokens), 0)              AS tokens,
           MIN(a."timestamp")                                AS first_at
      FROM proxy_logs a
     WHERE a.tenant_id = :tenant
       AND a.root_task_id = :run
       AND a.step_id IS NOT NULL
     GROUP BY a.step_id
     ORDER BY MIN(a."timestamp")
    """
)


def cost_by_step(
    db: Session, *, tenant_id: str, root_task_id: str
) -> List[Dict[str, Any]]:
    """*"Which phase is burning the budget."*

    Empty when the client asserted no steps. That is reported as **unphased**
    by the caller rather than as a single synthetic phase — absence of an
    assertion is not evidence of one.
    """
    rows = db.execute(
        _STEP_COST_SQL, {"tenant": tenant_id, "run": root_task_id}
    ).mappings()
    out = []
    for r in rows:
        d = dict(r)
        d["cost_usd"] = float(d["cost_usd"] or 0.0)
        d["tokens"] = int(d["tokens"] or 0)
        if d.get("first_at") is not None:
            d["first_at"] = d["first_at"].isoformat()
        out.append(d)
    return out


# ── 4. Cross-run action search ──────────────────────────────────────────
# The fleet-level question the drill-down cannot answer: "where has this tool
# been called at all, and in which phase". Bounded by time and system so it
# stays an indexed scan.
_FIND_ACTIONS_SQL = """
    SELECT a.root_task_id,
           a.step_id, a.step_name, a.turn_id, a.turn_seq,
           a.proxy_type, a.mcp_capability_name, a.llm_model_used,
           a.success, a.guardrail_blocked, a.argument_policy_denied,
           a.duration_ms, a.llm_estimated_cost_usd, a."timestamp",
           a.correlation_id, r.ai_system_id, r.terminal_state
      FROM proxy_logs a
      LEFT JOIN ai_system_runs r
             ON r.tenant_id = a.tenant_id AND r.root_task_id = a.root_task_id
     WHERE a.tenant_id = :tenant
       AND a.root_task_id IS NOT NULL
       {filters}
     ORDER BY a."timestamp" DESC, a.id DESC
     LIMIT :limit
"""


def find_actions(
    db: Session,
    *,
    tenant_id: str,
    ai_system_id: Optional[str] = None,
    capability: Optional[str] = None,
    proxy_type: Optional[str] = None,
    step_name: Optional[str] = None,
    only_blocked: bool = False,
    since_hours: int = 168,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Fleet-wide action lookup, filtered — the entry point to a trajectory."""
    filters = ['AND a."timestamp" >= NOW() - MAKE_INTERVAL(hours => :since)']
    params: Dict[str, Any] = {
        "tenant": tenant_id,
        "since": max(1, since_hours),
        "limit": min(limit, MAX_ROWS),
    }
    if ai_system_id:
        filters.append("AND r.ai_system_id = :sys")
        params["sys"] = ai_system_id
    if capability:
        filters.append("AND a.mcp_capability_name = :cap")
        params["cap"] = capability
    if proxy_type:
        filters.append("AND a.proxy_type = :ptype")
        params["ptype"] = proxy_type
    if step_name:
        filters.append("AND a.step_name = :sname")
        params["sname"] = step_name
    if only_blocked:
        filters.append(
            "AND (a.guardrail_blocked OR a.argument_policy_denied OR a.pii_detected)"
        )

    sql = text(_FIND_ACTIONS_SQL.format(filters="\n       ".join(filters)))
    rows = db.execute(sql, params).mappings()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("timestamp") is not None:
            d["timestamp"] = d["timestamp"].isoformat()
        out.append(d)
    return out

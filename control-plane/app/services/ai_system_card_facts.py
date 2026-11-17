# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Gallery-card facts for the AI Systems fleet board

The fleet board's gallery card shows each system as its governed execution
path — agent → gateway → resources — plus the observability and evaluation
signals an operator checks first (traces, latency, cost, tokens, errors,
offline evals). This module computes all of it for one PAGE of systems in
a fixed number of grouped queries: the fleet board is exactly where an N+1
would be felt, so nothing here issues a query per system except the two
latency percentile probes, which are index-ordered single-row reads.

Honesty rules, enforced here rather than left to the UI:

  * Gateway counts are EFFECTIVE, not just what is bound on the system:
    guardrails and policies are additive down the hierarchy, so a parent's
    binding governs the child's traffic too. Limits merge restrictively,
    so a key set at two levels is ONE limit (counted by path).
  * Disabled things are not counted: a disabled guardrail config, argument
    policy, semantic policy or response policy enforces nothing.
  * Every per-run figure is None when there are no runs to compute it
    over — "no runs" and "zero" mean opposite things.
  * Replays are simulations and never move a live KPI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.api.v1.models.admin.ai_system import (
    AISystemCardAgent,
    AISystemCardFacts,
    AISystemCardGateway,
    AISystemCardKpis,
    AISystemCardResources,
)
from app.constants.outcomes import RUN_FAILED
from app.constants.system_server import SYSTEM_SERVER_URL_PREFIX
from app.database.models.agent_card import AgentCard
from app.database.models.agent_identity import AgentIdentity, ResourceGroupAgentMember
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.ai_system_contract import CONTRACT_ACTIVE, AISystemContract
from app.database.models.ai_system_replay import (
    REPLAY_COMPLETED,
    AISystemReplayRun,
    AISystemReplaySuite,
)
from app.database.models.ai_system_response_policy import AISystemResponsePolicy
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.proxy_log import ProxyLog
from app.database.models.argument_policy import ArgumentPolicy, resource_group_argument_policies
from app.database.models.guardrail_config import GuardrailConfig, resource_group_guardrails
from app.database.models.llm_model import LLModel
from app.database.models.mcp_server import MCPServer
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_agent_cards,
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig
from app.database.models.semantic_policy import SemanticPolicy, resource_group_semantic_policies
from app.constants.outcomes import CONFORMANCE_UNDECLARED, CONFORMANCE_UNREPORTED
from app.services.ai_system_down_rule import (
    CALL_ANSWERED,
    CALL_BLOCKED,
    classify_segment_outcome,
    segment_call_outcome,
)
from app.services.ai_system_outcomes import class_cond, rate_cond, success_cond

DEFAULT_CARD_WINDOW_HOURS = 168  # 7 days — matches the unattributed-runs banner

#: Group-level limit columns. Each is merged restrictively down the chain.
_LIMIT_COLUMNS = (
    "llm_global_limits",
    "mcp_global_limits",
    "skill_global_limits",
    "a2a_global_limits",
    "media_global_limits",
    "operating_envelope",
)


def _numeric_leaf_paths(value: Any, prefix: str = "") -> Iterable[str]:
    """Paths of numeric leaves in a limits document.

    Booleans are flags (`hard_stop`, `enabled`), not limits, and are skipped
    — `bool` is a subclass of `int`, so it has to be excluded explicitly.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _numeric_leaf_paths(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield prefix


def _chains(
    db: Session, tenant_id: str, ids: List[str]
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Two chains per system from one read of the tenant's group tree (tens
    to hundreds of rows — never per-system walks):

    * the full chain `[self, parent, …, root]` — limits, policies and
      guardrails are restrictive and ALWAYS inherited;
    * the resource chain — models, MCP servers, skills and agent cards are
      additive and inherited only while each group has `inherit_from_parent`
      set. The walk stops at the first group that does not inherit; that
      group's own bindings still apply. This mirrors the proxy's access check
      (`usage::model_in_group_chain`) and `ai_system_closure.inheritance_chain`.
    """
    tree = {
        gid: (parent, bool(inherits))
        for gid, parent, inherits in db.query(
            ResourceGroup.id, ResourceGroup.parent_group_id, ResourceGroup.inherit_resources
        )
        .filter(ResourceGroup.tenant_id == tenant_id)
        .all()
    }
    full: Dict[str, List[str]] = {}
    resource: Dict[str, List[str]] = {}
    for sid in ids:
        chain: List[str] = []
        res_chain: List[str] = []
        seen: Set[str] = set()
        cur: Optional[str] = sid
        gated_open = True
        while cur and cur not in seen:  # `seen` guards a corrupt cycle
            seen.add(cur)
            chain.append(cur)
            parent, inherits = tree.get(cur, (None, False))
            if gated_open:
                res_chain.append(cur)
                gated_open = inherits
            cur = parent
        full[sid] = chain
        resource[sid] = res_chain
    return full, resource


def _bindings(db: Session, table, item_col: str, model, group_ids: Set[str]) -> Dict[str, Set[str]]:
    """`{group_id: {enabled item ids}}` for one additive association table."""
    if not group_ids:
        return {}
    rows = (
        db.query(table.c.resource_group_id, getattr(table.c, item_col))
        .join(model, model.id == getattr(table.c, item_col))
        .filter(table.c.resource_group_id.in_(group_ids), model.enabled.is_(True))
        .all()
    )
    out: Dict[str, Set[str]] = {}
    for gid, item in rows:
        out.setdefault(gid, set()).add(item)
    return out


def _union_over_chain(chain: List[str], by_group: Dict[str, Set[str]]) -> Set[str]:
    acc: Set[str] = set()
    for gid in chain:
        acc |= by_group.get(gid, set())
    return acc


def reachable_resources(
    db: Session,
    tenant_id: str,
    ids: List[str],
    *,
    resource_chains: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, AISystemCardResources]:
    """What each system can actually reach: its own resource bindings plus
    those inherited over the resource chain (see `_chains`), enabled only.

    The ONE count of a system's resources — the gallery card and the fleet
    table's composition columns both read it, so they cannot disagree.
    Direct bindings alone would show "0 models" for a system that runs on a
    model bound to its org unit. Four grouped queries for the whole page.
    """
    if not ids:
        return {}
    if resource_chains is None:
        _, resource_chains = _chains(db, tenant_id, ids)
    resource_chain_ids: Set[str] = {gid for chain in resource_chains.values() for gid in chain}

    models = _bindings(db, resource_group_llm_models, "llm_model_id", LLModel, resource_chain_ids)
    skills = _bindings(db, resource_group_agent_skills, "agent_skill_id", AgentSkill, resource_chain_ids)
    delegates = _bindings(db, resource_group_agent_cards, "agent_card_id", AgentCard, resource_chain_ids)
    # MCP servers the system's tools come from. Platform system servers
    # (`http://system/…`: the Agent Skills and Search VMCPs) are plumbing —
    # the Agent Skills one is auto-bound with every skill — so they are not
    # counted; skills have their own count. A config backed by a virtual
    # server has no MCPServer row and is kept by the outer join.
    mcp_servers: Dict[str, Set[str]] = {}
    if resource_chain_ids:
        for gid, cid in (
            db.query(resource_group_server_configs.c.resource_group_id, ServerConfig.id)
            .join(ServerConfig, ServerConfig.id == resource_group_server_configs.c.server_config_id)
            .outerjoin(MCPServer, MCPServer.id == ServerConfig.mcp_server_id)
            .filter(
                resource_group_server_configs.c.resource_group_id.in_(resource_chain_ids),
                ServerConfig.enabled.is_(True),
                (MCPServer.id.is_(None))
                | (
                    MCPServer.enabled.is_(True)
                    & ~func.coalesce(MCPServer.base_url, "").startswith(SYSTEM_SERVER_URL_PREFIX)
                ),
            )
            .all()
        ):
            mcp_servers.setdefault(gid, set()).add(cid)

    out: Dict[str, AISystemCardResources] = {}
    for sid in ids:
        chain = resource_chains.get(sid, [sid])
        out[sid] = AISystemCardResources(
            models=len(_union_over_chain(chain, models)),
            mcp_servers=len(_union_over_chain(chain, mcp_servers)),
            skills=len(_union_over_chain(chain, skills)),
            delegate_agents=len(_union_over_chain(chain, delegates)),
        )
    return out


#: `AISystemCardKpis.basis` values.
KPI_BASIS_RUNS = "runs"
KPI_BASIS_SERVED_CALLS = "served_calls"


def _nearest_rank(sorted_values: List[int], pct: float) -> Optional[int]:
    n = len(sorted_values)
    if n == 0:
        return None
    rank = 1 if n <= 1 else max(1, min(n, int(round(pct * n + 0.5))))
    return int(sorted_values[rank - 1])


def _served_call_kpis(
    db: Session, tenant_id: str, ids: List[str], since: datetime
) -> Dict[str, Dict[str, Any]]:
    """KPIs for systems that root no runs of their own in the window but
    served calls inside other systems' runs (a shared
    service is evidenced by the calls it served; "it has no runs" is a fact
    about the ledger, not an absence of work).

    One served call = one segment: this system's slice of one caller run.
    Its outcome is classified by THE served-call classification
    (`classify_segment_outcome`, the one health, liveness and dependency
    health read): answered/rejected = answered, failed/timed_out = failed,
    gateway-refused = out of the denominator, answered with an undeclared
    or unreported outcome = a deviation. Latency and the input/output split
    come from the service's own proxy rows in those runs (see below). Replay runs are excluded,
    as on the runs basis. A fixed number of grouped queries for the page.
    """
    if not ids:
        return {}
    seg_conds = [
        AISystemRunSegment.tenant_id == tenant_id,
        AISystemRunSegment.segment_system_id.in_(ids),
        AISystemRunSegment.first_action_at >= since,
        AISystemRun.is_replay.is_(False),
    ]
    base = db.query(AISystemRunSegment).join(
        AISystemRun,
        and_(
            AISystemRun.id == AISystemRunSegment.run_id,
            AISystemRun.tenant_id == AISystemRunSegment.tenant_id,
        ),
    )

    out: Dict[str, Dict[str, Any]] = {}
    for sid, outcome, conformance, cause, n, actions, cost, tokens, blocks in (
        base.with_entities(
            AISystemRunSegment.segment_system_id,
            AISystemRunSegment.outcome,
            AISystemRunSegment.outcome_conformance,
            AISystemRunSegment.error_cause,
            func.count(AISystemRunSegment.id),
            func.coalesce(func.sum(AISystemRunSegment.action_count), 0),
            func.coalesce(func.sum(AISystemRunSegment.cost_usd), 0),
            func.coalesce(func.sum(AISystemRunSegment.total_tokens), 0),
            func.coalesce(
                func.sum(
                    AISystemRunSegment.guardrail_block_count
                    + AISystemRunSegment.policy_denial_count
                ),
                0,
            ),
        )
        .filter(*seg_conds)
        .group_by(
            AISystemRunSegment.segment_system_id,
            AISystemRunSegment.outcome,
            AISystemRunSegment.outcome_conformance,
            AISystemRunSegment.error_cause,
        )
        .all()
    ):
        acc = out.setdefault(sid, {
            "served": 0, "spans": 0, "cost": 0.0, "tokens": 0, "blocks": 0,
            "answered": 0, "deviated": 0, "failed": 0,
            "input_tokens": 0, "output_tokens": 0, "latencies": [],
        })
        n = int(n)
        acc["served"] += n
        acc["spans"] += int(actions or 0)
        acc["cost"] += float(cost or 0)
        acc["tokens"] += int(tokens or 0)
        acc["blocks"] += int(blocks or 0)
        cls = classify_segment_outcome(segment_call_outcome(outcome, cause))
        if cls == CALL_BLOCKED:
            continue  # the service never saw it: out of the denominator
        if cls == CALL_ANSWERED:
            if conformance in (CONFORMANCE_UNDECLARED, CONFORMANCE_UNREPORTED):
                acc["deviated"] += n
            else:
                acc["answered"] += n
        else:
            acc["failed"] += n
    if not out:
        return {}

    served_ids = list(out)
    seg_served = [*seg_conds[:1], AISystemRunSegment.segment_system_id.in_(served_ids), *seg_conds[2:]]
    # The service's own proxy rows in the runs it served give the token
    # split (segments store only the total) and each served call's latency:
    # the summed duration of the service's calls in that run. Segment
    # `active_ms` is the wall-clock span from its first call's start to its
    # last stamp (minus holds) — it includes idle gaps between calls, and
    # segments under-counted it — so latency is
    # the service's own call time instead.
    served_runs = (
        base.with_entities(
            AISystemRunSegment.segment_system_id.label("sid"),
            AISystemRunSegment.root_task_id.label("root_task_id"),
        )
        .filter(*seg_served)
        .subquery()
    )
    for sid, _task, prompt, completion, duration in (
        db.query(
            served_runs.c.sid,
            served_runs.c.root_task_id,
            func.coalesce(func.sum(ProxyLog.llm_prompt_tokens), 0),
            func.coalesce(func.sum(ProxyLog.llm_completion_tokens), 0),
            func.sum(ProxyLog.duration_ms),
        )
        .join(
            ProxyLog,
            and_(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.root_task_id == served_runs.c.root_task_id,
                ProxyLog.resource_group_id == served_runs.c.sid,
            ),
        )
        .group_by(served_runs.c.sid, served_runs.c.root_task_id)
        .all()
    ):
        acc = out[sid]
        acc["input_tokens"] += int(prompt or 0)
        acc["output_tokens"] += int(completion or 0)
        if duration:
            acc["latencies"].append(int(duration))
    for acc in out.values():
        acc["latencies"].sort()
    return out


def card_facts(
    db: Session,
    tenant_id: str,
    page: List[ResourceGroup],
    *,
    window_hours: int = DEFAULT_CARD_WINDOW_HOURS,
    resources: Optional[Dict[str, AISystemCardResources]] = None,
) -> Dict[str, AISystemCardFacts]:
    """Card facts for every system on the page.

    Resources come from `reachable_resources`; pass `resources` when the
    caller already computed them (the fleet endpoint does, for its table
    columns) so they are counted once.
    """
    ids = [g.id for g in page]
    if not ids:
        return {}

    chains, resource_chains = _chains(db, tenant_id, ids)
    chain_ids: Set[str] = {gid for chain in chains.values() for gid in chain}
    if resources is None:
        resources = reachable_resources(db, tenant_id, ids, resource_chains=resource_chains)

    # ── Gateway: guardrails + policies (additive, enabled only) ─────────
    guardrails = _bindings(db, resource_group_guardrails, "guardrail_config_id", GuardrailConfig, chain_ids)
    arg_policies = _bindings(db, resource_group_argument_policies, "argument_policy_id", ArgumentPolicy, chain_ids)
    sem_policies = _bindings(db, resource_group_semantic_policies, "semantic_policy_id", SemanticPolicy, chain_ids)
    response_policies = dict(
        db.query(AISystemResponsePolicy.ai_system_id, func.count())
        .filter(
            AISystemResponsePolicy.tenant_id == tenant_id,
            AISystemResponsePolicy.ai_system_id.in_(ids),
            AISystemResponsePolicy.enabled.is_(True),
        )
        .group_by(AISystemResponsePolicy.ai_system_id)
        .all()
    )

    # ── Gateway: limits (restrictive — distinct paths across the chain) ─
    limit_docs: Dict[str, Dict[str, Any]] = {
        row[0]: dict(zip(_LIMIT_COLUMNS, row[1:]))
        for row in db.query(
            ResourceGroup.id, *[getattr(ResourceGroup, c) for c in _LIMIT_COLUMNS]
        ).filter(ResourceGroup.id.in_(chain_ids)).all()
    }
    # Per-binding limits on the system's own bindings (`limits` JSON on the
    # association row) — keyed by binding so two models' caps are two limits.
    binding_limit_paths: Dict[str, Set[str]] = {}
    for table, item_col in (
        (resource_group_llm_models, "llm_model_id"),
        (resource_group_server_configs, "server_config_id"),
        (resource_group_agent_cards, "agent_card_id"),
    ):
        if item_col not in table.c:
            continue
        for gid, item, limits in db.query(
            table.c.resource_group_id, getattr(table.c, item_col), table.c.limits
        ).filter(table.c.resource_group_id.in_(ids), table.c.limits.isnot(None)).all():
            binding_limit_paths.setdefault(gid, set()).update(
                f"{table.name}:{item}:{p}" for p in _numeric_leaf_paths(limits)
            )

    # ── Agent: identity, latest implementation, contract ────────────────
    identities: Dict[str, List[Tuple[str, str]]] = {}
    for gid, aid, name in (
        db.query(ResourceGroupAgentMember.group_id, AgentIdentity.id, AgentIdentity.name)
        .join(AgentIdentity, AgentIdentity.id == ResourceGroupAgentMember.agent_id)
        .filter(
            ResourceGroupAgentMember.tenant_id == tenant_id,
            ResourceGroupAgentMember.group_id.in_(ids),
        )
        .order_by(ResourceGroupAgentMember.joined_at.asc(), ResourceGroupAgentMember.id.asc())
        .all()
    ):
        identities.setdefault(gid, []).append((aid, name))

    rn = func.row_number().over(
        partition_by=AISystemRun.ai_system_id,
        order_by=AISystemRun.started_at.desc(),
    ).label("rn")
    latest_impl_sq = (
        db.query(
            AISystemRun.ai_system_id.label("sid"),
            AISystemRun.agent_impl_name.label("name"),
            AISystemRun.agent_impl_version.label("version"),
            AISystemRun.agent_impl_build.label("build"),
            AISystemRun.agent_impl_trust.label("trust"),
            rn,
        )
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id.in_(ids),
            AISystemRun.is_replay.is_(False),
            AISystemRun.agent_impl_name.isnot(None),
        )
        .subquery()
    )
    latest_impl = {
        r.sid: r
        for r in db.query(latest_impl_sq).filter(latest_impl_sq.c.rn == 1).all()
    }
    # A shared service roots no runs: the release that answered its latest
    # served call is the one on record (the core stamps the
    # release on each segment as on each run). Only for systems with no
    # reported release on a run of their own; replays excluded as above.
    no_run_release = [sid for sid in ids if sid not in latest_impl]
    if no_run_release:
        seg_rn = func.row_number().over(
            partition_by=AISystemRunSegment.segment_system_id,
            order_by=(AISystemRunSegment.first_action_at.desc(), AISystemRunSegment.id.desc()),
        ).label("rn")
        latest_seg_sq = (
            db.query(
                AISystemRunSegment.segment_system_id.label("sid"),
                AISystemRunSegment.agent_impl_name.label("name"),
                AISystemRunSegment.agent_impl_version.label("version"),
                AISystemRunSegment.agent_impl_build.label("build"),
                AISystemRunSegment.agent_impl_trust.label("trust"),
                seg_rn,
            )
            .join(
                AISystemRun,
                and_(
                    AISystemRun.id == AISystemRunSegment.run_id,
                    AISystemRun.tenant_id == AISystemRunSegment.tenant_id,
                ),
            )
            .filter(
                AISystemRunSegment.tenant_id == tenant_id,
                AISystemRunSegment.segment_system_id.in_(no_run_release),
                AISystemRunSegment.agent_impl_name.isnot(None),
                AISystemRun.is_replay.is_(False),
            )
            .subquery()
        )
        for r in db.query(latest_seg_sq).filter(latest_seg_sq.c.rn == 1).all():
            latest_impl[r.sid] = r

    contracts: Dict[str, Tuple[int, str]] = {}
    for sid, version, status in (
        db.query(AISystemContract.ai_system_id, AISystemContract.version, AISystemContract.status)
        .filter(AISystemContract.tenant_id == tenant_id, AISystemContract.ai_system_id.in_(ids))
        .all()
    ):
        cur = contracts.get(sid)
        # The active contract wins; otherwise the latest version.
        if cur is None or (status == CONTRACT_ACTIVE and cur[1] != CONTRACT_ACTIVE) or (
            cur[1] != CONTRACT_ACTIVE and status != CONTRACT_ACTIVE and version > cur[0]
        ):
            contracts[sid] = (version, status)

    # ── KPIs over the window ────────────────────────────────────────────
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    run_conds = [
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.ai_system_id.in_(ids),
        AISystemRun.is_replay.is_(False),
        AISystemRun.started_at >= since,
    ]

    def _one(cond):
        return func.sum(case((cond, 1), else_=0))

    kpi_rows = {
        r[0]: r
        for r in db.query(
            AISystemRun.ai_system_id,
            func.count(AISystemRun.id),
            func.coalesce(func.sum(AISystemRun.action_count), 0),
            func.coalesce(func.sum(AISystemRun.total_cost_usd), 0),
            func.coalesce(func.sum(AISystemRun.total_tokens), 0),
            _one(success_cond()),
            _one(rate_cond()),
            _one(class_cond(RUN_FAILED)),
            func.coalesce(
                func.sum(AISystemRun.guardrail_block_count + AISystemRun.policy_denial_count), 0
            ),
            func.count(AISystemRun.duration_ms),
            func.coalesce(func.sum(AISystemRun.prompt_tokens), 0),
            func.coalesce(func.sum(AISystemRun.completion_tokens), 0),
        )
        .filter(*run_conds)
        .group_by(AISystemRun.ai_system_id)
        .all()
    }

    def _latency_at(sid: str, n: int, pct: float) -> Optional[int]:
        """Nearest-rank percentile via one ordered probe (same semantics as
        the Signals tab: the value is one actually observed)."""
        if n <= 0:
            return None
        rank = 1 if n <= 1 else max(1, min(n, int(round(pct * n + 0.5))))
        row = (
            db.query(AISystemRun.duration_ms)
            .filter(*run_conds, AISystemRun.ai_system_id == sid, AISystemRun.duration_ms.isnot(None))
            .order_by(AISystemRun.duration_ms.asc())
            .offset(rank - 1)
            .limit(1)
            .first()
        )
        return int(row[0]) if row and row[0] is not None else None

    suites = dict(
        db.query(AISystemReplaySuite.ai_system_id, func.count())
        .filter(
            AISystemReplaySuite.tenant_id == tenant_id,
            AISystemReplaySuite.ai_system_id.in_(ids),
            AISystemReplaySuite.enabled.is_(True),
        )
        .group_by(AISystemReplaySuite.ai_system_id)
        .all()
    )
    last_eval: Dict[str, Tuple[Optional[float], Optional[datetime]]] = {}
    for sid, pass_rate, finished_at in (
        db.query(AISystemReplayRun.ai_system_id, AISystemReplayRun.pass_rate, AISystemReplayRun.finished_at)
        .filter(
            AISystemReplayRun.tenant_id == tenant_id,
            AISystemReplayRun.ai_system_id.in_(ids),
            AISystemReplayRun.status == REPLAY_COMPLETED,
            AISystemReplayRun.finished_at.isnot(None),
        )
        .order_by(AISystemReplayRun.finished_at.desc())
        .all()
    ):
        last_eval.setdefault(sid, (pass_rate, finished_at))  # first row = latest

    # Systems with no run of their own in the window: their served calls.
    served = _served_call_kpis(
        db, tenant_id, [sid for sid in ids if not (kpi_rows.get(sid) and kpi_rows[sid][1])], since
    )

    # ── Assemble ────────────────────────────────────────────────────────
    out: Dict[str, AISystemCardFacts] = {}
    for g in page:
        chain = chains[g.id]
        limit_paths: Set[str] = set(binding_limit_paths.get(g.id, set()))
        for gid in chain:
            for col, doc in (limit_docs.get(gid) or {}).items():
                limit_paths.update(f"{col}:{p}" for p in _numeric_leaf_paths(doc or {}))

        ids_for_agent = identities.get(g.id, [])
        impl = latest_impl.get(g.id)
        contract = contracts.get(g.id)
        k = kpi_rows.get(g.id)
        traces = int(k[1]) if k else 0
        rate_denominator = int(k[6] or 0) if k else 0
        timed = int(k[9] or 0) if k else 0
        ev = last_eval.get(g.id)
        catalogue = g.declared_outcomes if isinstance(g.declared_outcomes, list) else []

        out[g.id] = AISystemCardFacts(
            agent=AISystemCardAgent(
                identity_id=ids_for_agent[0][0] if ids_for_agent else None,
                identity_name=ids_for_agent[0][1] if ids_for_agent else None,
                identity_count=len(ids_for_agent),
                impl_name=impl.name if impl else None,
                impl_version=impl.version if impl else None,
                impl_build=impl.build if impl else None,
                impl_trust=impl.trust if impl else None,
                declared_outcomes=len(catalogue),
                contract_version=contract[0] if contract else None,
                contract_status=contract[1] if contract else None,
            ),
            gateway=AISystemCardGateway(
                policies=(
                    len(_union_over_chain(chain, arg_policies))
                    + len(_union_over_chain(chain, sem_policies))
                    + int(response_policies.get(g.id, 0))
                ),
                guardrails=len(_union_over_chain(chain, guardrails)),
                limits=len(limit_paths),
                blocks=(
                    int(k[8] or 0) if k else int(served[g.id]["blocks"]) if g.id in served else 0
                ),
            ),
            resources=resources[g.id],
            kpis=_served_kpis_model(window_hours, served[g.id], suites.get(g.id, 0), ev)
            if g.id in served
            else AISystemCardKpis(
                window_hours=window_hours,
                traces=traces,
                spans=int(k[2] or 0) if k else 0,
                success_rate=(
                    round(int(k[5] or 0) / rate_denominator, 6) if rate_denominator else None
                ),
                failed=int(k[7] or 0) if k else 0,
                p50_latency_ms=_latency_at(g.id, timed, 0.5),
                p95_latency_ms=_latency_at(g.id, timed, 0.95),
                cost_usd=round(float(k[3] or 0), 6) if k else 0.0,
                basis=KPI_BASIS_RUNS,
                tokens=int(k[4] or 0) if k else 0,
                input_tokens=int(k[10] or 0) if k else 0,
                output_tokens=int(k[11] or 0) if k else 0,
                eval_suites=int(suites.get(g.id, 0)),
                last_eval_pass_rate=ev[0] if ev else None,
                last_eval_at=ev[1].isoformat() if ev and ev[1] else None,
            ),
        )
    return out


def _served_kpis_model(
    window_hours: int, f: Dict[str, Any], eval_suites: int, ev: Optional[Tuple[Any, Any]]
) -> AISystemCardKpis:
    judged = f["answered"] + f["deviated"] + f["failed"]
    lat = f["latencies"]
    return AISystemCardKpis(
        window_hours=window_hours,
        basis=KPI_BASIS_SERVED_CALLS,
        traces=f["served"],
        spans=f["spans"],
        success_rate=round(f["answered"] / judged, 6) if judged else None,
        failed=f["failed"],
        p50_latency_ms=_nearest_rank(lat, 0.5),
        p95_latency_ms=_nearest_rank(lat, 0.95),
        cost_usd=round(f["cost"], 6),
        tokens=f["tokens"],
        input_tokens=f["input_tokens"],
        output_tokens=f["output_tokens"],
        eval_suites=int(eval_suites),
        last_eval_pass_rate=ev[0] if ev else None,
        last_eval_at=ev[1].isoformat() if ev and ev[1] else None,
    )

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Mission Control cards — the Resource Group dashboard API

    GET /v1/admin/mission-control/cards       — cards for one hierarchy level
    GET /v1/admin/mission-control/explainers  — operator-facing concept help

The landing-page API for the resource-group cards.
One request returns the cards for a hierarchy level (root when no
`parent_id`), each carrying identity, judgments, windowed measurements
and a trend — plus, at root, the one pseudo-card (unattributed runs).

Imported ("bought AI") usage is not a card of its own: it is attributed
to a Resource Group on ingest, so it rolls up that group's subtree like
any other spend, and each card reports how much of its spend was merely
observed rather than governed.

Gateway traffic routed under a group that is not an AI System (custom,
team, org, the system Unbound/Default groups) has no run-ledger system,
so it is measured from `usage_logs.group_id` — the ledger that group's
Usage tab reads — and rolls up the same way. AI System cards keep the
run ledger as their one source (their gateway rows mirror their runs).

## The two rules this endpoint exists to enforce

**Windows scale measurements, not judgments.** `window` selects the
measurement span for spend/runs/cost-per-task/trend (24h…all). Health,
verdict and Big-T class come from the materialized health row —
pinned to now / the standard 30-day period — and do not move with the
selector. "All-time healthy" is not a thing this API can say.

**Rollups are sums for additive metrics, worst-of for judgments.**
Group cards sum spend/runs/findings over their subtree and take the
WORST health, verdict and T-class beneath — never averages. The
comfortable number for an uncomfortable subtree is the failure mode
this dashboard was redesigned to kill.

## Query shape

Whole-tenant aggregates are computed in a handful of GROUP BY queries
(groups, health rows, run sums, trend buckets, findings) and assembled
in memory — the fleet is hundreds of rows, and the MC aggregation
lesson stands: no per-card queries, ever. Results are cached in-process
for `CACHE_TTL_SECONDS` per (tenant, window); search/sort/pagination
apply after the cache. A materialized daily-rollup table is the
documented follow-up if landing-page p95 ever demands it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.services.ai_system_outcomes import success_cond
from app.constants.groups import ResourceGroupName
from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.ai_system_drift import AISystemDriftEvent
from app.database.models.ai_system_health import AISystemHealth
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.usage_log import UsageLog
from app.services.tokenomics import EXPLAINERS, WORKLOAD_GUIDE, worst_class

WINDOWS: Dict[str, Optional[int]] = {
    "24h": 24, "7d": 168, "30d": 720, "90d": 2160, "all": None,
}
TREND_BUCKETS = 10

#: Landing-page cache. Keyed (tenant, window); search/sort/pagination are
#: applied after, so a filter change never misses the cache.
CACHE_TTL_SECONDS = 60
_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}

#: "Completed" on a card is a processed outcome: THE run
#: classification's `success` (`ai_system_outcomes`), so cost per task is
#: cost per processed outcome and empty polls never dilute it.
_PROCESSED = success_cond

#: Worst-first attention ranks. Lower = needs eyes sooner.
VERDICT_RANK = {"violations": 0, "insufficient_evidence": 3,
                "assured_with_exceptions": 2, "assured": 5}
HEALTH_RANK = {"suspended": 0, "silent": 1, "drifting": 1, "stalled": 1, "failing": 1,
               "degraded": 2, "unknown": 3, "learning": 4, "healthy": 5}


class UnattributedGroup(BaseModel):
    """One row of the Unattributed card's to-do list: the group its runs
    started in (`resource_group_id`); None = not recorded."""
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    runs: int = 0
    spend: float = 0.0


#: One-line purpose shown on system group cards that have one.
SYSTEM_GROUP_PURPOSE: Dict[str, str] = {
    ResourceGroupName.UNBOUND.value: "Platform calls: health checks, judges, embeddings",
}


class Card(BaseModel):
    id: str
    name: str
    group_type: str
    is_system: bool = Field(
        False, description="Platform-provisioned group (Default, Unbound); "
        "the UI labels it 'system' rather than its group_type."
    )
    purpose: Optional[str] = Field(
        None, description="One-line purpose of a system group, when it has one."
    )
    system_kind: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    eu_ai_act_risk_tier: Optional[str] = None
    is_group: bool = False
    systems: int = 0
    parent_group_id: Optional[str] = None

    health_state: Optional[str] = None
    verdict_status: Optional[str] = None
    t_class: Optional[str] = None
    t_class_contains: bool = Field(
        False, description="True on group cards: the class is the worst in "
        "the subtree, rendered as 'contains T(…)'."
    )
    k_median: Optional[float] = None
    a_max: Optional[int] = None

    spend: float = 0.0
    observed_spend: float = Field(
        0.0, description="The part of `spend` that was imported rather than "
        "routed: costed in the same ledger, but no guardrail, policy or "
        "budget was enforced on it. Equal to `spend` when nothing on this "
        "card was governed."
    )
    runs: int = 0
    completed_runs: int = 0
    cost_per_task: Optional[float] = None
    cost_per_task_blended: bool = False
    cpt_bound: Optional[float] = None
    cpt_breached: bool = False

    trend: List[float] = Field(default_factory=list)
    # Tokens are the native tokenomics unit (cost = tokens × price, so a
    # token trend diverging from the cost trend is a model-mix / price
    # signal). data_bytes is the run ledger's input+output payload mass —
    # the non-LLM half (MCP/A2A payloads) made visible.
    tokens: int = 0
    data_bytes: int = 0
    trend_tokens: List[float] = Field(default_factory=list)
    trend_bytes: List[float] = Field(default_factory=list)
    live: bool = False
    findings: int = 0
    governance_gaps: int = 0
    # Attested exclusion from the rollup. The card still shows its
    # own health/verdict; only the parent's worst-of stops reading it.
    excluded: bool = False
    exclusion_reason: Optional[str] = None
    exclusion_expires_at: Optional[str] = None
    excluded_systems: int = Field(
        0, description="On group cards: systems in the subtree left out of the rollup."
    )

    pseudo: Optional[str] = Field(
        None, description="'unattributed' for the one pseudo-card; None for "
        "real groups. Imported spend is no longer a pseudo-card — it lands "
        "on the Resource Group it was attributed to."
    )
    note: Optional[str] = None
    unattributed_by_group: Optional[List[UnattributedGroup]] = Field(
        None, description="On the unattributed pseudo-card only: its runs "
        "by the group they started in, runs desc. Unbound platform calls "
        "are excluded from the card entirely."
    )


class CardsResponse(BaseModel):
    cards: List[Card]
    total: int
    window: str
    parent_id: Optional[str] = None
    path: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Breadcrumb: ancestors of parent_id, root-first.",
    )
    judgments_pinned_note: str = (
        "health/verdict/t_class are pinned to the standard 30-day judgment "
        "window; the window parameter scales measurements only"
    )


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _since(window: str) -> Optional[datetime]:
    hours = WINDOWS[window]
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None
    )


AUTO_EXCLUDED_STAGES = ("deprecated", "retired")


def effective_exclusion(g: ResourceGroup, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The exclusion in force for a group, or None.

    Deprecated and retired systems are excluded by their stage alone — the
    lifecycle already attests that. A stored exclusion counts only until its
    expiry: a lapsed one is not "still excluded, just late", it is included,
    and the scheduler raises an inbox item so somebody re-decides.
    """
    now = now or datetime.now(timezone.utc)
    if g.lifecycle_stage in AUTO_EXCLUDED_STAGES:
        return {"reason": g.lifecycle_stage, "note": None, "expires_at": None, "by": None, "at": None}
    raw = getattr(g, "rollup_exclusion", None)
    if not raw or not isinstance(raw, dict) or not raw.get("reason"):
        return None
    exp = raw.get("expires_at")
    if exp:
        try:
            when = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= now:
                return None
        except ValueError:
            return None
    return raw


#: Shape of a per-group measurement row. A group with imported usage but
#: no runs still needs every key present.
_EMPTY_SUMS: Dict[str, Any] = {
    "runs": 0, "completed": 0, "spend": 0.0, "completed_spend": 0.0,
    "tokens": 0, "data_bytes": 0, "observed_spend": 0.0,
}


def _build_estate(db: Session, tenant_id: str, window: str) -> Dict[str, Any]:
    """Everything the cards need, in a fixed number of queries."""
    since = _since(window)

    groups = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.is_active.is_(True),
        )
        .all()
    )

    health = {
        h.ai_system_id: h
        for h in db.query(AISystemHealth)
        .filter(AISystemHealth.tenant_id == tenant_id)
        .all()
    }

    run_filters = [
        AISystemRun.tenant_id == tenant_id,
        AISystemRun.is_replay.is_(False),
        AISystemRun.terminal_state.isnot(None),
    ]
    if since is not None:
        run_filters.append(AISystemRun.started_at >= since)

    sums: Dict[str, Dict[str, float]] = {}
    for gid, runs, completed, spend, cspend, tokens, dbytes in (
        db.query(
            AISystemRun.ai_system_id,
            func.count(AISystemRun.id),
            func.sum(
                case((_PROCESSED(), 1), else_=0)
            ),
            func.sum(AISystemRun.total_cost_usd),
            func.sum(
                case(
                    (
                        _PROCESSED(),
                        AISystemRun.total_cost_usd,
                    ),
                    else_=0,
                )
            ),
            func.sum(AISystemRun.total_tokens),
            func.sum(AISystemRun.input_bytes + AISystemRun.output_bytes),
        )
        .filter(*run_filters)
        .filter(AISystemRun.ai_system_id.isnot(None))
        .group_by(AISystemRun.ai_system_id)
        .all()
    ):
        sums[gid] = {
            "runs": int(runs or 0),
            "completed": int(completed or 0),
            "spend": float(spend or 0.0),
            "completed_spend": float(cspend or 0.0),
            "tokens": int(tokens or 0),
            "data_bytes": int(dbytes or 0),
            "observed_spend": 0.0,
        }

    # Trend: bucket spend by day per system (cap the span for 'all').
    trend_since = since or (datetime.now(timezone.utc) - timedelta(days=365))
    bucket = func.date(AISystemRun.started_at)
    trends: Dict[str, Dict[str, float]] = {}
    trends_tokens: Dict[str, Dict[str, float]] = {}
    trends_bytes: Dict[str, Dict[str, float]] = {}
    for gid, day, spend, tok, byt in (
        db.query(
            AISystemRun.ai_system_id,
            bucket,
            func.sum(AISystemRun.total_cost_usd),
            func.sum(AISystemRun.total_tokens),
            func.sum(AISystemRun.input_bytes + AISystemRun.output_bytes),
        )
        .filter(*[f for f in run_filters if f is not None])
        .filter(AISystemRun.started_at >= trend_since)
        .filter(AISystemRun.ai_system_id.isnot(None))
        .group_by(AISystemRun.ai_system_id, bucket)
        .all()
    ):
        trends.setdefault(gid, {})[str(day)] = float(spend or 0.0)
        trends_tokens.setdefault(gid, {})[str(day)] = float(tok or 0)
        trends_bytes.setdefault(gid, {})[str(day)] = float(byt or 0)

    findings: Dict[str, int] = {
        gid: int(n)
        for gid, n in db.query(
            AISystemDriftEvent.ai_system_id, func.count(AISystemDriftEvent.id)
        )
        .filter(
            AISystemDriftEvent.tenant_id == tenant_id,
            AISystemDriftEvent.status.in_(("active", "acknowledged")),
        )
        .group_by(AISystemDriftEvent.ai_system_id)
        .all()
    }

    # Unattributed + observed pseudo-card facts.
    # Every additive measure a system card carries, not just runs and spend
    # (tokens and bytes too).
    #
    # The card is a to-do list ("attribute it to a system"), so it is
    # broken down by the group each run started in
    # (`resource_group_id`), and runs started in the tenant's Unbound
    # system group are left out: those are platform calls (health checks,
    # judges, embeddings) that can never belong to an AI System. Runs with
    # no recorded group stay counted under an
    # "unknown group" row — recorded, never dropped.
    unbound_ids = {
        gid for (gid,) in db.query(ResourceGroup.id).filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.is_system.is_(True),
            ResourceGroup.name == ResourceGroupName.UNBOUND.value,
        ).all()
    }
    ua_runs, ua_spend = 0, 0.0
    ua_tokens = ua_bytes = 0
    ua_by_group: List[Dict[str, Any]] = []
    for rgid, n, cost, tokens, dbytes in (
        db.query(
            AISystemRun.resource_group_id,
            func.count(AISystemRun.id),
            func.sum(AISystemRun.total_cost_usd),
            func.sum(AISystemRun.total_tokens),
            func.sum(AISystemRun.input_bytes + AISystemRun.output_bytes),
        )
        .filter(*run_filters)
        .filter(AISystemRun.ai_system_id.is_(None))
        .group_by(AISystemRun.resource_group_id)
        .all()
    ):
        if rgid in unbound_ids:
            continue
        ua_runs += int(n or 0)
        ua_spend += float(cost or 0.0)
        ua_tokens += int(tokens or 0)
        ua_bytes += int(dbytes or 0)
        ua_by_group.append({
            "group_id": rgid, "runs": int(n or 0),
            "spend": round(float(cost or 0.0), 6),
        })
    names = {g.id: (g.display_name or g.name) for g in groups}
    missing = [r["group_id"] for r in ua_by_group
               if r["group_id"] and r["group_id"] not in names]
    if missing:  # an inactive group still names its runs
        names.update({
            gid: (dn or n) for gid, dn, n in db.query(
                ResourceGroup.id, ResourceGroup.display_name, ResourceGroup.name
            ).filter(ResourceGroup.id.in_(missing)).all()
        })
    for r in ua_by_group:
        r["group_name"] = names.get(r["group_id"]) if r["group_id"] else None
    ua_by_group.sort(key=lambda r: (-r["runs"], -r["spend"], r["group_name"] or ""))
    unattributed = (ua_runs, ua_spend, ua_tokens, ua_bytes)
    # Imported ("observed") usage. Provenance is the usage_logs source
    # 'import:<api>' — NOT the proxy_metrics 'observed' source
    # (cost_metrics_service rule). It is attributed to a Resource Group
    # like any other spend, so it belongs on that group's card rather
    # than in a synthetic estate-wide one; imported aggregates carry no
    # run, so they add spend and tokens and never runs or cost-per-task.
    # `created_at` on an imported row is the bucket start, so the window selector scales it correctly.
    #
    # Direct gateway traffic on a group that is NOT an AI System. The run
    # ledger keys runs by `ai_system_id`, so traffic routed under a
    # custom/team/org group (or the system Unbound/Default group) has no
    # system and no card of its own. That traffic is counted here from the
    # same ledger the group's Usage tab reads (`usage_logs.group_id`). An AI System's gateway rows
    # mirror its runs, so for systems the run ledger stays the one source
    # (counting both would double it). Direct traffic is spend, tokens and
    # MCP payload bytes — never a run or a cost-per-task.
    ai_system_ids = {
        g.id for g in groups if g.group_type == GroupType.AI_SYSTEM
    }
    imported = case((UsageLog.source.like("import:%"), 1), else_=0)
    usage_filters = [
        UsageLog.tenant_id == tenant_id,
        UsageLog.group_id.isnot(None),
    ]

    def _counts(gid: str, is_import: Any) -> bool:
        return bool(is_import) or gid not in ai_system_ids

    usage_q = db.query(
        UsageLog.group_id,
        imported,
        func.sum(UsageLog.estimated_cost),
        func.sum(UsageLog.total_tokens),
        func.sum(UsageLog.mcp_data_size_bytes),
    ).filter(*usage_filters)
    if since is not None:
        usage_q = usage_q.filter(UsageLog.created_at >= since)
    for gid, is_import, cost, tokens, mbytes in usage_q.group_by(
        UsageLog.group_id, imported
    ).all():
        if not _counts(gid, is_import):
            continue
        row = sums.setdefault(gid, dict(_EMPTY_SUMS))
        row["spend"] += float(cost or 0.0)
        row["tokens"] += int(tokens or 0)
        if is_import:
            row["observed_spend"] += float(cost or 0.0)
        else:
            # Imported aggregates carry no payload; MCP rows do.
            row["data_bytes"] += int(mbytes or 0)

    # The resolver always assigns a group, so this should be zero — but
    # spend that lands nowhere is money the dashboard would silently lose,
    # and "recorded, never dropped" is the one promise the unattributed
    # card exists to keep. Anything group-less joins it.
    orphan_q = db.query(
        func.sum(UsageLog.estimated_cost), func.sum(UsageLog.total_tokens)
    ).filter(
        UsageLog.tenant_id == tenant_id,
        UsageLog.source.like("import:%"),
        UsageLog.group_id.is_(None),
    )
    if since is not None:
        orphan_q = orphan_q.filter(UsageLog.created_at >= since)
    orphan_cost, orphan_tokens = orphan_q.first() or (None, None)
    orphan_observed = float(orphan_cost or 0.0)

    obucket = func.date(UsageLog.created_at)
    for gid, is_import, day, cost, tokens, mbytes in (
        db.query(
            UsageLog.group_id,
            imported,
            obucket,
            func.sum(UsageLog.estimated_cost),
            func.sum(UsageLog.total_tokens),
            func.sum(UsageLog.mcp_data_size_bytes),
        )
        .filter(*usage_filters)
        .filter(UsageLog.created_at >= trend_since)
        .group_by(UsageLog.group_id, imported, obucket)
        .all()
    ):
        if not _counts(gid, is_import):
            continue
        d = str(day)
        bucket_spend = trends.setdefault(gid, {})
        bucket_spend[d] = bucket_spend.get(d, 0.0) + float(cost or 0.0)
        bucket_tokens = trends_tokens.setdefault(gid, {})
        bucket_tokens[d] = bucket_tokens.get(d, 0.0) + float(tokens or 0)
        if not is_import and mbytes:
            bucket_bytes = trends_bytes.setdefault(gid, {})
            bucket_bytes[d] = bucket_bytes.get(d, 0.0) + float(mbytes)

    return {
        "groups": groups,
        "health": health,
        "sums": sums,
        "trends": trends,
        "trends_tokens": trends_tokens,
        "trends_bytes": trends_bytes,
        "findings": findings,
        "unattributed": {
            "runs": int(unattributed[0] or 0),
            "spend": float(unattributed[1] or 0.0) + orphan_observed,
            "observed_spend": orphan_observed,
            # Imported usage carries tokens but no payload, like on group cards.
            "tokens": int(unattributed[2] or 0) + int(orphan_tokens or 0),
            "data_bytes": int(unattributed[3] or 0),
            "by_group": ua_by_group,
        },
    }


def _trend_series(daymap: Dict[str, float]) -> List[float]:
    if not daymap:
        return []
    days = sorted(daymap)
    values = [daymap[d] for d in days]
    if len(values) <= TREND_BUCKETS:
        return [round(v, 4) for v in values]
    # Downsample by summing evenly-sized chunks — a spend trend, so sums.
    size = len(values) / TREND_BUCKETS
    out = []
    for i in range(TREND_BUCKETS):
        chunk = values[int(i * size): int((i + 1) * size)]
        out.append(round(sum(chunk), 4))
    return out


def _governance_gap_count(g: ResourceGroup) -> int:
    if g.group_type != GroupType.AI_SYSTEM:
        return 0
    return sum(
        1
        for attr in ("owner", "intended_use", "lifecycle_stage",
                     "eu_ai_act_risk_tier")
        if not getattr(g, attr, None)
    )


def _card_for(g: ResourceGroup, estate: Dict[str, Any]) -> Card:
    h = estate["health"].get(g.id)
    s = estate["sums"].get(g.id, {})
    completed = s.get("completed", 0)
    completed_spend = s.get("completed_spend", 0.0)
    cpt = (completed_spend / completed) if completed else None

    envelope = g.operating_envelope or {}
    # The card's cost-per-task figure is a MEAN over completed runs, so it
    # must be compared against the mean bound. `max_cost_per_run_usd` is a
    # ceiling for one run (enforced at the gate) and comparing a mean to it
    # would badge a healthy system red the moment its ceiling was tightened.
    bound = envelope.get("max_avg_cost_per_completed_task_usd")

    excl = effective_exclusion(g)
    return Card(
        id=g.id,
        name=g.display_name or g.name,
        group_type=str(g.group_type.value if hasattr(g.group_type, "value") else g.group_type),
        is_system=bool(g.is_system),
        purpose=SYSTEM_GROUP_PURPOSE.get(g.name) if g.is_system else None,
        system_kind=g.system_kind,
        lifecycle_stage=g.lifecycle_stage,
        eu_ai_act_risk_tier=g.eu_ai_act_risk_tier,
        parent_group_id=g.parent_group_id,
        health_state=h.health_state if h else None,
        verdict_status=h.verdict_status if h else None,
        t_class=h.t_class if h else None,
        k_median=h.k_median if h else None,
        a_max=h.a_max if h else None,
        spend=round(s.get("spend", 0.0), 6),
        observed_spend=round(s.get("observed_spend", 0.0), 6),
        runs=s.get("runs", 0),
        completed_runs=completed,
        cost_per_task=round(cpt, 6) if cpt is not None else None,
        cpt_bound=bound,
        cpt_breached=bool(bound and cpt is not None and cpt > bound),
        trend=_trend_series(estate["trends"].get(g.id, {})),
        tokens=s.get("tokens", 0),
        data_bytes=s.get("data_bytes", 0),
        trend_tokens=_trend_series(estate["trends_tokens"].get(g.id, {})),
        trend_bytes=_trend_series(estate["trends_bytes"].get(g.id, {})),
        live=bool(h and h.runs_24h),
        findings=estate["findings"].get(g.id, 0),
        governance_gaps=_governance_gap_count(g),
        excluded=excl is not None,
        exclusion_reason=excl.get("reason") if excl else None,
        exclusion_expires_at=excl.get("expires_at") if excl else None,
    )


def _rollup(card: Card, children: List[Card]) -> Card:
    """Group rollup: sums for additive metrics, worst-of for judgments."""
    if not children:
        return card
    kids_systems = sum(
        c.systems if c.is_group else (1 if c.group_type == "ai_system" else 0)
        for c in children
    )
    spend = card.spend + sum(c.spend for c in children)
    runs = card.runs + sum(c.runs for c in children)
    completed = card.completed_runs + sum(c.completed_runs for c in children)
    completed_spend = sum(
        (c.cost_per_task or 0) * c.completed_runs for c in children
    ) + (card.cost_per_task or 0) * card.completed_runs
    cpt = (completed_spend / completed) if completed else None

    worst = lambda rank, values: min(
        (v for v in values if v in rank), key=lambda v: rank[v], default=None
    )
    judged = [c for c in children if not c.excluded]
    t = worst_class(card.t_class, *(c.t_class for c in judged))

    def _merge(series_of) -> Dict[int, float]:
        merged: Dict[int, float] = {}
        for c in [card, *children]:
            for i, v in enumerate(series_of(c)):
                merged[i] = merged.get(i, 0.0) + v
        return merged

    merged_trend = _merge(lambda c: c.trend)
    merged_tokens = _merge(lambda c: c.trend_tokens)
    merged_bytes = _merge(lambda c: c.trend_bytes)

    return card.model_copy(update={
        "is_group": True,
        "systems": kids_systems,
        "spend": round(spend, 6),
        "observed_spend": round(
            card.observed_spend + sum(c.observed_spend for c in children), 6
        ),
        "runs": runs,
        "completed_runs": completed,
        "tokens": card.tokens + sum(c.tokens for c in children),
        "data_bytes": card.data_bytes + sum(c.data_bytes for c in children),
        "cost_per_task": round(cpt, 6) if cpt is not None else None,
        "cost_per_task_blended": True,
        # Judgments take the worst of the subtree — minus the attested
        # exclusions. Sums above still include them: an excluded system's
        # spend is real; only its verdict is excused, and the count says so.
        "health_state": worst(
            HEALTH_RANK,
            [card.health_state, *(c.health_state for c in judged)],
        ),
        "verdict_status": worst(
            VERDICT_RANK,
            [card.verdict_status, *(c.verdict_status for c in judged)],
        ),
        "t_class": t,
        "t_class_contains": t is not None,
        "excluded_systems": sum(
            (c.excluded_systems if c.is_group else (1 if c.excluded else 0)) for c in children
        ),
        "trend": [round(merged_trend[i], 4) for i in sorted(merged_trend)],
        "trend_tokens": [round(merged_tokens[i], 1) for i in sorted(merged_tokens)],
        "trend_bytes": [round(merged_bytes[i], 1) for i in sorted(merged_bytes)],
        "live": card.live or any(c.live for c in children),
        "findings": card.findings + sum(c.findings for c in children),
        "governance_gaps": card.governance_gaps
        + sum(c.governance_gaps for c in children),
    })


def _assemble(estate: Dict[str, Any]) -> Dict[str, Card]:
    """Every group's card with subtree rollups applied, bottom-up."""
    groups: List[ResourceGroup] = estate["groups"]
    by_parent: Dict[Optional[str], List[ResourceGroup]] = {}
    for g in groups:
        by_parent.setdefault(g.parent_group_id, []).append(g)

    cards: Dict[str, Card] = {}

    def build(g: ResourceGroup) -> Card:
        base = _card_for(g, estate)
        children = [build(c) for c in by_parent.get(g.id, [])]
        card = _rollup(base, children)
        cards[g.id] = card
        return card

    for root in by_parent.get(None, []):
        build(root)
    return cards


def _attention_key(c: Card) -> Tuple:
    return (
        2 if c.pseudo else (1 if c.excluded else 0),
        min(
            VERDICT_RANK.get(c.verdict_status, 9),
            HEALTH_RANK.get(c.health_state, 9),
        ),
        -c.findings,
        -(c.spend or 0),
    )


def create_mission_control_cards_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/mission-control", tags=["Mission Control"])

    @router.get("/explainers", summary="Operator-facing concept explainers")
    async def explainers(
        _admin=Depends(get_current_admin),
    ) -> Dict[str, Any]:
        # Served from the tokenomics module — one definition, every surface.
        return {"explainers": EXPLAINERS, "guide": WORKLOAD_GUIDE}

    @router.get(
        "/cards",
        response_model=CardsResponse,
        summary="Resource Group cards for one hierarchy level",
    )
    def cards(
        parent_id: Optional[str] = Query(None),
        window: str = Query("30d"),
        q: Optional[str] = Query(None, max_length=200),
        group_type: Optional[str] = Query(None),
        sort: str = Query("attention"),
        limit: int = Query(60, ge=1, le=200),
        offset: int = Query(0, ge=0),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> CardsResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        if window not in WINDOWS:
            raise HTTPException(
                status_code=422, detail=f"window must be one of {sorted(WINDOWS)}"
            )
        if sort not in ("attention", "spend", "cpt", "name"):
            raise HTTPException(
                status_code=422,
                detail="sort must be one of attention|spend|cpt|name",
            )

        key = (resolved, window)
        now = time.monotonic()
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            estate, cards_by_id = hit[1]["estate"], hit[1]["cards"]
        else:
            estate = _build_estate(db, resolved, window)
            cards_by_id = _assemble(estate)
            _cache[key] = (now, {"estate": estate, "cards": cards_by_id})

        if q:
            needle = q.lower()
            selected = [
                c for c in cards_by_id.values() if needle in c.name.lower()
            ]
        elif parent_id is not None:
            if parent_id not in cards_by_id:
                raise HTTPException(
                    status_code=404, detail=f"Group {parent_id!r} not found"
                )
            selected = [
                c for c in cards_by_id.values()
                if c.parent_group_id == parent_id
            ]
        else:
            selected = [
                c for c in cards_by_id.values() if c.parent_group_id is None
            ]

        if group_type:
            selected = [c for c in selected if c.group_type == group_type]

        sorters = {
            "attention": _attention_key,
            "spend": lambda c: (1 if c.pseudo else 0, -(c.spend or 0)),
            "cpt": lambda c: (1 if c.pseudo else 0, -(c.cost_per_task or 0)),
            "name": lambda c: (1 if c.pseudo else 0, c.name.lower()),
        }
        selected.sort(key=sorters[sort])

        # Pseudo-cards only on the unfiltered root level. Appended after
        # the sort (they always trail) but BEFORE total/slicing, so
        # `total` is stable across pages and they paginate like any card.
        if parent_id is None and not q and not group_type:
            ua = estate["unattributed"]
            if ua["runs"] or ua["spend"]:
                selected.append(Card(
                    id="pseudo-unattributed", name="Unattributed runs",
                    group_type="pseudo", pseudo="unattributed",
                    runs=ua["runs"], spend=round(ua["spend"], 6),
                    observed_spend=round(ua["observed_spend"], 6),
                    tokens=ua["tokens"], data_bytes=ua["data_bytes"],
                    unattributed_by_group=[
                        UnattributedGroup(**r) for r in ua["by_group"]
                    ],
                    note=EXPLAINERS["unattributed"]["body"],
                ))

        # Breadcrumb for the requested level.
        path: List[Dict[str, str]] = []
        cursor = parent_id
        guard = 0
        while cursor and cursor in cards_by_id and guard < 10:
            c = cards_by_id[cursor]
            path.append({"id": c.id, "name": c.name})
            cursor = c.parent_group_id
            guard += 1
        path.reverse()

        total = len(selected)
        return CardsResponse(
            cards=selected[offset: offset + limit],
            total=total,
            window=window,
            parent_id=parent_id,
            path=path,
        )

    return router

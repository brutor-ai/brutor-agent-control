# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent-activity service for Mission Control's "Agents" tab.

Aggregates the per-agent runtime-authorization signals an Agent Gateway
operator cares about, from `proxy_logs` (the Rust core stamps `actor_agent_id`
and `agent_authz_decision` on every governed action an agent drives):

  * total agent-driven actions, and how many were allowed / approval-required /
    denied (the "actions, not access" outcome distribution)
  * top agents by volume, each with its deny + approval rates
  * a denied-action feed (most recent agent_authz blocks) for drill-down

No new columns — `proxy_logs.actor_agent_id` / `agent_authz_decision` /
`agent_grant_id` already exist. Observability only; this never changes
enforcement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import Integer, func
from sqlalchemy.orm import Session

from app.constants.proxy import AgentAuthzDecision, ProxySubtype
from app.core.logging_system import BrutorLogger
from app.services.metrics_window import group_id_filter
from app.database.models.proxy_log import ProxyLog


class AgentMetricsService:
    """Read-only per-agent activity aggregations over proxy_logs."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("agent-metrics-service")

    def activity(
        self, db: Session, tenant_id: str, period_hours: int = 720, top_n: int = 10,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        start = None
        if period_hours and period_hours > 0:
            start = datetime.now(timezone.utc) - timedelta(hours=period_hours)

        # Only rows an agent principal drove.
        base = [
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.actor_agent_id.isnot(None),
        ]
        if start is not None:
            base.append(ProxyLog.timestamp >= start)
        if group_id:
            gf = group_id_filter(ProxyLog.resource_group_id, group_id)
            if gf is not None:
                base.append(gf)

        def _count(*extra) -> int:
            return int(
                db.query(func.count(ProxyLog.id)).filter(*base, *extra).scalar() or 0
            )

        total = _count()
        approval = _count(ProxyLog.agent_authz_decision == AgentAuthzDecision.APPROVAL_REQUIRED.value)
        denied = _count(ProxyLog.agent_authz_decision == AgentAuthzDecision.DENY.value)
        # "Allowed" = agent actions that ran (passed authz). Only denials and
        # approval-gates are stamped with an explicit decision; the ~all ungated
        # actions carry a NULL decision, so an `== ALLOW` equality count read 0
        # even though nearly everything was allowed. Allowed is the complement.
        allowed = max(0, total - approval - denied)

        # Top agents by volume, each with deny/approval counts.
        rows = (
            db.query(
                ProxyLog.actor_agent_id,
                func.count(ProxyLog.id),
                func.sum(func.cast(ProxyLog.agent_authz_decision == AgentAuthzDecision.DENY.value, Integer)),
                func.sum(
                    func.cast(ProxyLog.agent_authz_decision == AgentAuthzDecision.APPROVAL_REQUIRED.value, Integer)
                ),
            )
            .filter(*base)
            .group_by(ProxyLog.actor_agent_id)
            .order_by(func.count(ProxyLog.id).desc())
            .limit(top_n)
            .all()
        )
        top_agents = []
        for agent_id, calls, deny_n, appr_n in rows:
            calls = int(calls or 0)
            deny_n = int(deny_n or 0)
            appr_n = int(appr_n or 0)
            top_agents.append(
                {
                    "agent_id": agent_id,
                    "calls": calls,
                    "denied": deny_n,
                    "approval_required": appr_n,
                    "deny_rate": round((deny_n / calls * 100.0), 2) if calls else 0.0,
                    "approval_rate": round((appr_n / calls * 100.0), 2) if calls else 0.0,
                }
            )

        # Most recent denied actions for the drill-down feed.
        recent_denied = [
            {
                "agent_id": r.actor_agent_id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "proxy_type": r.proxy_type,
                "capability": r.mcp_capability_name,
                "grant_id": r.agent_grant_id,
                "resource_group_id": r.resource_group_id,
            }
            for r in (
                db.query(ProxyLog)
                .filter(*base, ProxyLog.agent_authz_decision == AgentAuthzDecision.DENY.value)
                .order_by(ProxyLog.timestamp.desc())
                .limit(top_n)
                .all()
            )
        ]

        # Semantic Governance Policy outcomes (across all actors, not just
        # agents — SGP applies to any governed request). Surfaces shadow
        # "would-block" verdicts so operators can measure a policy before
        # flipping it to enforce.
        sgp_base = [ProxyLog.tenant_id == tenant_id]
        if start is not None:
            sgp_base.append(ProxyLog.timestamp >= start)
        if group_id:
            gf = group_id_filter(ProxyLog.resource_group_id, group_id)
            if gf is not None:
                sgp_base.append(gf)
        sgp_block = int(
            db.query(func.count(ProxyLog.id))
            .filter(*sgp_base, ProxyLog.proxy_subtype == ProxySubtype.SEMANTIC_POLICY_BLOCK.value)
            .scalar() or 0
        )
        sgp_shadow_rows = (
            db.query(ProxyLog)
            .filter(*sgp_base, ProxyLog.proxy_subtype == ProxySubtype.SEMANTIC_POLICY_SHADOW.value)
            .order_by(ProxyLog.timestamp.desc())
            .limit(top_n)
            .all()
        )
        sgp_shadow_total = int(
            db.query(func.count(ProxyLog.id))
            .filter(*sgp_base, ProxyLog.proxy_subtype == ProxySubtype.SEMANTIC_POLICY_SHADOW.value)
            .scalar() or 0
        )
        recent_shadow = [
            {
                "policy_id": r.semantic_policy_id,
                "agent_id": r.actor_agent_id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "proxy_type": r.proxy_type,
                "reason": r.error_message,
                "resource_group_id": r.resource_group_id,
            }
            for r in sgp_shadow_rows
        ]

        return {
            "period_hours": period_hours,
            "total_actions": total,
            "allowed": allowed,
            "approval_required": approval,
            "denied": denied,
            "deny_rate": round((denied / total * 100.0), 2) if total else 0.0,
            "approval_rate": round((approval / total * 100.0), 2) if total else 0.0,
            "top_agents": top_agents,
            "recent_denied": recent_denied,
            "semantic_policy": {
                "blocked": sgp_block,
                "shadow_would_block": sgp_shadow_total,
                "recent_shadow": recent_shadow,
            },
        }

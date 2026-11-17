# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Governance-posture service for Mission Control's Governance tab.

Aggregates the detective/preventive-control signals an AI governance operator
cares about:
  * guardrail blocks, argument-policy denials, PII detections (proxy_logs
    boolean outcome columns — block-only; there is no redact/flag column, so
    we report blocks/denials/detections, not redactions)
  * shadow-AI / ungoverned usage — observed (imported) traffic that never hit
    the gateway's enforcement, plus usage of un-approved models (imported /
    disabled / not in the curated catalog) from the unified ledger

Compliance coverage and budget/quota breaches are NOT recomputed here — the
Governance tab reuses the existing `/v1/admin/compliance/dashboard` and
`/v1/admin/resource-groups/usage-alerts` endpoints for those.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.services.metrics_window import group_id_filter, resolve_window
from app.database.models.llm_model import LLModel
from app.database.models.proxy_log import ProxyLog
from app.database.models.usage_log import UsageLog


class GovernanceMetricsService:
    """Read-only governance-posture aggregations."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("governance-metrics-service")

    def posture(
        self, db: Session, tenant_id: str, period_hours: int = 720, top_n: int = 10,
        group_id: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        start, end = resolve_window(period_hours, start, end)

        # group_id scopes the request-log-backed sections (guardrail
        # enforcement + shadow usage) to one Resource Group (Design Note 002: AI
        # Systems are RGs). Discovery is a tenant-wide inventory (no per-RG
        # attribution), so it stays unscoped.
        guardrails = self._guardrails(db, tenant_id, start, top_n, group_id, end=end)
        shadow = self._shadow_ai(db, tenant_id, start, top_n, group_id, end=end)
        discovery = self._discovery(db, tenant_id, top_n)
        return {
            "period_hours": period_hours,
            "guardrails": guardrails,
            "shadow_ai": shadow,
            "discovery": discovery,
        }

    # ------------------------------------------------------------------ #
    # Discovery coverage (out-of-band discovered assets vs governed config)
    # ------------------------------------------------------------------ #
    def _discovery(self, db, tenant_id, top_n) -> Dict[str, Any]:
        """Coverage roll-up over the discovered-asset inventory — the honest
        'N AI assets seen, M governed' metric, plus the top ungoverned assets.
        Independent of the time window (inventory is cumulative)."""
        from app.database.models.discovery import (
            DiscoveredAsset, GOV_GOVERNED, GOV_UNGOVERNED, GOV_UNKNOWN, STATUS_DISMISSED,
        )
        rows = (
            db.query(DiscoveredAsset.governance_status, func.count(DiscoveredAsset.id))
            .filter(
                DiscoveredAsset.tenant_id == tenant_id,
                DiscoveredAsset.status != STATUS_DISMISSED,
            )
            .group_by(DiscoveredAsset.governance_status)
            .all()
        )
        by = {s: int(n) for s, n in rows}
        total = sum(by.values())
        governed = by.get(GOV_GOVERNED, 0)
        by_type = [
            {"asset_type": t, "count": int(n)}
            for t, n in (
                db.query(DiscoveredAsset.asset_type, func.count(DiscoveredAsset.id))
                .filter(
                    DiscoveredAsset.tenant_id == tenant_id,
                    DiscoveredAsset.status != STATUS_DISMISSED,
                )
                .group_by(DiscoveredAsset.asset_type).all()
            )
        ]
        top_ungoverned = [
            {
                "id": a.id, "asset_key": a.asset_key, "asset_type": a.asset_type,
                "provider": a.provider, "confidence": a.computed_confidence,
                "last_seen": a.last_seen.isoformat() if a.last_seen else None,
            }
            for a in (
                db.query(DiscoveredAsset)
                .filter(
                    DiscoveredAsset.tenant_id == tenant_id,
                    DiscoveredAsset.governance_status == GOV_UNGOVERNED,
                    DiscoveredAsset.status != STATUS_DISMISSED,
                )
                .order_by(DiscoveredAsset.computed_confidence.desc(),
                          DiscoveredAsset.last_seen.desc())
                .limit(top_n).all()
            )
        ]
        return {
            "total": total,
            "governed": governed,
            "ungoverned": by.get(GOV_UNGOVERNED, 0),
            "unknown": by.get(GOV_UNKNOWN, 0),
            "coverage_pct": round(governed / total * 100.0, 1) if total else 0.0,
            "by_type": by_type,
            "top_ungoverned": top_ungoverned,
        }

    # ------------------------------------------------------------------ #
    # Guardrail / policy / PII outcomes (proxy_logs, governed inline only)
    # ------------------------------------------------------------------ #
    def _guardrails(self, db, tenant_id, start, top_n, group_id=None, end=None) -> Dict[str, Any]:
        base = [ProxyLog.tenant_id == tenant_id]
        if start is not None:
            base.append(ProxyLog.timestamp >= start)
        if end is not None:
            base.append(ProxyLog.timestamp < end)
        gf = group_id_filter(ProxyLog.resource_group_id, group_id)
        if gf is not None:
            base.append(gf)

        def _count(*extra) -> int:
            return int(
                db.query(func.count(ProxyLog.id)).filter(*base, *extra).scalar() or 0
            )

        total = _count()
        blocked = _count(ProxyLog.guardrail_blocked.is_(True))
        denied = _count(ProxyLog.argument_policy_denied.is_(True))
        pii = _count(ProxyLog.pii_detected.is_(True))

        # Blocks per day for the trend chart.
        day = func.date(ProxyLog.timestamp)
        blocks_by_day = [
            {"date": str(r[0]), "blocks": int(r[1])}
            for r in (
                db.query(day, func.count(ProxyLog.id))
                .filter(*base, ProxyLog.guardrail_blocked.is_(True))
                .group_by(day)
                .order_by(day)
                .all()
            )
        ]

        # Top resource groups by blocks.
        top_blocked_groups = [
            {"group_id": r[0], "blocks": int(r[1])}
            for r in (
                db.query(ProxyLog.resource_group_id, func.count(ProxyLog.id))
                .filter(
                    *base,
                    ProxyLog.guardrail_blocked.is_(True),
                    ProxyLog.resource_group_id.isnot(None),
                )
                .group_by(ProxyLog.resource_group_id)
                .order_by(func.count(ProxyLog.id).desc())
                .limit(top_n)
                .all()
            )
        ]

        return {
            "total_requests": total,
            "blocked": blocked,
            "policy_denied": denied,
            "pii_detected": pii,
            "block_rate": round((blocked / total * 100.0), 2) if total else 0.0,
            "blocks_by_day": blocks_by_day,
            "top_blocked_groups": top_blocked_groups,
            # Honest limitation surfaced to the UI.
            "outcome_granularity": "block_only",
        }

    # ------------------------------------------------------------------ #
    # Shadow-AI / ungoverned model usage (usage_logs ledger)
    # ------------------------------------------------------------------ #
    def _shadow_ai(self, db, tenant_id, start, top_n, group_id=None, end=None) -> Dict[str, Any]:
        base = [UsageLog.tenant_id == tenant_id]
        if start is not None:
            base.append(UsageLog.created_at >= start)
        if end is not None:
            base.append(UsageLog.created_at < end)
        gf = group_id_filter(UsageLog.group_id, group_id)
        if gf is not None:
            base.append(gf)

        cost = func.coalesce(func.sum(UsageLog.estimated_cost), 0.0)
        reqs = func.count(UsageLog.id)

        # Observed (imported) traffic — never enforced inline.
        obs = (
            db.query(cost, reqs)
            .filter(*base, UsageLog.source.like("import:%"))
            .one()
        )
        observed = {"requests": int(obs[1]), "cost": float(obs[0])}

        # Un-approved model usage: ledger rows whose model is imported,
        # disabled, or not in the curated catalog. Joined by model id
        # (usage_logs.resource_id == llm_model.id).
        unapproved_rows = (
            db.query(UsageLog.model_name, cost, reqs)
            .join(LLModel, LLModel.id == UsageLog.resource_id)
            .filter(
                *base,
                UsageLog.resource_type == "llm",
                (LLModel.enabled.is_(False))
                | (LLModel.source == "import")
                | (LLModel.catalog_id.is_(None)),
            )
            .group_by(UsageLog.model_name)
            .order_by(cost.desc())
            .limit(top_n)
            .all()
        )
        unapproved_models: List[Dict[str, Any]] = [
            {"model": r[0], "cost": float(r[1]), "requests": int(r[2])}
            for r in unapproved_rows
        ]
        unapproved_requests = sum(m["requests"] for m in unapproved_models)

        return {
            "observed_requests": observed["requests"],
            "observed_cost": observed["cost"],
            "unapproved_requests": unapproved_requests,
            "unapproved_models": unapproved_models,
        }

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI Asset Factsheet service

A factsheet is a per-AI-asset governance dossier, auto-assembled from
three sources:

  * the `ai_assets` row itself (operator-set governance metadata),
  * the underlying config row (static technical metadata per kind), and
  * runtime evidence aggregated from `proxy_logs` over a time window.

The dossier has these sections:

  * asset       — the ai_assets row (to_dict)
  * config      — static metadata read off the source config model
  * usage       — request / token / cost / latency aggregates
  * guardrails  — guardrail-block / pii / arg-policy counts
  * compliance  — per-framework implicated counts (asset-scoped)
  * residency   — regions routed to + residency-class breakdown
  * access      — resource groups that can reach the asset
  * meta        — generated_at, window bounds, period_hours

# The proxy_logs join recipe

`ai_assets.ref_id` is the underlying config row id. The Rust proxy
stamps `proxy_logs.server_id` with that same config row id (except for
skills, which use the dedicated `skill_id` column). Per asset_kind the
evidence filter is:

  * llm_model     — server_id == ref_id            AND proxy_type == 'llm'
  * mcp_server    — server_id == ref_id            AND proxy_type IN ('mcp','virtual_mcp')
  * agent_card    — server_id == ref_id            AND proxy_type == 'a2a'
  * skill         — skill_id  == ref_id            (own column)
  * llm_provider  — server_id IN (member model ids) AND proxy_type == 'llm'
                    where members = LLModel.id where provider_id == ref_id
  * routing_group — server_id IN (member model ids) AND proxy_type == 'llm'
                    where members = LLModel.id where routing_group_id == ref_id

Gotchas baked in:
  * Total tokens is derived as COALESCE(prompt,0)+COALESCE(completion,0)
    — never SUM(llm_total_tokens) (NULL when completion is NULL).
  * `llm_model_used` holds the provider model NAME, not an id — never
    join on it; join on server_id.
  * An empty member set (provider/routing group with no models) yields
    zeroed evidence, not an error.

Domain errors raise `FactsheetError(code, message)`; the endpoint layer
maps `code` to an HTTP status (404 / 400).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import String, and_, func, or_, select
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_skill import AgentSkill, resource_group_agent_skills
from app.database.models.ai_asset import (
    ASSET_KIND_AGENT_CARD,
    ASSET_KIND_AGENT_IDENTITY,
    ASSET_KIND_AI_SYSTEM,
    ASSET_KIND_LLM_MODEL,
    ASSET_KIND_LLM_PROVIDER,
    ASSET_KIND_MCP_SERVER,
    ASSET_KIND_ROUTING_GROUP,
    ASSET_KIND_SKILL,
    AiAsset,
)
from app.database.models.ai_asset_factsheet import (
    AiAssetFactsheet,
    TRIGGER_MANUAL,
)
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.mcp_server import MCPServer
from app.database.models.usage_log import UsageLog
from app.constants.proxy import ProxyType
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import (
    ResourceGroup,
    resource_group_agent_cards,
    resource_group_llm_models,
    resource_group_server_configs,
)
from app.database.models.server_config import ServerConfig


# Default evidence window — 720h = 30 days. Mirrors the metrics
# 0=all-time convention.
DEFAULT_PERIOD_HOURS = 720


# Per-framework top-level tag(s) the compliance tagging engine emits.
# Kept in lockstep with app/api/v1/endpoints/admin/compliance.py's
# framework_tag_map and services/compliance/tagging.py.
FRAMEWORK_TAG_MAP: Dict[str, List[str]] = {
    "gdpr_art_30": ["gdpr_art_30"],
    "soc2": [
        "soc2.cc6.1", "soc2.cc6.6", "soc2.cc6.7",
        "soc2.cc7.1", "soc2.cc7.2",
    ],
    "hipaa": ["hipaa.phi", "hipaa.rg_marked"],
    "eu_ai_act": [
        "eu_ai_act.minimal", "eu_ai_act.limited",
        "eu_ai_act.high", "eu_ai_act.unacceptable",
    ],
    "iso_42001": ["iso_42001"],
}


class FactsheetError(Exception):
    """Service-level error. Caught by the endpoint layer and translated
    into an HTTP 4xx response with a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class FactsheetService:
    """Builds + persists per-asset factsheet dossiers."""

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("factsheet-service")

    # =====================================================================
    # Public API
    # =====================================================================

    def build_factsheet(
        self,
        db: Session,
        tenant_id: str,
        asset_id: str,
        *,
        period_hours: int = DEFAULT_PERIOD_HOURS,
    ) -> Dict[str, Any]:
        """Assemble the live factsheet dossier for an asset.

        404 if the asset is missing / cross-tenant. Empty evidence yields
        zeroed sections, never an error.
        """
        asset = self._get_asset(db, tenant_id, asset_id)

        now = datetime.now(timezone.utc)
        if period_hours and period_hours > 0:
            window_start: Optional[datetime] = now - timedelta(hours=period_hours)
        else:
            # 0 (or negative) → all-time, no lower bound.
            period_hours = 0
            window_start = None

        # The per-kind proxy_logs filter. `members` is the resolved set of
        # member model ids for provider / routing_group kinds.
        log_filter, members = self._build_log_filter(db, tenant_id, asset)

        usage = self._usage_section(db, tenant_id, log_filter, window_start, now)
        # Observed (imported) usage lives in usage_logs (source 'import:…'),
        # never in proxy_logs — the gateway was not in that path. Without this
        # merge an observed-only asset (e.g. a model discovered via Traffic
        # Data Import) shows an all-zero factsheet despite real vendor spend.
        observed_model_ids = self._observed_model_ids(asset, members)
        if observed_model_ids:
            observed = self._observed_usage(
                db, tenant_id, observed_model_ids, window_start, now
            )
            usage = self._merge_observed_usage(usage, observed)
        guardrails = self._guardrail_section(
            db, tenant_id, log_filter, window_start, now
        )
        compliance = self._compliance_section(
            db, tenant_id, log_filter, window_start, now
        )
        residency = self._residency_section(
            db, tenant_id, log_filter, window_start, now
        )
        config = self._config_section(db, tenant_id, asset, members)
        access = self._access_section(db, tenant_id, asset, members)

        return {
            "asset": asset.to_dict(),
            "config": config,
            "usage": usage,
            "guardrails": guardrails,
            "compliance": compliance,
            "residency": residency,
            "access": access,
            "meta": {
                "generated_at": now.isoformat(),
                "period_hours": period_hours,
                "window_start": window_start.isoformat() if window_start else None,
                "window_end": now.isoformat(),
            },
        }

    def capture_snapshot(
        self,
        db: Session,
        tenant_id: str,
        asset_id: str,
        *,
        period_hours: int = DEFAULT_PERIOD_HOURS,
        trigger: str = TRIGGER_MANUAL,
    ) -> AiAssetFactsheet:
        """Build the dossier and persist it as an immutable snapshot."""
        dossier = self.build_factsheet(
            db, tenant_id, asset_id, period_hours=period_hours
        )
        snapshot = AiAssetFactsheet(
            id=f"aiasset-fs-{ULID()}",
            tenant_id=tenant_id,
            asset_id=asset_id,
            # Reflect the resolved period (0 = all-time) the dossier used.
            period_hours=dossier["meta"]["period_hours"],
            snapshot_json=dossier,
            trigger=trigger,
        )
        db.add(snapshot)
        db.flush()
        self.logger.info(
            f"Captured factsheet snapshot {snapshot.id} for asset {asset_id} "
            f"in tenant {tenant_id} (trigger={trigger})"
        )
        return snapshot

    def list_snapshots(
        self,
        db: Session,
        tenant_id: str,
        asset_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[AiAssetFactsheet], int]:
        """Paginated snapshot history for an asset (newest first)."""
        # 404 if the asset doesn't exist in this tenant.
        self._get_asset(db, tenant_id, asset_id)

        where = [
            AiAssetFactsheet.tenant_id == tenant_id,
            AiAssetFactsheet.asset_id == asset_id,
        ]
        total = db.execute(
            select(func.count()).select_from(AiAssetFactsheet).where(and_(*where))
        ).scalar_one()
        rows = db.execute(
            select(AiAssetFactsheet)
            .where(and_(*where))
            .order_by(AiAssetFactsheet.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars().all()
        return list(rows), int(total or 0)

    def get_snapshot(
        self, db: Session, tenant_id: str, snapshot_id: str
    ) -> AiAssetFactsheet:
        """Tenant-scoped fetch of a single snapshot. 404 when missing."""
        snap = db.execute(
            select(AiAssetFactsheet).where(
                AiAssetFactsheet.id == snapshot_id,
                AiAssetFactsheet.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if snap is None:
            raise FactsheetError(
                "not_found",
                f"Factsheet snapshot {snapshot_id!r} not found in tenant",
            )
        return snap

    # =====================================================================
    # CSV flattener
    # =====================================================================

    @staticmethod
    def factsheet_csv_rows(
        dossier: Dict[str, Any],
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Flatten a dossier into (headers, rows) for CSV export.

        One flat row per asset — the auditor's "give me the factsheet in
        a spreadsheet" view. Nested sections are flattened into prefixed
        scalar columns; lists are joined / JSON-encoded by the CSV writer.
        """
        asset = dossier.get("asset", {}) or {}
        config = dossier.get("config", {}) or {}
        usage = dossier.get("usage", {}) or {}
        guardrails = dossier.get("guardrails", {}) or {}
        compliance = dossier.get("compliance", {}) or {}
        residency = dossier.get("residency", {}) or {}
        access = dossier.get("access", {}) or {}
        meta = dossier.get("meta", {}) or {}

        rg_names = [
            rg.get("name") for rg in (access.get("resource_groups") or [])
        ]

        headers = [
            "asset_id",
            "asset_kind",
            "ref_id",
            "display_name",
            "owner",
            "lifecycle_stage",
            "risk_tier",
            "enforcement_mode",
            "status",
            "config_enabled",
            "requests",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "success_count",
            "error_count",
            "error_rate",
            "avg_duration_ms",
            "observed_requests",
            "observed_cost_usd",
            "first_seen",
            "last_seen",
            "guardrail_blocked",
            "pii_detected",
            "argument_policy_denied",
            "compliance_frameworks",
            "regions_routed_to",
            "resource_groups",
            "period_hours",
            "window_start",
            "window_end",
            "generated_at",
        ]

        # Implicated frameworks = those with a non-zero count.
        per_fw = compliance.get("frameworks", {}) or {}
        implicated = sorted(k for k, v in per_fw.items() if (v or 0) > 0)

        row = {
            "asset_id": asset.get("id"),
            "asset_kind": asset.get("asset_kind"),
            "ref_id": asset.get("ref_id"),
            "display_name": asset.get("display_name"),
            "owner": asset.get("owner"),
            "lifecycle_stage": asset.get("lifecycle_stage"),
            "risk_tier": asset.get("risk_tier"),
            "enforcement_mode": asset.get("enforcement_mode"),
            "status": asset.get("status"),
            "config_enabled": config.get("enabled"),
            "requests": usage.get("requests"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "success_count": usage.get("success_count"),
            "error_count": usage.get("error_count"),
            "error_rate": usage.get("error_rate"),
            "avg_duration_ms": usage.get("avg_duration_ms"),
            "observed_requests": usage.get("observed_requests", 0),
            "observed_cost_usd": usage.get("observed_cost_usd", 0.0),
            "first_seen": usage.get("first_seen"),
            "last_seen": usage.get("last_seen"),
            "guardrail_blocked": guardrails.get("guardrail_blocked"),
            "pii_detected": guardrails.get("pii_detected"),
            "argument_policy_denied": guardrails.get("argument_policy_denied"),
            "compliance_frameworks": implicated,
            "regions_routed_to": residency.get("regions_routed_to") or [],
            "resource_groups": rg_names,
            "period_hours": meta.get("period_hours"),
            "window_start": meta.get("window_start"),
            "window_end": meta.get("window_end"),
            "generated_at": meta.get("generated_at"),
        }
        return headers, [row]

    # =====================================================================
    # Internals — asset + per-kind log filter
    # =====================================================================

    def _get_asset(self, db: Session, tenant_id: str, asset_id: str) -> AiAsset:
        asset = db.execute(
            select(AiAsset).where(
                AiAsset.id == asset_id,
                AiAsset.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if asset is None:
            raise FactsheetError(
                "not_found",
                f"AI asset {asset_id!r} not found in tenant",
            )
        return asset

    def _resolve_members(
        self, db: Session, tenant_id: str, asset: AiAsset
    ) -> List[str]:
        """Member model ids for provider / routing_group kinds."""
        if asset.asset_kind == ASSET_KIND_LLM_PROVIDER:
            col = LLModel.provider_id
        elif asset.asset_kind == ASSET_KIND_ROUTING_GROUP:
            col = LLModel.routing_group_id
        else:
            return []
        rows = db.execute(
            select(LLModel.id).where(
                LLModel.tenant_id == tenant_id,
                col == asset.ref_id,
            )
        ).scalars().all()
        return [str(r) for r in rows]

    def _build_log_filter(
        self, db: Session, tenant_id: str, asset: AiAsset
    ) -> Tuple[Any, List[str]]:
        """Build the SQLAlchemy boolean expression isolating this asset's
        proxy_logs rows. Returns (filter_expr, member_model_ids).

        A None filter means "no rows can match" (e.g. empty member set) —
        callers short-circuit to zeroed evidence.
        """
        kind = asset.asset_kind
        ref_id = asset.ref_id
        members: List[str] = []

        if kind == ASSET_KIND_LLM_MODEL:
            return and_(
                ProxyLog.server_id == ref_id,
                ProxyLog.proxy_type == "llm",
            ), members

        if kind == ASSET_KIND_MCP_SERVER:
            return and_(
                ProxyLog.server_id == ref_id,
                ProxyLog.proxy_type.in_([ProxyType.MCP.value, ProxyType.VIRTUAL_MCP.value]),
            ), members

        if kind == ASSET_KIND_AGENT_CARD:
            return and_(
                ProxyLog.server_id == ref_id,
                ProxyLog.proxy_type == "a2a",
            ), members

        if kind == ASSET_KIND_SKILL:
            return (ProxyLog.skill_id == ref_id), members

        if kind == ASSET_KIND_AI_SYSTEM:
            # The AI System is a resource group; its evidence is every governed
            # call attributed to that group, across ALL surfaces (LLM, MCP,
            # A2A, skills) — the same combined picture as the RG Usage tab.
            return (ProxyLog.resource_group_id == ref_id), members

        if kind == ASSET_KIND_AGENT_IDENTITY:
            # The agent principal's evidence is every call it made, across
            # every surface, keyed by the actor agent id.
            return (ProxyLog.actor_agent_id == ref_id), members

        if kind in (ASSET_KIND_LLM_PROVIDER, ASSET_KIND_ROUTING_GROUP):
            members = self._resolve_members(db, tenant_id, asset)
            if not members:
                # Empty member set → no evidence (not an error).
                return None, members
            return and_(
                ProxyLog.server_id.in_(members),
                ProxyLog.proxy_type == "llm",
            ), members

        # Unknown kind — defensive; the model @validates already gates this.
        return None, members

    def _base_where(
        self,
        tenant_id: str,
        log_filter: Any,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> List[Any]:
        where: List[Any] = [
            ProxyLog.tenant_id == tenant_id,
            log_filter,
            ProxyLog.timestamp <= window_end,
        ]
        if window_start is not None:
            where.append(ProxyLog.timestamp >= window_start)
        return where

    # =====================================================================
    # Internals — sections
    # =====================================================================

    @staticmethod
    def _empty_usage() -> Dict[str, Any]:
        return {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "success_count": 0,
            "error_count": 0,
            "error_rate": 0.0,
            "avg_duration_ms": 0.0,
            "first_seen": None,
            "last_seen": None,
            "observed_requests": 0,
            "observed_cost_usd": 0.0,
        }

    @staticmethod
    def _observed_model_ids(asset: AiAsset, members: List[str]) -> List[str]:
        """LLM-model ids whose imported (observed) usage attributes to this
        asset: the model itself, or the member set for provider /
        routing-group assets. Non-model kinds have no observed plane."""
        if asset.asset_kind == ASSET_KIND_LLM_MODEL:
            return [asset.ref_id]
        if asset.asset_kind in (ASSET_KIND_LLM_PROVIDER, ASSET_KIND_ROUTING_GROUP):
            return members
        return []

    def _observed_usage(
        self,
        db: Session,
        tenant_id: str,
        model_ids: List[str],
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> Dict[str, Any]:
        """Aggregate imported (observed) usage_logs for the given model ids.

        Imported rows carry source 'import:<api>', resource_type 'llm' and
        resource_id = the Brutor llm_model id the identity resolver mapped
        the vendor model to; created_at is the vendor bucket start. The
        source LIKE filter excludes governed gateway rows, so this never
        double-counts traffic already aggregated from proxy_logs.
        """
        where: List[Any] = [
            UsageLog.tenant_id == tenant_id,
            UsageLog.source.like("import:%"),
            UsageLog.resource_type == "llm",
            UsageLog.resource_id.in_(model_ids),
            UsageLog.created_at <= window_end,
        ]
        if window_start is not None:
            where.append(UsageLog.created_at >= window_start)

        row = db.execute(
            select(
                func.count().label("requests"),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                func.coalesce(func.sum(UsageLog.estimated_cost), 0.0),
                func.min(UsageLog.created_at),
                func.max(UsageLog.created_at),
            ).where(and_(*where))
        ).first()

        errors = db.execute(
            select(func.count()).select_from(UsageLog).where(
                and_(*where, UsageLog.status_code >= 400)
            )
        ).scalar_one()

        return {
            "requests": int(row[0] or 0),
            "prompt_tokens": int(row[1] or 0),
            "completion_tokens": int(row[2] or 0),
            "estimated_cost_usd": float(row[3] or 0.0),
            "first_seen": row[4],
            "last_seen": row[5],
            "error_count": int(errors or 0),
        }

    @staticmethod
    def _merge_observed_usage(
        usage: Dict[str, Any], observed: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Fold observed (imported) usage into the governed usage section.

        Totals span both planes; the observed split stays visible via
        observed_requests / observed_cost_usd. avg_duration_ms remains
        governed-only — imported vendor aggregates carry no latency.
        """
        obs_requests = observed["requests"]
        usage["observed_requests"] = obs_requests
        usage["observed_cost_usd"] = round(observed["estimated_cost_usd"], 6)
        if not obs_requests:
            return usage

        usage["requests"] += obs_requests
        usage["prompt_tokens"] += observed["prompt_tokens"]
        usage["completion_tokens"] += observed["completion_tokens"]
        usage["total_tokens"] += (
            observed["prompt_tokens"] + observed["completion_tokens"]
        )
        usage["estimated_cost_usd"] = round(
            usage["estimated_cost_usd"] + observed["estimated_cost_usd"], 6
        )
        obs_errors = observed["error_count"]
        usage["success_count"] += obs_requests - obs_errors
        usage["error_count"] += obs_errors
        usage["error_rate"] = (
            round(usage["error_count"] / usage["requests"], 4)
            if usage["requests"]
            else 0.0
        )

        # first/last seen span both planes (ISO strings on the governed side).
        for key, pick in (("first_seen", min), ("last_seen", max)):
            obs_ts = observed[key]
            if obs_ts is None:
                continue
            obs_iso = obs_ts.isoformat()
            cur = usage[key]
            usage[key] = obs_iso if cur is None else pick(cur, obs_iso)
        return usage

    def _usage_section(
        self,
        db: Session,
        tenant_id: str,
        log_filter: Any,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> Dict[str, Any]:
        if log_filter is None:
            return self._empty_usage()

        where = self._base_where(tenant_id, log_filter, window_start, window_end)

        # Single aggregate pass. Total tokens derived per the gotcha:
        # COALESCE(prompt,0)+COALESCE(completion,0), never SUM(total).
        row = db.execute(
            select(
                func.count().label("requests"),
                func.coalesce(func.sum(ProxyLog.llm_prompt_tokens), 0),
                func.coalesce(func.sum(ProxyLog.llm_completion_tokens), 0),
                func.coalesce(
                    func.sum(
                        func.coalesce(ProxyLog.llm_prompt_tokens, 0)
                        + func.coalesce(ProxyLog.llm_completion_tokens, 0)
                    ),
                    0,
                ),
                func.coalesce(func.sum(ProxyLog.llm_estimated_cost_usd), 0.0),
                func.avg(ProxyLog.duration_ms),
                func.min(ProxyLog.timestamp),
                func.max(ProxyLog.timestamp),
            ).where(and_(*where))
        ).first()

        requests = int(row[0] or 0)
        # success counting via a portable count() so we don't rely on the
        # boolean->int cast across dialects.
        success_count = db.execute(
            select(func.count()).select_from(ProxyLog).where(
                and_(*where, ProxyLog.success == True)  # noqa: E712
            )
        ).scalar_one()
        success_count = int(success_count or 0)
        error_count = requests - success_count

        prompt_tokens = int(row[1] or 0)
        completion_tokens = int(row[2] or 0)
        total_tokens = int(row[3] or 0)
        estimated_cost = float(row[4] or 0.0)
        avg_duration = float(row[5]) if row[5] is not None else 0.0
        first_seen = row[6]
        last_seen = row[7]

        return {
            "requests": requests,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "success_count": success_count,
            "error_count": error_count,
            "error_rate": round(error_count / requests, 4) if requests else 0.0,
            "avg_duration_ms": round(avg_duration, 2),
            "first_seen": first_seen.isoformat() if first_seen else None,
            "last_seen": last_seen.isoformat() if last_seen else None,
        }

    def _guardrail_section(
        self,
        db: Session,
        tenant_id: str,
        log_filter: Any,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> Dict[str, Any]:
        empty = {
            "guardrail_blocked": 0,
            "pii_detected": 0,
            "argument_policy_denied": 0,
        }
        if log_filter is None:
            return empty
        where = self._base_where(tenant_id, log_filter, window_start, window_end)

        def _count(flag_col) -> int:
            c = db.execute(
                select(func.count()).select_from(ProxyLog).where(
                    and_(*where, flag_col == True)  # noqa: E712
                )
            ).scalar_one()
            return int(c or 0)

        return {
            "guardrail_blocked": _count(ProxyLog.guardrail_blocked),
            "pii_detected": _count(ProxyLog.pii_detected),
            "argument_policy_denied": _count(ProxyLog.argument_policy_denied),
        }

    def _compliance_section(
        self,
        db: Session,
        tenant_id: str,
        log_filter: Any,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> Dict[str, Any]:
        """Per-framework implicated counts, asset-scoped.

        Replicates compliance.py's JSONB containment count but ALSO
        filters by the asset's proxy_logs filter. A row counts toward a
        framework when its compliance_tags array contains ANY of that
        framework's tags (a row with two of a framework's tags still
        counts once per framework, mirroring the dashboard's max-over-tags
        semantics)."""
        per_framework = {fw: 0 for fw in FRAMEWORK_TAG_MAP}
        if log_filter is None:
            return {"frameworks": per_framework}

        where = self._base_where(tenant_id, log_filter, window_start, window_end)

        for fw, tags in FRAMEWORK_TAG_MAP.items():
            # OR the per-tag containment so a row counts once for the
            # framework even if it carries several of its tags.
            ors = [self._tag_contains(tag) for tag in tags]
            count = db.execute(
                select(func.count()).select_from(ProxyLog).where(
                    and_(*where, or_(*ors))
                )
            ).scalar_one()
            per_framework[fw] = int(count or 0)

        return {"frameworks": per_framework}

    @staticmethod
    def _tag_contains(tag: str):
        """Dialect-portable "compliance_tags contains `tag`".

        On Postgres this is the GIN-indexed JSONB `@>` containment; on
        SQLite (the test harness) JSONB degrades to TEXT, so we fall back
        to a LIKE on the serialized array. We express it portably with a
        LIKE on the textual form, which both back-ends satisfy for the
        tag-membership question."""
        # The stored shape is a JSON array of strings. A quoted-tag
        # substring match is sufficient to test membership for both
        # Postgres (text-cast) and SQLite. Tags are interned identifiers
        # (no embedded quotes), so this is unambiguous.
        needle = f'%"{tag}"%'
        return func.cast(ProxyLog.compliance_tags, String).like(needle)

    def _residency_section(
        self,
        db: Session,
        tenant_id: str,
        log_filter: Any,
        window_start: Optional[datetime],
        window_end: datetime,
    ) -> Dict[str, Any]:
        empty = {"regions_routed_to": [], "residency_class_counts": {}}
        if log_filter is None:
            return empty
        where = self._base_where(tenant_id, log_filter, window_start, window_end)

        region_rows = db.execute(
            select(ProxyLog.region_routed_to)
            .where(and_(*where, ProxyLog.region_routed_to.isnot(None)))
            .distinct()
        ).scalars().all()
        regions = sorted({str(r) for r in region_rows if r})

        class_rows = db.execute(
            select(ProxyLog.residency_class, func.count())
            .where(and_(*where, ProxyLog.residency_class.isnot(None)))
            .group_by(ProxyLog.residency_class)
        ).all()
        class_counts = {str(c[0]): int(c[1] or 0) for c in class_rows if c[0]}

        return {
            "regions_routed_to": regions,
            "residency_class_counts": class_counts,
        }

    # =====================================================================
    # Internals — static config per kind
    # =====================================================================

    def _config_section(
        self, db: Session, tenant_id: str, asset: AiAsset, members: List[str]
    ) -> Dict[str, Any]:
        kind = asset.asset_kind
        ref_id = asset.ref_id

        if kind == ASSET_KIND_LLM_MODEL:
            m = db.get(LLModel, ref_id)
            if not m or m.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "name": m.name,
                "model_name": m.model_name,
                "provider_id": m.provider_id,
                "mode": m.mode,
                "routing_group_id": m.routing_group_id,
                "enabled": bool(m.enabled),
            }

        if kind == ASSET_KIND_LLM_PROVIDER:
            p = db.get(LlmProvider, ref_id)
            if not p or p.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "key": p.key,
                "display_name": p.display_name,
                "category": p.category,
                "region": p.region,
                "enabled": bool(p.enabled),
                "member_model_count": len(members),
            }

        if kind == ASSET_KIND_ROUTING_GROUP:
            rg = db.get(LLMRoutingGroup, ref_id)
            if not rg or rg.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "name": rg.name,
                "routing_strategy": rg.routing_strategy,
                "mode": rg.mode,
                "member_model_count": len(members),
            }

        if kind == ASSET_KIND_MCP_SERVER:
            s = db.get(MCPServer, ref_id)
            if not s or s.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "name": s.name,
                "description": s.description,
                "base_url": s.base_url,
                "region": s.region,
                "enabled": bool(s.enabled),
            }

        if kind == ASSET_KIND_SKILL:
            sk = db.get(AgentSkill, ref_id)
            if not sk or sk.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "name": sk.name,
                "description": sk.description,
                "version": sk.version,
                "status": self._enum_value(sk.status),
                "enabled": bool(sk.enabled),
            }

        if kind == ASSET_KIND_AGENT_CARD:
            c = db.get(AgentCard, ref_id)
            if not c or c.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "name": c.name,
                "version": c.version,
                "description": c.description,
                "trust_tier": c.trust_tier,
                "enabled": bool(c.enabled),
            }

        if kind == ASSET_KIND_AI_SYSTEM:
            from app.database.models.resource_group import ResourceGroup
            g = db.get(ResourceGroup, ref_id)
            if not g or g.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "name": g.display_name or g.name,
                "group_type": g.group_type,
                "eu_ai_act_risk_tier": g.eu_ai_act_risk_tier,
                "enabled": bool(getattr(g, "is_active", True)),
            }

        if kind == ASSET_KIND_AGENT_IDENTITY:
            from app.database.models.agent_identity import AgentIdentity
            a = db.get(AgentIdentity, ref_id)
            if not a or a.tenant_id != tenant_id:
                return self._missing_config()
            return {
                "name": a.name,
                "owner_label": a.owner_label,
                "enforcement_mode": a.enforcement_mode,
                "default_deny": bool(a.default_deny),
                "enabled": bool(a.enabled),
            }

        return self._missing_config()

    @staticmethod
    def _missing_config() -> Dict[str, Any]:
        """The underlying config row vanished (orphaned asset). Surface a
        marker rather than erroring — the registry already flags orphans."""
        return {"enabled": None, "orphaned": True}

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return getattr(value, "value", value)

    # =====================================================================
    # Internals — access scope (resource groups that can reach the asset)
    # =====================================================================

    def _access_section(
        self, db: Session, tenant_id: str, asset: AiAsset, members: List[str]
    ) -> Dict[str, Any]:
        rg_ids = self._access_rg_ids(db, tenant_id, asset, members)
        if not rg_ids:
            return {"resource_groups": []}

        rows = db.execute(
            select(ResourceGroup.id, ResourceGroup.name).where(
                ResourceGroup.tenant_id == tenant_id,
                ResourceGroup.id.in_(rg_ids),
            )
        ).all()
        groups = [{"id": r[0], "name": r[1]} for r in rows]
        groups.sort(key=lambda g: (g["name"] or "", g["id"]))
        return {"resource_groups": groups}

    def _access_rg_ids(
        self, db: Session, tenant_id: str, asset: AiAsset, members: List[str]
    ) -> List[str]:
        kind = asset.asset_kind
        ref_id = asset.ref_id

        if kind == ASSET_KIND_LLM_MODEL:
            return self._rg_ids_for_models(db, [ref_id])

        if kind in (ASSET_KIND_LLM_PROVIDER, ASSET_KIND_ROUTING_GROUP):
            return self._rg_ids_for_models(db, members)

        if kind == ASSET_KIND_MCP_SERVER:
            # The binding table references server_configs.id; resolve the
            # ServerConfig rows backing this MCP server first.
            config_ids = db.execute(
                select(ServerConfig.id).where(
                    ServerConfig.tenant_id == tenant_id,
                    ServerConfig.mcp_server_id == ref_id,
                )
            ).scalars().all()
            if not config_ids:
                return []
            rows = db.execute(
                select(resource_group_server_configs.c.resource_group_id)
                .where(
                    resource_group_server_configs.c.server_config_id.in_(
                        list(config_ids)
                    )
                )
                .distinct()
            ).scalars().all()
            return [str(r) for r in rows]

        if kind == ASSET_KIND_SKILL:
            rows = db.execute(
                select(resource_group_agent_skills.c.resource_group_id)
                .where(resource_group_agent_skills.c.agent_skill_id == ref_id)
                .distinct()
            ).scalars().all()
            return [str(r) for r in rows]

        if kind == ASSET_KIND_AGENT_CARD:
            rows = db.execute(
                select(resource_group_agent_cards.c.resource_group_id)
                .where(resource_group_agent_cards.c.agent_card_id == ref_id)
                .distinct()
            ).scalars().all()
            return [str(r) for r in rows]

        return []

    @staticmethod
    def _rg_ids_for_models(db: Session, model_ids: List[str]) -> List[str]:
        if not model_ids:
            return []
        rows = db.execute(
            select(resource_group_llm_models.c.resource_group_id)
            .where(resource_group_llm_models.c.llm_model_id.in_(model_ids))
            .distinct()
        ).scalars().all()
        return [str(r) for r in rows]


__all__ = [
    "FactsheetService",
    "FactsheetError",
    "DEFAULT_PERIOD_HOURS",
    "FRAMEWORK_TAG_MAP",
]

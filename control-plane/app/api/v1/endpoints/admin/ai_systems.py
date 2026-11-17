# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System fleet board

`GET /v1/admin/ai-systems` — every AI System in one place.

An AI System is a Resource Group with `group_type='ai_system'`,
so this router is a *projection* over `resource_groups`, not a new store.
It exists as its own surface because the question "what AI Systems do we
run, who owns them, and which are undocumented?" is asked by a different
person, at a different cadence, than "how is my resource-group hierarchy
laid out".

Each row carries identity, kind, governance attributes, composition counts
and the governance-gap rollup; the health / drift / liveness columns come
from the run ledger as extra fields of the same row.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models.ai_system_run import AISystemRun
from app.services.ai_system_lifecycle_gate import (
    DEFAULT_RETIREMENT_QUIET_DAYS,
    LIFECYCLE_DEPRECATED,
)

from app.api.v1.models.admin.ai_system import (
    AISystemComposition,
    AISystemFleetResponse,
    AISystemFleetStats,
    AISystemLiveness,
    AISystemMembers,
    AISystemSummary,
)
from app.services.ai_system_composition import (
    DEFAULT_COMPOSITION_WINDOW_HOURS,
    ROLE_FILTERS,
    CompositionIndex,
)
from app.services.ai_system_card_facts import (
    DEFAULT_CARD_WINDOW_HOURS,
    card_facts,
    reachable_resources,
)
from app.services.ai_system_liveness_state import liveness_states
from app.core.admin_user.dependencies import (
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.agent_identity import ResourceGroupAgentMember
from app.database.models.resource_group import (
    SYSTEM_KINDS,
    GroupType,
    ResourceGroup,
)

# Attributes an AI System should carry before anyone tries to assure it.
# Ordered for stable output — the UI renders these as chips.
GOVERNANCE_ATTRIBUTES = (
    "owner",
    "intended_use",
    "lifecycle_stage",
    "eu_ai_act_risk_tier",
)


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _governance_gaps(group: ResourceGroup) -> List[str]:
    """Which governance attributes this AI System has not filled in."""
    return [attr for attr in GOVERNANCE_ATTRIBUTES if not getattr(group, attr, None)]


def create_ai_systems_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/ai-systems", tags=["AI Systems"])

    @router.get(
        "",
        response_model=AISystemFleetResponse,
        summary="List AI Systems (fleet board)",
        description=(
            "Every Resource Group with group_type='ai_system' in the tenant, "
            "with its kind, governance attributes, composition counts and "
            "governance gaps."
        ),
    )
    def list_ai_systems(
        tenant_id: Optional[str] = Query(None, description="Required for system admins"),
        system_kind: Optional[str] = Query(None, description="Filter by kind"),
        lifecycle_stage: Optional[str] = Query(None, description="Filter by lifecycle stage"),
        risk_tier: Optional[str] = Query(None, description="Filter by EU AI Act risk tier"),
        owner: Optional[str] = Query(None, description="Filter by owner (exact match)"),
        undocumented_only: bool = Query(
            False,
            description=(
                "Only systems with a governance gap: an unset governance "
                "attribute or an open composition finding"
            ),
        ),
        role: Optional[str] = Query(
            None,
            description=(
                "root | shared_service | external_deps. root and "
                "shared_service both include root_and_service systems."
            ),
        ),
        window_hours: int = Query(
            DEFAULT_COMPOSITION_WINDOW_HOURS, ge=1, le=24 * 366,
            description="Window for observed composition (callers, dependency health, findings)",
        ),
        include_inactive: bool = Query(False, description="Include is_active=false systems"),
        include_health: bool = Query(
            False,
            description=(
                "Attach the materialised assurance rollup (health_state, "
                "health_score, open_drift_events, health_computed_at) to each "
                "row — one grouped read of ai_system_health, no recompute. "
                "Systems without a computed row keep nulls: render as "
                "unknown, never as healthy."
            ),
        ),
        include_card: bool = Query(
            False,
            description=(
                "Attach the gallery card facts to each row: agent identity, "
                "implementation and contract; effective gateway policies, "
                "guardrails and limits; resources; and observability / "
                "evaluation KPIs over `card_window_hours`. Batched per page."
            ),
        ),
        card_window_hours: int = Query(
            DEFAULT_CARD_WINDOW_HOURS, ge=1, le=24 * 366,
            description="Window for the card's KPIs (traces, latency, cost, feedback)",
        ),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> AISystemFleetResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)

        if role is not None and role not in ROLE_FILTERS:
            raise HTTPException(
                status_code=422,
                detail=f"role must be one of {list(ROLE_FILTERS)}",
            )
        if system_kind is not None and system_kind not in SYSTEM_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"system_kind must be one of {sorted(SYSTEM_KINDS)}",
            )

        query = db.query(ResourceGroup).filter(
            ResourceGroup.tenant_id == resolved_tenant,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
        if not include_inactive:
            query = query.filter(ResourceGroup.is_active.is_(True))
        if system_kind is not None:
            query = query.filter(ResourceGroup.system_kind == system_kind)
        if lifecycle_stage is not None:
            query = query.filter(ResourceGroup.lifecycle_stage == lifecycle_stage)
        if risk_tier is not None:
            query = query.filter(ResourceGroup.eu_ai_act_risk_tier == risk_tier)
        if owner is not None:
            query = query.filter(ResourceGroup.owner == owner)

        # `undocumented_only` filters on a computed property, so it cannot be
        # pushed into SQL without duplicating GOVERNANCE_ATTRIBUTES as a
        # column list that would silently drift from the tuple above. The
        # fleet of AI Systems in a tenant is small (tens, not millions), so
        # the stats pass below already materialises them all.
        all_matching = query.order_by(
            ResourceGroup.hierarchy_path.asc(), ResourceGroup.name.asc()
        ).all()
        # One tenant-wide composition pass feeds the role filter,
        # the governance-gap filter and every row's composition fields, so a
        # row's finding count and the Composition tab's list are one result.
        index = CompositionIndex(db, resolved_tenant, window_hours=window_hours)
        if role is not None:
            all_matching = [g for g in all_matching if index.matches_role_filter(g.id, role)]
        if undocumented_only:
            all_matching = [
                g for g in all_matching
                if _governance_gaps(g) or index.composition_finding_count(g.id)
            ]

        total = len(all_matching)
        page = all_matching[offset : offset + limit]
        page_ids = [g.id for g in page]
        # The full summary only for the rows on the page: the filters above
        # need a lookup each, the summary's dependency health and findings
        # are per row shown (performance: tests/test_composition_performance_pg.py).
        summaries_by_id = {g.id: index.summary(g.id) for g in page}

        # Composition counts. Models / MCP servers / skills are what the
        # system can REACH (own + inherited, enabled, platform VMCPs left
        # out) — the same function the gallery card reads, so the table and
        # the card can never disagree. Agent members are the system's own.
        reachable = reachable_resources(db, resolved_tenant, page_ids)

        agent_counts: Dict[str, int] = {}
        if page_ids:
            agent_rows = (
                db.query(ResourceGroupAgentMember.group_id, func.count())
                .filter(
                    ResourceGroupAgentMember.tenant_id == resolved_tenant,
                    ResourceGroupAgentMember.group_id.in_(page_ids),
                )
                .group_by(ResourceGroupAgentMember.group_id)
                .all()
            )
            agent_counts = {gid: count for gid, count in agent_rows}

        child_counts: Dict[str, int] = {}
        if page_ids:
            child_rows = (
                db.query(ResourceGroup.parent_group_id, func.count())
                .filter(
                    ResourceGroup.tenant_id == resolved_tenant,
                    ResourceGroup.parent_group_id.in_(page_ids),
                )
                .group_by(ResourceGroup.parent_group_id)
                .all()
            )
            child_counts = {gid: count for gid, count in child_rows}

        # The one liveness function, batched over the page.
        live = liveness_states(db, resolved_tenant, page)

        # Assurance rollup — read the materialised rows in one query. A
        # missing row stays None on the summary (render as unknown): a fleet
        # board that recomputed health per row would be an N-system fan-out,
        # and one that defaulted missing rows to healthy would be lying.
        health_map: Dict[str, object] = {}
        if include_health and page_ids:
            from app.database.models.ai_system_health import AISystemHealth

            for h in (
                db.query(
                    AISystemHealth.ai_system_id,
                    AISystemHealth.health_state,
                    AISystemHealth.health_score,
                    AISystemHealth.open_drift_events,
                    AISystemHealth.computed_at,
                    AISystemHealth.verdict_status,
                )
                .filter(
                    AISystemHealth.tenant_id == resolved_tenant,
                    AISystemHealth.ai_system_id.in_(page_ids),
                )
                .all()
            ):
                health_map[h[0]] = h

        # Retirement readiness, for deprecated systems on this page only.
        # One grouped query rather than a readiness call per row — the
        # fleet board is the place an N+1 would actually be felt.
        retire_map: Dict[str, tuple] = {}
        deprecated_ids = [
            g.id for g in page if g.lifecycle_stage == LIFECYCLE_DEPRECATED
        ]
        if deprecated_ids:
            from datetime import datetime, timedelta, timezone

            now = datetime.now(timezone.utc)
            window = timedelta(days=DEFAULT_RETIREMENT_QUIET_DAYS)
            last_runs = dict(
                db.query(
                    AISystemRun.ai_system_id,
                    func.max(AISystemRun.started_at),
                )
                .filter(
                    AISystemRun.tenant_id == resolved_tenant,
                    AISystemRun.ai_system_id.in_(deprecated_ids),
                )
                .group_by(AISystemRun.ai_system_id)
                .all()
            )
            for sid in deprecated_ids:
                last = last_runs.get(sid)
                if last is None:
                    # Never ran, so nothing breaks by retiring it.
                    retire_map[sid] = (True, None)
                    continue
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                at = last + window
                retire_map[sid] = (at <= now, at.isoformat())

        cards = (
            card_facts(
                db,
                resolved_tenant,
                page,
                window_hours=card_window_hours,
                resources=reachable,
            )
            if include_card
            else {}
        )

        summaries = [
            AISystemSummary(
                id=g.id,
                name=g.name,
                display_name=g.display_name,
                description=g.description,
                system_kind=g.system_kind,
                owner=g.owner,
                intended_use=g.intended_use,
                intended_client_labels=g.intended_client_labels,
                intended_client_systems=g.intended_client_systems,
                lifecycle_stage=g.lifecycle_stage,
                eu_ai_act_risk_tier=g.eu_ai_act_risk_tier,
                eu_ai_act_role=g.eu_ai_act_role,
                parent_group_id=g.parent_group_id,
                hierarchy_level=g.hierarchy_level or 0,
                hierarchy_path=g.hierarchy_path,
                is_active=g.is_active,
                composition=AISystemComposition(
                    members=AISystemMembers(
                        agents=agent_counts.get(g.id, 0),
                        models=reachable[g.id].models,
                        mcp_servers=reachable[g.id].mcp_servers,
                        skills=reachable[g.id].skills,
                    ),
                    depends_on=summaries_by_id[g.id]["depends_on"],
                    worst_dependency_health=summaries_by_id[g.id]["worst_dependency_health"],
                    used_by_declared=summaries_by_id[g.id]["used_by_declared"],
                    used_by_observed=summaries_by_id[g.id]["used_by_observed"],
                    idle=summaries_by_id[g.id]["idle"],
                    child_groups=child_counts.get(g.id, 0),
                ),
                role=summaries_by_id[g.id]["role"],
                has_external_dependencies=summaries_by_id[g.id]["has_external_dependencies"],
                risk_tier_effective=summaries_by_id[g.id]["risk_tier_effective"],
                effective_tier_sources=summaries_by_id[g.id]["effective_tier_sources"],
                separability_declared=summaries_by_id[g.id]["separability_declared"],
                separability_sealed=summaries_by_id[g.id]["separability_sealed"],
                enforce_accepted_callers=summaries_by_id[g.id]["enforce_accepted_callers"],
                composition_finding_count=summaries_by_id[g.id]["composition_finding_count"],
                liveness=AISystemLiveness(
                    state=live[g.id].state, reason=live[g.id].reason
                ),
                governance_gaps=_governance_gaps(g),
                health_state=(health_map[g.id][1] if g.id in health_map else None),
                health_score=(health_map[g.id][2] if g.id in health_map else None),
                open_drift_events=(health_map[g.id][3] if g.id in health_map else None),
                health_computed_at=(
                    health_map[g.id][4].isoformat()
                    if g.id in health_map and health_map[g.id][4]
                    else None
                ),
                verdict_status=(health_map[g.id][5] if g.id in health_map else None),
                retirable=(retire_map[g.id][0] if g.id in retire_map else None),
                retirable_at=(retire_map[g.id][1] if g.id in retire_map else None),
                card=cards.get(g.id),
                created_at=g.created_at.isoformat() if g.created_at else None,
                updated_at=g.updated_at.isoformat() if g.updated_at else None,
            )
            for g in page
        ]

        # Stats span the whole filtered fleet, not just the page — an
        # operator paging through 3 of 40 systems still needs "12 have no
        # owner" to mean 12 of 40.
        by_kind: Dict[str, int] = {}
        by_stage: Dict[str, int] = {}
        by_tier: Dict[str, int] = {}
        without_owner = 0
        without_intended_use = 0
        fully_documented = 0
        for g in all_matching:
            by_kind[g.system_kind or "unset"] = by_kind.get(g.system_kind or "unset", 0) + 1
            by_stage[g.lifecycle_stage or "unset"] = (
                by_stage.get(g.lifecycle_stage or "unset", 0) + 1
            )
            by_tier[g.eu_ai_act_risk_tier or "unset"] = (
                by_tier.get(g.eu_ai_act_risk_tier or "unset", 0) + 1
            )
            if not g.owner:
                without_owner += 1
            if not g.intended_use:
                without_intended_use += 1
            if not _governance_gaps(g):
                fully_documented += 1

        logger.debug(
            "AI System fleet board served",
            context={
                "tenant_id": resolved_tenant,
                "total": total,
                "returned": len(summaries),
            },
        )

        return AISystemFleetResponse(
            systems=summaries,
            stats=AISystemFleetStats(
                total=total,
                by_kind=by_kind,
                by_lifecycle_stage=by_stage,
                by_risk_tier=by_tier,
                without_owner=without_owner,
                without_intended_use=without_intended_use,
                fully_documented=fully_documented,
            ),
            total=total,
            limit=limit,
            offset=offset,
        )


    @router.get(
        "/{ai_system_id}/statement",
        summary="Monthly per-system statement (FinOps + assurance)",
        description=(
            "One calendar month of one AI System, assembled for the system's "
            "owner: identity & lifecycle, cost (totals, by model, by user), "
            "cost avoided, the run ledger's task economics, and the "
            "materialised health verdict. The artifact a system owner "
            "forwards — and the number set a budget review opens with."
        ),
    )
    def system_statement(
        ai_system_id: str,
        month: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="Calendar month, YYYY-MM (UTC)"),
        tenant_id: Optional[str] = Query(None, description="Required for system admins"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> Dict:
        import calendar as _cal
        from datetime import datetime, timezone

        from app.database.models.ai_system_health import AISystemHealth
        from app.services.cost_metrics_service import CostMetricsService
        from app.services.run_signals import run_signal_service

        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        system = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.id == ai_system_id,
                ResourceGroup.tenant_id == resolved,
                ResourceGroup.group_type == GroupType.AI_SYSTEM,
            )
            .first()
        )
        if system is None:
            raise HTTPException(status_code=404, detail="AI System not found")

        y, m = (int(x) for x in month.split("-"))
        if not 1 <= m <= 12:
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        win_start = datetime(y, m, 1, tzinfo=timezone.utc)
        win_end = (datetime(y + 1, 1, 1, tzinfo=timezone.utc) if m == 12
                   else datetime(y, m + 1, 1, tzinfo=timezone.utc))

        cost_service = CostMetricsService()
        cost = cost_service.breakdown(
            db, tenant_id=resolved, period_hours=0,
            start=win_start, end=win_end, group_id=ai_system_id,
            top_n=25, compare=True,
        )
        savings = cost_service.savings(
            db, tenant_id=resolved, period_hours=0,
            start=win_start, end=win_end, group_id=ai_system_id,
        )
        runs = run_signal_service.compute(
            db, resolved, ai_system_id=ai_system_id,
            period_hours=0, start=win_start, end=win_end,
        )

        health = (
            db.query(AISystemHealth)
            .filter(
                AISystemHealth.tenant_id == resolved,
                AISystemHealth.ai_system_id == ai_system_id,
            )
            .first()
        )

        return {
            "month": month,
            "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "system": {
                "id": system.id,
                "name": system.name,
                "display_name": system.display_name,
                "system_kind": system.system_kind,
                "owner": system.owner,
                "intended_use": system.intended_use,
                "lifecycle_stage": system.lifecycle_stage,
                "eu_ai_act_risk_tier": system.eu_ai_act_risk_tier,
                "eu_ai_act_role": system.eu_ai_act_role,
                "hierarchy_path": system.hierarchy_path,
            },
            "cost": cost,
            "savings": savings,
            "runs": runs.model_dump(),
            # The CURRENT materialised verdict, clearly stamped: a monthly
            # statement generated mid-August carries August's health row,
            # not a reconstruction of July's — computed_at says so.
            "health": (
                {
                    "state": health.health_state,
                    "score": health.health_score,
                    "open_drift_events": health.open_drift_events,
                    "computed_at": (
                        health.computed_at.isoformat() if health.computed_at else None
                    ),
                }
                if health else None
            ),
        }

    return router

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Regulatory obligations API

    GET /admin/compliance/obligation-frameworks     — catalog listing
    GET /admin/compliance/obligations               — tenant board
    GET /admin/ai-systems/{id}/obligations          — one system

Read-only projections of the dated obligation catalog
(`app/services/ai_obligations.py`) over the AI Systems' declared facts.
Nothing is stored — see the service docstring for why applicability is
computed, dated, and never a compliance boolean.

"Today" is the UTC day: the evidence matrix of the same board and the
compliance overview date every action by its UTC timestamp and split a
window at UTC midnight where the catalog version changes,
so the board answers on the same day as its matrix, whatever the host's
local zone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import offering
from app.services.ai_obligations import (
    Framework,
    evaluate_system,
)


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _framework_or_404(framework_id: str) -> Framework:
    """The offered catalog; 404 unknown, 409 known but not offered."""
    return offering.require(framework_id)


class ObligationFrameworkInfo(BaseModel):
    id: str
    title: str
    jurisdiction: str
    reference: str
    obligation_count: int


class EvidenceSpecOut(BaseModel):
    """An evidence statement an obligation is shown against. The spec only — statuses are in `matrix` and in
    `/compliance/obligations/{key}`."""

    key: str
    statement: str
    basis: str  # recomputed | judged | indicator | declared
    resolver: str
    params: Dict[str, Any] = Field(default_factory=dict)
    applies_to_kinds: Optional[List[str]] = None
    limit: Optional[str] = None


class ObligationResult(BaseModel):
    key: str
    title: str
    reference: str
    summary: str
    applies_from: str
    in_force: bool
    status: str  # applies | not_applicable | undetermined
    reason: str
    needs_review: bool = False
    review_note: Optional[str] = None
    #: What the gateway cannot show for this obligation at all.
    limit: Optional[str] = None
    #: Earlier keys this obligation replaces.
    aliases: List[str] = Field(default_factory=list)
    evidence: List[EvidenceSpecOut] = Field(default_factory=list)


class EffectiveTierSource(BaseModel):
    system_id: str
    system_name: Optional[str] = None
    tier: Optional[str] = None


class SystemObligationsResponse(BaseModel):
    framework: ObligationFrameworkInfo
    system_id: str
    system_name: str
    eu_ai_act_risk_tier: Optional[str] = None
    eu_ai_act_role: Optional[str] = None
    #: The tier the obligations below were evaluated at
    #: (the effective tier under a composite-classification framework).
    risk_tier_evaluated: Optional[str] = None
    effective_tier_sources: List[EffectiveTierSource] = Field(default_factory=list)
    as_of: str
    obligations: List[ObligationResult]


class BoardSystemRef(BaseModel):
    id: str
    name: str
    eu_ai_act_risk_tier: Optional[str] = None
    eu_ai_act_role: Optional[str] = None
    #: The tier the obligation was evaluated at.
    risk_tier_evaluated: Optional[str] = None
    status: str
    reason: str


class BoardObligation(ObligationResult):
    systems: List[BoardSystemRef] = Field(default_factory=list)
    undetermined_count: int = 0


class ObligationMatrixCell(BaseModel):
    obligation_key: str
    applicability: str
    reason: Optional[str] = None
    in_force: bool
    present_in_window: bool = True
    worst_status: Optional[str] = None
    status_counts: Dict[str, int] = Field(default_factory=dict)
    #: met / measured statements per basis — recomputed and judged are
    #: never summed together.
    by_basis: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    summary: str = ""
    catalog_versions: List[str] = Field(default_factory=list)


class ObligationMatrixSystem(BaseModel):
    ai_system_id: str
    name: str
    risk_tier: Optional[str] = Field(
        None, description="The tier the system is evaluated at (effective under composite classification)"
    )
    risk_tier_declared: Optional[str] = None
    effective_tier_sources: List[EffectiveTierSource] = Field(default_factory=list)
    role: Optional[str] = None
    profile_declared: bool = False
    cells: List[ObligationMatrixCell]


class ObligationMatrix(BaseModel):
    """System × obligation, each cell the worst statement
    status over the window with n-of-m counts per basis."""

    window: Dict[str, str]
    catalog_versions: List[Optional[str]] = Field(default_factory=list)
    systems: List[ObligationMatrixSystem]


class ObligationsBoardResponse(BaseModel):
    framework: ObligationFrameworkInfo
    as_of: str
    system_count: int
    obligations: List[BoardObligation]
    matrix: Optional[ObligationMatrix] = None


def _evaluated_tier(fw: Framework, system: ResourceGroup, tier_facts: Any):
    """The EU tier the registry evaluates `system` at (declared, or the
    effective tier under composite classification) and its sources. The
    same rule `system_context` applies (`ai_obligations.evaluated_tier`)."""
    from app.services.ai_obligations import evaluated_tier

    return evaluated_tier(fw, system.eu_ai_act_risk_tier, system.id, tier_facts)


def _fw_info(fw: Framework) -> ObligationFrameworkInfo:
    return ObligationFrameworkInfo(
        id=fw.id,
        title=fw.title,
        jurisdiction=fw.jurisdiction,
        reference=fw.reference,
        obligation_count=len(fw.obligations),
    )


def create_ai_obligations_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["AI Obligations"])

    @router.get(
        "/compliance/obligation-frameworks",
        response_model=List[ObligationFrameworkInfo],
        summary="Available obligation frameworks",
    )
    async def list_frameworks(
        _admin=Depends(get_current_admin),
    ) -> List[ObligationFrameworkInfo]:
        return [_fw_info(fw) for fw in offering.offered_catalogs().values()]

    @router.get(
        "/compliance/obligations",
        response_model=ObligationsBoardResponse,
        summary="Tenant obligation board (per framework)",
    )
    def obligations_board(
        framework: str = Query("eu-ai-act"),
        ai_system_id: Optional[str] = Query(None, description="Restrict the matrix to one AI System"),
        from_: Optional[datetime] = Query(None, alias="from", description="Window start (default: 30 days ago)"),
        to: Optional[datetime] = Query(None, description="Window end (default: now)"),
        include_matrix: bool = Query(True, description="Evaluate the evidence statements (system × obligation)"),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> ObligationsBoardResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        fw = _framework_or_404(framework)
        today = datetime.now(timezone.utc).date()

        systems = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.tenant_id == resolved,
                ResourceGroup.group_type == GroupType.AI_SYSTEM,
                ResourceGroup.is_active.is_(True),
            )
            .order_by(ResourceGroup.name)
            .all()
        )

        # One tenant tier computation for the whole board.
        tier_facts = None
        if fw.composite_classification:
            from app.services.ai_system_composition import load_tier_facts

            tier_facts = load_tier_facts(db, resolved)

        # obligation key → aggregated rows across the fleet.
        per_key: Dict[str, Dict[str, Any]] = {}
        for system in systems:
            evaluated_at = _evaluated_tier(fw, system, tier_facts)[0]
            for row in evaluate_system(db, fw, system, today=today, tier_facts=tier_facts):
                agg = per_key.setdefault(row["key"], {**row, "systems": []})
                if row["status"] != "not_applicable":
                    agg["systems"].append(BoardSystemRef(
                        id=system.id,
                        name=system.display_name or system.name,
                        eu_ai_act_risk_tier=system.eu_ai_act_risk_tier,
                        eu_ai_act_role=system.eu_ai_act_role,
                        risk_tier_evaluated=evaluated_at,
                        status=str(row["status"]),
                        reason=str(row["reason"]),
                    ))

        obligations: List[BoardObligation] = []
        for o in fw.obligations:
            agg = per_key.get(o.key)
            if agg is None:
                continue
            systems_in_scope = agg.pop("systems")
            obligations.append(BoardObligation(
                **{k: v for k, v in agg.items() if k not in ("status", "reason")},
                # Board-level status summarises the fleet: applies if any
                # system applies; undetermined if only undetermined remain.
                status=(
                    "applies"
                    if any(s.status == "applies" for s in systems_in_scope)
                    else "undetermined"
                    if systems_in_scope
                    else "not_applicable"
                ),
                reason=f"{len(systems_in_scope)} system(s) in scope",
                systems=systems_in_scope,
                undetermined_count=sum(
                    1 for s in systems_in_scope if s.status == "undetermined"
                ),
            ))

        matrix = None
        if include_matrix:
            from app.database.models.tenant import Tenant
            from app.services.compliance import evaluation

            tenant = db.get(Tenant, resolved)
            if tenant is not None:
                window = evaluation.parse_window(from_, to)
                built = evaluation.matrix(db, tenant, fw, window, ai_system_id)
                matrix = ObligationMatrix(
                    window=built["window"],
                    catalog_versions=built["catalog_versions"],
                    systems=built["systems"],
                )

        return ObligationsBoardResponse(
            framework=_fw_info(fw),
            as_of=today.isoformat(),
            system_count=len(systems),
            obligations=obligations,
            matrix=matrix,
        )

    @router.get(
        "/ai-systems/{system_id}/obligations",
        response_model=SystemObligationsResponse,
        summary="Obligations for one AI System",
    )
    def system_obligations(
        system_id: str,
        framework: str = Query("eu-ai-act"),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> SystemObligationsResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        fw = _framework_or_404(framework)
        today = datetime.now(timezone.utc).date()

        system = (
            db.query(ResourceGroup)
            .filter(
                ResourceGroup.id == system_id,
                ResourceGroup.tenant_id == resolved,
                ResourceGroup.group_type == GroupType.AI_SYSTEM,
            )
            .first()
        )
        if system is None:
            raise HTTPException(
                status_code=404, detail=f"AI System {system_id!r} not found"
            )

        tier_facts = None
        if fw.composite_classification:
            from app.services.ai_system_composition import load_tier_facts

            tier_facts = load_tier_facts(db, resolved)
        rows = evaluate_system(db, fw, system, today=today, tier_facts=tier_facts)
        evaluated_at, sources = _evaluated_tier(fw, system, tier_facts)
        return SystemObligationsResponse(
            framework=_fw_info(fw),
            system_id=system.id,
            system_name=system.display_name or system.name,
            eu_ai_act_risk_tier=system.eu_ai_act_risk_tier,
            eu_ai_act_role=system.eu_ai_act_role,
            risk_tier_evaluated=evaluated_at,
            effective_tier_sources=sources,
            as_of=today.isoformat(),
            obligations=[ObligationResult(**r) for r in rows],
        )

    return router

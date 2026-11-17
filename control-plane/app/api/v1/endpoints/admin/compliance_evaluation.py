# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Compliance evaluation API

    GET /admin/compliance/overview?framework=&from=&to=
    GET /admin/compliance/obligations/{key}?framework=&ai_system_id=&from=&to=
    GET /admin/compliance/systems
    GET /admin/compliance/retention

(The system × obligation matrix is served by the obligations board,
`GET /admin/compliance/obligations`, which keeps its own shape and adds
`matrix`.)

Statements are RECOMPUTED · JUDGED · INDICATOR · DECLARED, `n` of `m`,
never a percentage, never "compliant". Every provision string is a pointer
into the instrument — not legal advice. Evaluations are cached per
(tenant, framework, window) for a minute.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.tenant import Tenant
from app.services.compliance import evaluation
from app.services.compliance.retention_floors import retention_view

# ---------------------------------------------------------------------------
# Response models (every field declared — Pydantic strips the rest)
# ---------------------------------------------------------------------------


class ComplianceFrameworkSummary(BaseModel):
    id: str
    title: str
    kind: str
    jurisdiction: str
    reference: str
    catalog_version: Optional[str] = None
    versions: List[Dict[str, Any]] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list)
    classifications: List[str] = Field(default_factory=list)
    deadline_rules: Dict[str, Any] = Field(default_factory=dict)
    #: Staged regimes (DORA, NIS2): each stage's anchor and period; the
    #: `deadline_rules` of a staged framework are only earliest bounds.
    deadline_stages: List[Dict[str, Any]] = Field(default_factory=list)
    retention_floors: Dict[str, Any] = Field(default_factory=dict)
    verified_against_instrument: bool
    composite_classification: bool = False
    obligation_count: int
    enabled: bool = False
    registered: bool = False
    not_legal_advice: str


class EvaluationWindow(BaseModel):
    from_: str = Field(..., alias="from")
    to: str

    model_config = {"populate_by_name": True}


class EvidenceStatementOut(BaseModel):
    key: str
    statement: str
    basis: str
    resolver: str
    params: Dict[str, Any] = Field(default_factory=dict)
    status: str
    n: Optional[int] = None
    m: Optional[int] = None
    detail: str = ""
    limit: Optional[str] = None
    citations: List[str] = Field(default_factory=list)
    catalog_version: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    evaluated_at: str
    facts: Dict[str, Any] = Field(default_factory=dict)
    refs: List[Dict[str, Any]] = Field(default_factory=list)
    other_frameworks: List[Dict[str, Any]] = Field(default_factory=list)


class EvidenceStatementSummary(BaseModel):
    worst_status: Optional[str] = None
    status_counts: Dict[str, int] = Field(default_factory=dict)
    by_basis: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    summary: str = ""


class ObligationEvidenceDetail(EvidenceStatementSummary):
    key: str
    title: str
    reference: str
    applies_from: str
    in_force: bool
    applicability: str
    reason: str
    needs_review: bool = False
    review_note: Optional[str] = None
    present_in_window: bool = True
    statements: List[EvidenceStatementOut] = Field(default_factory=list)


class ObligationDetailSystem(BaseModel):
    ai_system_id: str
    name: str
    obligations: List[ObligationEvidenceDetail]


class ObligationDetailResponse(BaseModel):
    framework: ComplianceFrameworkSummary
    key: str
    resolved_keys: List[str]
    title: str
    reference: str
    summary: Optional[str] = None
    limit: Optional[str] = None
    not_legal_advice: str
    window: EvaluationWindow
    systems: List[ObligationDetailSystem]


class ComplianceDatedObligation(BaseModel):
    key: str
    title: str
    applies_from: str
    reason: Optional[str] = None


class ComplianceTimelineEntry(BaseModel):
    key: str
    title: str
    applies_from: str
    in_force: bool


class EvidenceCaptureBlock(BaseModel):
    unsealed_governed_actions: Optional[int] = None
    open_capture_gap_items: int = 0


class ComplianceOverviewSystem(BaseModel):
    ai_system_id: str
    name: str
    risk_tier: Optional[str] = Field(
        None, description="The tier the system is evaluated at (effective under composite classification)"
    )
    #: The declared tier, and the systems whose decisions
    #: raised it (empty when the evaluated tier is the declared one).
    risk_tier_declared: Optional[str] = None
    effective_tier_sources: List[Dict[str, Any]] = Field(default_factory=list)
    role: Optional[str] = None
    profile_declared: bool
    profile_sealed: bool
    obligations_in_force: List[ComplianceDatedObligation]
    obligations_upcoming: List[ComplianceDatedObligation]
    undetermined: List[ComplianceDatedObligation]
    statement_counts: EvidenceStatementSummary
    capture: EvidenceCaptureBlock
    latest_report: Optional[Dict[str, Any]] = None


class ComplianceOverviewEvidence(BaseModel):
    latest_head: Optional[Dict[str, Any]] = None
    grade: Optional[str] = None
    record_lag: Dict[str, Any] = Field(default_factory=dict)
    open_capture_gap_items: int = 0


class ComplianceOverviewResponse(BaseModel):
    framework: ComplianceFrameworkSummary
    window: EvaluationWindow
    evidence: ComplianceOverviewEvidence
    timeline: List[ComplianceTimelineEntry]
    systems: List[ComplianceOverviewSystem]


class ComplianceSystemRow(BaseModel):
    ai_system_id: str
    tenant_id: str
    name: str
    lifecycle_stage: Optional[str] = None
    core: Dict[str, Any] = Field(default_factory=dict)
    frameworks: Dict[str, Any] = Field(default_factory=dict)
    declaration_record_id: Optional[str] = None
    sealed: bool = False
    exists: bool
    active_frameworks: List[str] = Field(default_factory=list)
    retention_floor_days: Optional[int] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class ComplianceSystemsResponse(BaseModel):
    items: List[ComplianceSystemRow]
    frameworks_enabled: List[str]


class RetentionFloorSystem(BaseModel):
    ai_system_id: str
    name: str
    floor_days: Optional[int] = None
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    effective_log_days: Optional[int] = None
    effective_run_days: Optional[int] = None


class RetentionCompositionResponse(BaseModel):
    policy: Optional[Dict[str, Any]] = None
    floor_days: Optional[int] = None
    effective: Optional[Dict[str, Any]] = None
    raised: bool = False
    systems: List[RetentionFloorSystem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _tenant(db: Session, tenant_id: str) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id!r} not found")
    return tenant


def create_compliance_evaluation_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/compliance", tags=["Compliance Evaluation"])

    @router.get(
        "/overview",
        response_model=ComplianceOverviewResponse,
        summary="Per-system evidence readiness for one framework",
    )
    def overview(
        framework: str = Query("eu-ai-act"),
        from_: Optional[datetime] = Query(None, alias="from"),
        to: Optional[datetime] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> ComplianceOverviewResponse:
        admin, admin_type = admin_and_type
        tenant = _tenant(db, _resolve_tenant_id(admin, admin_type, tenant_id))
        fw = evaluation.framework_or_404(framework)
        window = evaluation.parse_window(from_, to)
        return ComplianceOverviewResponse(**evaluation.overview(db, tenant, fw, window))

    @router.get(
        "/obligations/{key}",
        response_model=ObligationDetailResponse,
        summary="One obligation's evidence statements per system",
    )
    def obligation_detail(
        key: str,
        framework: str = Query("eu-ai-act"),
        ai_system_id: Optional[str] = Query(None),
        from_: Optional[datetime] = Query(None, alias="from"),
        to: Optional[datetime] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> ObligationDetailResponse:
        admin, admin_type = admin_and_type
        tenant = _tenant(db, _resolve_tenant_id(admin, admin_type, tenant_id))
        fw = evaluation.framework_or_404(framework)
        window = evaluation.parse_window(from_, to)
        return ObligationDetailResponse(**evaluation.detail(db, tenant, fw, key, ai_system_id, window))

    @router.get(
        "/systems",
        response_model=ComplianceSystemsResponse,
        summary="Compliance profiles of every AI System",
    )
    def systems(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> ComplianceSystemsResponse:
        admin, admin_type = admin_and_type
        tenant = _tenant(db, _resolve_tenant_id(admin, admin_type, tenant_id))
        return ComplianceSystemsResponse(**evaluation.systems_table(db, tenant))

    @router.get(
        "/retention",
        response_model=RetentionCompositionResponse,
        summary="Retention policy composed with framework floors",
        description=(
            "Effective retention = max(tenant policy, the floors of each system's active frameworks by "
            "classification). The sweeper enforces the tenant-level effective policy."
        ),
    )
    def retention(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> RetentionCompositionResponse:
        admin, admin_type = admin_and_type
        tenant = _tenant(db, _resolve_tenant_id(admin, admin_type, tenant_id))
        return RetentionCompositionResponse(**retention_view(db, tenant))

    return router


__all__ = ["create_compliance_evaluation_router"]

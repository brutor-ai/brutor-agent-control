# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Compliance framework registry + AI System compliance profiles

    GET   /admin/compliance/frameworks                        — registry, with tenant enablement
    PATCH /admin/compliance/frameworks/{id}                   — enable / disable for the tenant
    GET   /admin/compliance/ai-systems/{ai_system_id}/profile — declared facts (defaults if none)
    PUT   /admin/compliance/ai-systems/{ai_system_id}/profile — declare / update

The registry is data (`compliance_frameworks`); tenant enablement lives on
`tenants.compliance_profile` so enabling the EU AI Act here is the same
switch that turns `eu_ai_act` tagging on (see
`app/services/compliance_registry.py`).

A profile is a neutral `core` plus a `frameworks` block keyed by registry
id. The `eu-ai-act` block's `role` / `risk_tier` are written through to
`resource_groups.eu_ai_act_role` / `eu_ai_act_risk_tier` — those columns
stay the single source of truth for the existing obligation board, and
the GET prefills from them when no profile row exists yet.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.tenant import Tenant
from app.services import compliance_registry
from app.services.compliance import profile as profile_service


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.middleware.trusted_proxies import client_ip


EU_AI_ACT_ID = "eu-ai-act"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FrameworkOut(BaseModel):
    id: str
    tag_key: Optional[str] = None
    title: str
    kind: str
    jurisdiction: str
    reference: str
    catalog_version: str
    verified_against_instrument: bool
    #: final | draft | licensed — what the catalog can be checked against.
    instrument_status: Optional[str] = None
    verified_on: Optional[str] = None
    #: Offered = verified against a final published text.
    offered: bool = False
    #: Why it is not offered, for the console; None when offered.
    unavailable_reason: Optional[str] = None
    retention_floor_days: Optional[int] = None
    deadline_rules: Dict[str, Any] = Field(default_factory=dict)
    sort_order: int
    enabled: bool
    obligation_count: int


class FrameworkListResponse(BaseModel):
    items: List[FrameworkOut]


class FrameworkEnableRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ProfileOut(BaseModel):
    ai_system_id: str
    tenant_id: str
    core: Dict[str, Any] = Field(default_factory=dict)
    frameworks: Dict[str, Any] = Field(default_factory=dict)
    declaration_record_id: Optional[str] = None
    exists: bool
    #: Whether the current save is sealed as an `ai.brutor.declaration`.
    #: False with `seal_error` when the core could not seal
    #: it — the profile is saved either way.
    sealed: bool = False
    seal_error: Optional[str] = None
    seal_detail: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class ProfilePutRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    core: Dict[str, Any] = Field(default_factory=dict)
    frameworks: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation lives in app/services/compliance/profile.py (pydantic)
# ---------------------------------------------------------------------------


def _client(request: Request) -> Dict[str, Any]:
    return {
        "ip_address": client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "request_method": request.method,
        "request_path": str(request.url.path),
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def create_compliance_registry_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/compliance", tags=["Compliance Registry"])

    def _load_tenant(db: Session, tenant_id: str) -> Tenant:
        tenant = db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id!r} not found")
        return tenant

    def _load_system(db: Session, tenant_id: str, ai_system_id: str) -> ResourceGroup:
        system = (
            db.query(ResourceGroup)
            .filter(ResourceGroup.id == ai_system_id, ResourceGroup.tenant_id == tenant_id)
            .first()
        )
        if system is None:
            raise HTTPException(status_code=404, detail=f"AI System {ai_system_id!r} not found")
        if system.group_type != GroupType.AI_SYSTEM:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{ai_system_id!r} is a {system.group_type} group, not an AI System — "
                    f"a compliance profile describes a governed workload, not an access group"
                ),
            )
        return system

    def _profile_out(
        system: ResourceGroup,
        row: Optional[AISystemComplianceProfile],
        tenant_id: str,
        seal: Optional[Any] = None,
    ) -> ProfileOut:
        data = profile_service.profile_view(system, row, tenant_id)
        if seal is not None:
            data["sealed"] = seal.sealed
            data["seal_error"] = seal.error
            data["seal_detail"] = seal.detail
        return ProfileOut(**data)

    # -- frameworks -------------------------------------------------------

    @router.get(
        "/frameworks",
        response_model=FrameworkListResponse,
        summary="The framework registry, with this tenant's enablement",
    )
    def list_frameworks(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> FrameworkListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        tenant = _load_tenant(db, resolved)
        rows = compliance_registry.list_frameworks(db, tenant)
        return FrameworkListResponse(items=[FrameworkOut(**r) for r in rows])

    @router.patch(
        "/frameworks/{framework_id}",
        response_model=FrameworkOut,
        summary="Enable or disable a framework for the tenant",
    )
    def set_framework_enabled(
        request: Request,
        framework_id: str,
        body: FrameworkEnableRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> FrameworkOut:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        tenant = _load_tenant(db, resolved)
        before = dict(tenant.compliance_profile or {})
        view = compliance_registry.set_enabled(db, tenant, framework_id, body.enabled)
        db.commit()
        audit.log(
            action="compliance_framework_enable" if body.enabled else "compliance_framework_disable",
            admin=admin,
            admin_type=admin_type,
            tenant_id=resolved,
            success=True,
            resource_type="compliance_framework",
            resource_id=framework_id,
            details={
                "enabled": body.enabled,
                "previous_profile": before,
                "new_profile": tenant.compliance_profile,
            },
            db=db,
            **_client(request),
        )
        return FrameworkOut(**view)

    # -- profiles ---------------------------------------------------------

    @router.get(
        "/ai-systems/{ai_system_id}/profile",
        response_model=ProfileOut,
        summary="The AI System's compliance profile (defaults when none is declared)",
    )
    def get_profile(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> ProfileOut:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)
        row = db.get(AISystemComplianceProfile, ai_system_id)
        if row is not None and row.tenant_id != resolved:
            row = None
        return _profile_out(system, row, resolved)

    @router.put(
        "/ai-systems/{ai_system_id}/profile",
        response_model=ProfileOut,
        summary="Declare or update the AI System's compliance profile",
    )
    def put_profile(
        request: Request,
        ai_system_id: str,
        body: ProfilePutRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> ProfileOut:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        system = _load_system(db, resolved, ai_system_id)
        existing = db.get(AISystemComplianceProfile, ai_system_id)
        already = (existing.frameworks or {}).keys() if existing is not None and existing.tenant_id == resolved else ()
        core, frameworks = profile_service.validate(db, body.core, body.frameworks, already)
        row, previous, group_changes, seal = profile_service.save(
            db, system, resolved, core, frameworks
        )
        created = previous is None

        audit.log(
            action="compliance_profile_declare" if created else "compliance_profile_update",
            admin=admin,
            admin_type=admin_type,
            tenant_id=resolved,
            success=True,
            resource_type="compliance_profile",
            resource_id=ai_system_id,
            details={
                "previous": previous,
                "core": core,
                "frameworks": frameworks,
                "group_changes": group_changes,
                "declaration_record_id": row.declaration_record_id,
                "sealed": seal.sealed,
                "seal_error": seal.error,
            },
            db=db,
            **_client(request),
        )
        return _profile_out(system, row, resolved, seal)

    return router


__all__ = ["create_compliance_registry_router"]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Transparency API — AI-interaction notice configuration and evidence

    GET    /admin/compliance/notice-configs                  — tenant default + per-system rows
    GET    /admin/compliance/notice-configs/effective        — what the portal resolves for a system
    PUT    /admin/compliance/notice-configs                  — save the tenant default (?ai_system_id= for a system)
    DELETE /admin/compliance/notice-configs/{config_id}      — remove a scope's config
    GET    /admin/compliance/transparency?ai_system_id=      — config, notice evidence (n of m
                                                               conversations), synthetic-content declaration

Texts are stored per locale with their SHA-256 digests (the value the
portal's rendered-notice record references). Every save seals an
`ai.brutor.notice-config` record; an asserted exemption also seals an
`ai.brutor.declaration` and is shown as DECLARED — it is the operator's
statement, not evidence that no notice was needed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin._compliance_http import (
    SealStatus,
    client_info,
    load_ai_system,
    parse_window,
    seal_status,
)
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
    resolve_tenant_id,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.compliance_notice_config import ComplianceNoticeConfig
from app.database.models.resource_mixins import actor_name
from app.services.compliance import notices as svc


class NoticeExemption(BaseModel):
    asserted: bool = False
    basis: Optional[str] = Field(None, description="obvious_from_context | authorised_by_law | no_natural_person | other")
    rationale: Optional[str] = Field(None, max_length=4000)
    declaration_record_id: Optional[str] = None


class NoticeConfigOut(BaseModel):
    id: Optional[str] = None
    tenant_id: str
    ai_system_id: Optional[str] = None
    enabled: bool = True
    surface: str = "both"
    texts: Dict[str, str] = Field(default_factory=dict)
    text_digests: Dict[str, str] = Field(default_factory=dict)
    exemption: Optional[NoticeExemption] = None
    record_id: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    source: Optional[str] = Field(None, description="system | tenant | default (effective view only)")


class NoticeConfigListResponse(BaseModel):
    items: List[NoticeConfigOut]
    total: int


class NoticeConfigPutRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    surface: str = Field("both", description="banner | inline | both")
    texts: Dict[str, str] = Field(default_factory=dict, description="{locale: notice text}")
    exemption: Optional[NoticeExemption] = None


class NoticeConfigSaveResponse(BaseModel):
    config: NoticeConfigOut
    seal: SealStatus
    declaration: Optional[SealStatus] = Field(
        None, description="The exemption declaration's seal, when this save asserted or changed one"
    )


class NoticeConfigDeleteResponse(BaseModel):
    deleted: bool
    seal: SealStatus


class TransparencyStatementOut(BaseModel):
    key: str
    statement: str = ""
    basis: str
    status: str
    n: Optional[int] = None
    m: Optional[int] = None
    detail: str = ""
    limit: Optional[str] = None
    citations: List[str] = Field(default_factory=list)
    facts: Dict[str, Any] = Field(default_factory=dict)


class TransparencySummary(BaseModel):
    tenant_id: str
    ai_system_id: Optional[str] = None
    window_start: str
    window_end: str
    config: NoticeConfigOut
    notice_evidence: Optional[TransparencyStatementOut] = None
    notice_text: Optional[TransparencyStatementOut] = None
    synthetic_content: Optional[TransparencyStatementOut] = None
    generates_synthetic_content: Optional[bool] = None
    interacts_with_natural_persons: Optional[bool] = None
    limits: List[str] = Field(default_factory=list)


#: Stated on every transparency summary.
TRANSPARENCY_LIMITS = [
    "Notice evidence counts conversations through the Brutor portal; other clients must render and "
    "record their own notice.",
    "Content-level marking (watermarking, C2PA) is not provided; outputs carry an "
    "X-Brutor-AI-Generated header only.",
    "An exemption is the operator's declaration; it is shown as declared, never as evidence.",
]


def _config_out(data: Dict[str, Any]) -> NoticeConfigOut:
    return NoticeConfigOut(**data)


def create_compliance_transparency_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/compliance", tags=["Compliance Transparency"])

    def _guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except svc.NoticeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.get("/notice-configs", response_model=NoticeConfigListResponse,
                summary="AI-interaction notice configs (tenant default and per system)")
    def list_notice_configs(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> NoticeConfigListResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        rows = (
            db.query(ComplianceNoticeConfig)
            .filter(ComplianceNoticeConfig.tenant_id == tenant)
            .order_by(ComplianceNoticeConfig.ai_system_id.isnot(None), ComplianceNoticeConfig.ai_system_id)
            .all()
        )
        return NoticeConfigListResponse(items=[_config_out(r.to_dict()) for r in rows], total=len(rows))

    @router.get("/notice-configs/effective", response_model=NoticeConfigOut,
                summary="The notice config the portal resolves for a system (system → tenant → default)")
    def effective_notice_config(
        ai_system_id: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> NoticeConfigOut:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        if ai_system_id:
            load_ai_system(db, tenant, ai_system_id)
        return _config_out(svc.effective_config(db, tenant, ai_system_id))

    @router.put("/notice-configs", response_model=NoticeConfigSaveResponse,
                summary="Save the notice config for the tenant (or one system with ?ai_system_id=)")
    def put_notice_config(
        request: Request,
        body: NoticeConfigPutRequest,
        ai_system_id: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> NoticeConfigSaveResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        saved = _guard(
            svc.save_config, db, tenant,
            ai_system_id=ai_system_id, enabled=body.enabled, surface=body.surface, texts=body.texts,
            exemption=body.exemption.model_dump() if body.exemption else None, actor=actor_name(admin),
        )
        db.commit()
        db.refresh(saved.config)
        audit.log(
            action="compliance_notice_config_save",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant,
            success=True,
            resource_type="compliance_notice_config",
            resource_id=saved.config.id,
            details={
                "ai_system_id": ai_system_id,
                "surface": saved.config.surface,
                "enabled": bool(saved.config.enabled),
                "locales": sorted((saved.config.text_digests or {}).keys()),
                "exemption_asserted": bool(saved.config.exemption),
                "record_id": saved.seal.record_id,
            },
            db=db,
            **client_info(request),
        )
        return NoticeConfigSaveResponse(
            config=_config_out(saved.config.to_dict()),
            seal=seal_status(saved.seal),
            declaration=seal_status(saved.declaration) if saved.declaration is not None else None,
        )

    @router.delete("/notice-configs/{config_id}", response_model=NoticeConfigDeleteResponse,
                   summary="Remove a notice config (the scope falls back to the tenant row or the default)")
    def delete_notice_config(
        request: Request,
        config_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> NoticeConfigDeleteResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = db.get(ComplianceNoticeConfig, config_id)
        if row is None or row.tenant_id != tenant:
            raise HTTPException(status_code=404, detail="Notice config not found")
        scope = row.ai_system_id
        outcome = svc.delete_config(db, row)
        db.commit()
        audit.log(
            action="compliance_notice_config_delete",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant,
            success=True,
            resource_type="compliance_notice_config",
            resource_id=config_id,
            details={"ai_system_id": scope, "record_id": outcome.record_id},
            db=db,
            **client_info(request),
        )
        return NoticeConfigDeleteResponse(deleted=True, seal=seal_status(outcome))

    @router.get("/transparency", response_model=TransparencySummary,
                summary="Notice config, notice evidence (n of m conversations) and synthetic-content declarations")
    def transparency_summary(
        ai_system_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> TransparencySummary:
        from app.services.compliance import transparency

        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        system = load_ai_system(db, tenant, ai_system_id) if ai_system_id else None
        start, end = parse_window(from_, to)
        summary = transparency.summary(db, tenant, system, start, end)
        summary["limits"] = TRANSPARENCY_LIMITS
        return TransparencySummary(**summary)

    return router


__all__ = ["create_compliance_transparency_router", "TRANSPARENCY_LIMITS"]

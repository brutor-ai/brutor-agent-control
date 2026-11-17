# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Documentation export API

    GET /admin/ai-systems/{id}/documentation?framework=&format=json|html
    GET /admin/ai-systems/{id}/documentation/exports?framework=   — previous exports

Renders the framework's documentation template (EU Annex IV, ISO A.6.2.7 /
A.8.2, SOC 2 system description, generic) for the AI System, persists it
with its digest and seals an `ai.brutor.documentation` record. The document
is a skeleton generated from configuration — it says so in its own text —
and re-requesting an unchanged document returns the same export (same
digest, no new record). `format=html` returns a printable page; there is
no PDF renderer on the control plane.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin._compliance_http import (
    SealStatus,
    client_info,
    load_ai_system,
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
from app.database.models.compliance_documentation_export import ComplianceDocumentationExport
from app.database.models.resource_mixins import actor_name
from app.services.compliance import documentation as svc


class DocumentationExportSummary(BaseModel):
    id: str
    tenant_id: str
    ai_system_id: str
    framework_id: str
    template: str
    contract_version: Optional[int] = None
    digest: str
    record_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None


class DocumentationExportOut(DocumentationExportSummary):
    content: Dict[str, Any]
    skeleton: bool = True
    reused: bool = Field(False, description="True when the unchanged document was already exported")
    seal: SealStatus


class DocumentationExportList(BaseModel):
    items: List[DocumentationExportSummary]
    total: int


def create_compliance_documentation_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/ai-systems", tags=["Compliance Documentation"])

    @router.get(
        "/{ai_system_id}/documentation",
        response_model=DocumentationExportOut,
        summary="Render (and seal) the framework's documentation skeleton for an AI System",
        responses={200: {"content": {"text/html": {}}}},
    )
    def get_documentation(
        request: Request,
        ai_system_id: str,
        framework: str = Query("eu-ai-act"),
        format: str = Query("json", pattern="^(json|html)$"),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        system = load_ai_system(db, tenant, ai_system_id)
        try:
            result = svc.export(db, system, framework, actor=actor_name(admin))
        except svc.DocumentationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        db.commit()
        row = result.export
        if not result.reused:
            audit.log(
                action="compliance_documentation_export",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant,
                success=True,
                resource_type="compliance_documentation_export",
                resource_id=row.id,
                details={
                    "ai_system_id": ai_system_id,
                    "framework_id": framework,
                    "digest": row.digest,
                    "record_id": row.record_id,
                },
                db=db,
                **client_info(request),
            )
        if format == "html":
            return HTMLResponse(svc.render_html(row))
        return DocumentationExportOut(
            **row.to_dict(),
            reused=result.reused,
            seal=seal_status(result.seal, record_id=row.record_id),
        )

    @router.get(
        "/{ai_system_id}/documentation/exports",
        response_model=DocumentationExportList,
        summary="Previous documentation exports for an AI System (newest first)",
    )
    def list_documentation_exports(
        ai_system_id: str,
        framework: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> DocumentationExportList:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        load_ai_system(db, tenant, ai_system_id)
        q = db.query(ComplianceDocumentationExport).filter(
            ComplianceDocumentationExport.tenant_id == tenant,
            ComplianceDocumentationExport.ai_system_id == ai_system_id,
        )
        if framework:
            q = q.filter(ComplianceDocumentationExport.framework_id == framework)
        rows = q.order_by(ComplianceDocumentationExport.created_at.desc()).limit(100).all()
        return DocumentationExportList(
            items=[DocumentationExportSummary(**r.summary_dict()) for r in rows], total=len(rows)
        )

    return router


__all__ = ["create_compliance_documentation_router"]

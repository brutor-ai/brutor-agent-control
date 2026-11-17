# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence Reports and Human Oversight API

    GET  /admin/compliance/reports?framework=&ai_system_id=&cadence=   — generated reports (no rows)
    POST /admin/compliance/reports                                     — generate now (idempotent per period)
    GET  /admin/compliance/reports/{id}                                — one report with its rows
    GET  /admin/compliance/reports/{id}/bundle                         — the monthly evidence bundle (JSON)
    GET  /admin/compliance/oversight?ai_system_id=&from=&to=           — Human Oversight summary

Recomputed and judged rows are never merged; judged rows carry human
agreement beside them. Countersignatures are passed through from the
stored bundle as-is (`countersignature_status`).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
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
from app.database.models.compliance_report import REPORT_CADENCES, ComplianceReport
from app.database.models.evidence_bundle import EvidenceBundle
from app.database.models.resource_mixins import actor_name
from app.services.compliance import reports as svc


class ReportSummaryOut(BaseModel):
    id: str
    tenant_id: str
    framework_id: str
    ai_system_id: Optional[str] = None
    cadence: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    catalog_version: str
    status: str
    summary: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    record_id: Optional[str] = None
    bundle_id: Optional[str] = None
    countersignatures: List[Any] = Field(default_factory=list)
    countersignature_status: str = "none"
    permalink: Optional[str] = None
    generated_by: Optional[str] = None
    created_at: Optional[str] = None


class ReportOut(ReportSummaryOut):
    rows: List[Dict[str, Any]] = Field(default_factory=list)


class ReportListResponse(BaseModel):
    items: List[ReportSummaryOut]
    total: int


class ReportGenerateRequest(BaseModel):
    framework_id: str
    cadence: str = Field("monthly", description="daily | weekly | monthly")
    ai_system_id: Optional[str] = None
    period_start: Optional[date] = Field(None, description="Any day in the period (default: the last complete one)")
    force: bool = Field(False, description="Regenerate an existing period in place")


class ReportGenerateResponse(BaseModel):
    report: ReportOut
    created: bool
    regenerated: bool
    seal: SealStatus
    bundle_error: Optional[str] = None


class OversightSummary(BaseModel):
    tenant_id: str
    ai_system_id: Optional[str] = None
    window_start: str
    window_end: str
    approvals: Dict[str, int]
    systems: List[Dict[str, Any]]
    review_queue: Dict[str, int]
    judged_checks: List[Dict[str, Any]]
    movement_note: str


def _countersignature_status(row: ComplianceReport, bundle: Optional[EvidenceBundle]) -> List[Any]:
    sigs = list(row.countersignatures or [])
    if bundle is not None and isinstance(bundle.bundle, dict):
        for s in bundle.bundle.get("countersignatures") or []:
            if s not in sigs:
                sigs.append(s)
    return sigs


def _summary_out(row: ComplianceReport, bundle: Optional[EvidenceBundle] = None) -> Dict[str, Any]:
    data = row.summary_dict()
    sigs = _countersignature_status(row, bundle)
    data["countersignatures"] = sigs
    data["countersignature_status"] = (
        "countersigned" if sigs else ("awaiting_countersignature" if row.bundle_id else "none")
    )
    data["permalink"] = f"/v1/admin/compliance/reports/{row.id}/bundle" if row.bundle_id else None
    return data


def create_compliance_reports_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/compliance", tags=["Compliance Reports"])

    def _load(db: Session, tenant: str, report_id: str) -> ComplianceReport:
        row = db.get(ComplianceReport, report_id)
        if row is None or row.tenant_id != tenant:
            raise HTTPException(status_code=404, detail="Report not found")
        return row

    def _bundle(db: Session, row: ComplianceReport) -> Optional[EvidenceBundle]:
        if not row.bundle_id:
            return None
        b = db.get(EvidenceBundle, row.bundle_id)
        return b if b is not None and b.tenant_id == row.tenant_id else None

    @router.get("/reports", response_model=ReportListResponse, summary="Generated Evidence Reports")
    def list_reports(
        framework: Optional[str] = Query(None),
        ai_system_id: Optional[str] = Query(None, description="A system id, or 'estate' for estate-wide only"),
        cadence: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=500),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> ReportListResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        if cadence is not None and cadence not in REPORT_CADENCES:
            raise HTTPException(status_code=422, detail=f"cadence must be one of {sorted(REPORT_CADENCES)}")
        q = db.query(ComplianceReport).filter(ComplianceReport.tenant_id == tenant)
        if framework:
            q = q.filter(ComplianceReport.framework_id == framework)
        if ai_system_id == "estate":
            q = q.filter(ComplianceReport.ai_system_id.is_(None))
        elif ai_system_id:
            q = q.filter(ComplianceReport.ai_system_id == ai_system_id)
        if cadence:
            q = q.filter(ComplianceReport.cadence == cadence)
        rows = q.order_by(ComplianceReport.period_start.desc(), ComplianceReport.created_at.desc()).limit(limit).all()
        return ReportListResponse(items=[ReportSummaryOut(**_summary_out(r)) for r in rows], total=len(rows))

    @router.post("/reports", response_model=ReportGenerateResponse, summary="Generate an Evidence Report now")
    def generate_report(
        request: Request,
        body: ReportGenerateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> ReportGenerateResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        if body.ai_system_id:
            load_ai_system(db, tenant, body.ai_system_id)
        if body.cadence not in REPORT_CADENCES:
            raise HTTPException(status_code=422, detail=f"cadence must be one of {sorted(REPORT_CADENCES)}")
        day = body.period_start or svc.previous_period(body.cadence, datetime.now(timezone.utc).date())[0]
        try:
            result = svc.generate(
                db, tenant, body.framework_id, cadence=body.cadence, day=day,
                ai_system_id=body.ai_system_id, force=body.force, actor=actor_name(admin),
            )
        except svc.ReportError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        db.commit()
        row = result.report
        regenerated = result.seal is not None and not result.created
        if result.seal is not None:
            audit.log(
                action="compliance_report_generate",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant,
                success=row.status == "ready",
                resource_type="compliance_report",
                resource_id=row.id,
                details={
                    "framework_id": row.framework_id,
                    "ai_system_id": row.ai_system_id,
                    "cadence": row.cadence,
                    "period_start": row.period_start.isoformat(),
                    "record_id": row.record_id,
                    "bundle_id": row.bundle_id,
                    "regenerated": regenerated,
                },
                db=db,
                **client_info(request),
            )
        return ReportGenerateResponse(
            report=ReportOut(**_summary_out(row, _bundle(db, row)), rows=row.rows or []),
            created=result.created,
            regenerated=regenerated,
            seal=seal_status(result.seal, record_id=row.record_id),
            bundle_error=result.bundle_error,
        )

    @router.get("/reports/{report_id}", response_model=ReportOut, summary="One Evidence Report with its rows")
    def get_report(
        report_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> ReportOut:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, report_id)
        return ReportOut(**_summary_out(row, _bundle(db, row)), rows=row.rows or [])

    @router.get(
        "/reports/{report_id}/bundle",
        summary="The report's evidence bundle (ai.brutor/evidence-bundle/v1 JSON)",
        responses={200: {"content": {"application/json": {}}}},
    )
    def get_report_bundle(
        report_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.EVIDENCE_READ)),
    ) -> JSONResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, report_id)
        bundle = _bundle(db, row)
        if bundle is None:
            reason = (row.summary or {}).get("bundle_error") or "bundles are built for monthly reports"
            raise HTTPException(status_code=404, detail=f"No bundle for this report ({reason})")
        return JSONResponse(
            bundle.bundle,
            headers={
                "Content-Disposition": f'inline; filename="evidence-report-{row.id}.json"',
                "X-Brutor-Bundle-Digest": bundle.bundle_digest,
            },
        )

    @router.get("/oversight", response_model=OversightSummary, summary="Human Oversight summary")
    def oversight_summary(
        ai_system_id: Optional[str] = Query(None),
        from_: Optional[str] = Query(None, alias="from"),
        to: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> OversightSummary:
        from app.services.compliance import oversight

        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        if ai_system_id:
            load_ai_system(db, tenant, ai_system_id)
        start, end = parse_window(from_, to)
        return OversightSummary(**oversight.summary(db, tenant, ai_system_id, start, end))

    return router


__all__ = ["create_compliance_reports_router"]

# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Incident register API

    GET    /admin/compliance/incidents                   — register (filters: status, framework, ai_system_id)
    POST   /admin/compliance/incidents                   — open an incident
    GET    /admin/compliance/incidents/{id}              — one incident with deadlines and events
    PATCH  /admin/compliance/incidents/{id}              — change fields (recomputes deadlines)
    POST   /admin/compliance/incidents/{id}/report       — record the report to an authority
    POST   /admin/compliance/incidents/{id}/submissions  — record a staged report (DORA / NIS2 stage)
    POST   /admin/compliance/incidents/{id}/close        — close
    GET    /admin/compliance/incidents/{id}/events       — the sealed history
    POST   /admin/compliance/incidents/{id}/events       — add a note

Deadlines come from each framework's `deadline_rules` (alternatives) or
`deadline_stages` (staged regimes, each stage on its own anchor: awareness,
`classified_at`, or an earlier stage's submission) and are shown, never
enforced (`deadlines_enforced: false`). Every change is an event row and an
`ai.brutor.incident` record; the response's `seal` block says whether it
sealed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin._compliance_http import SealStatus, client_info, seal_status
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
from app.database.models.compliance_incident import (
    INCIDENT_STATUSES,
    ComplianceIncident,
    ComplianceIncidentEvent,
)
from app.database.models.resource_mixins import actor_name
from app.services.compliance import incidents as svc

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DeadlineOut(BaseModel):
    framework_id: str
    rule: str
    stage: str
    #: None = the stage's clock has not started (`status` waiting).
    due_at: Optional[str] = None
    status: str = Field(..., description=(
        "open | due_soon | overdue | reported_on_time | reported_late | closed | "
        "waiting (anchor stage not submitted) | closed_unsubmitted"
    ))
    hours_remaining: Optional[float] = None
    anchor: str = Field("aware", description="aware | classified | stage:<name>")
    anchor_at: Optional[str] = None
    #: `classification` (due bounded by awareness until classified_at is
    #: recorded) or the stage whose submission starts this clock.
    waiting_for: Optional[str] = None
    submitted_at: Optional[str] = None
    #: True for a stage of a staged regime (DORA, NIS2): recorded with
    #: POST …/submissions, not /report.
    staged: bool = False


class IncidentOut(BaseModel):
    id: str
    tenant_id: str
    ai_system_id: Optional[str] = None
    title: str
    description: str = ""
    classification: str
    severity: str
    became_aware_at: Optional[str] = None
    classified_at: Optional[str] = None
    frameworks: List[str] = Field(default_factory=list)
    deadlines: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    stage_submissions: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    deadline_status: List[DeadlineOut] = Field(default_factory=list)
    deadlines_enforced: bool = False
    status: str
    reported_at: Optional[str] = None
    authority: Optional[str] = None
    external_reference: Optional[str] = None
    linked_run_ids: List[str] = Field(default_factory=list)
    linked_inbox_item_ids: List[str] = Field(default_factory=list)
    last_record_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class IncidentEventOut(BaseModel):
    id: str
    incident_id: str
    tenant_id: str
    event: str
    at: Optional[str] = None
    actor_id: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)
    record_id: Optional[str] = None


class IncidentListResponse(BaseModel):
    items: List[IncidentOut]
    total: int


class IncidentDetail(IncidentOut):
    events: List[IncidentEventOut] = Field(default_factory=list)


class IncidentChangeResponse(BaseModel):
    incident: IncidentOut
    event: Optional[IncidentEventOut] = None
    seal: SealStatus


class IncidentEventListResponse(BaseModel):
    items: List[IncidentEventOut]
    total: int


class IncidentCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=20000)
    classification: str = Field(..., min_length=1, max_length=64,
                                description="e.g. serious_incident, death, widespread, major_incident, breach")
    severity: str = Field(..., description="low | medium | high | critical")
    became_aware_at: datetime
    classified_at: Optional[datetime] = Field(
        None, description="When the incident was classified as major/significant (>= became_aware_at)"
    )
    frameworks: List[str] = Field(..., min_length=1)
    ai_system_id: Optional[str] = None
    linked_run_ids: List[str] = Field(default_factory=list)
    linked_inbox_item_ids: List[str] = Field(default_factory=list)


class IncidentUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=20000)
    classification: Optional[str] = Field(None, min_length=1, max_length=64)
    severity: Optional[str] = None
    became_aware_at: Optional[datetime] = None
    classified_at: Optional[datetime] = None
    frameworks: Optional[List[str]] = None
    ai_system_id: Optional[str] = None
    linked_run_ids: Optional[List[str]] = None
    linked_inbox_item_ids: Optional[List[str]] = None
    authority: Optional[str] = Field(None, max_length=255)
    external_reference: Optional[str] = Field(None, max_length=255)


class IncidentReportRequest(BaseModel):
    authority: str = Field(..., min_length=1, max_length=255)
    reported_at: Optional[datetime] = None
    external_reference: Optional[str] = Field(None, max_length=255)


class IncidentSubmissionRequest(BaseModel):
    framework_id: str = Field(..., min_length=1, max_length=64)
    stage: str = Field(..., min_length=1, max_length=64, description="e.g. initial, intermediate, final")
    submitted_at: Optional[datetime] = None


class IncidentCloseRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=4000)


class IncidentNoteRequest(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _out(incident: ComplianceIncident) -> IncidentOut:
    return IncidentOut(**svc.incident_view(incident))


def _event_out(event: ComplianceIncidentEvent) -> IncidentEventOut:
    return IncidentEventOut(**event.to_dict())


def create_compliance_incidents_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/compliance/incidents", tags=["Compliance Incidents"])

    def _load(db: Session, tenant: str, incident_id: str) -> ComplianceIncident:
        row = db.get(ComplianceIncident, incident_id)
        if row is None or row.tenant_id != tenant:
            raise HTTPException(status_code=404, detail="Incident not found")
        return row

    def _events(db: Session, incident_id: str) -> List[ComplianceIncidentEvent]:
        return (
            db.query(ComplianceIncidentEvent)
            .filter(ComplianceIncidentEvent.incident_id == incident_id)
            .order_by(ComplianceIncidentEvent.at.asc(), ComplianceIncidentEvent.id.asc())
            .all()
        )

    def _respond(change: svc.Change) -> IncidentChangeResponse:
        return IncidentChangeResponse(
            incident=_out(change.incident), event=_event_out(change.event), seal=seal_status(change.seal)
        )

    def _audit(audit, db, request, admin, admin_type, tenant, action, incident, change):
        audit.log(
            action=action,
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant,
            success=True,
            resource_type="compliance_incident",
            resource_id=incident.id,
            details={
                "status": incident.status,
                "frameworks": incident.frameworks,
                "record_id": change.seal.record_id if change else None,
                "sealed": bool(change and change.seal.sealed),
            },
            db=db,
            **client_info(request),
        )

    def _guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except svc.IncidentError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.get("", response_model=IncidentListResponse, summary="The incident register")
    def list_incidents(
        status: Optional[str] = Query(None, description="open | reported | closed"),
        framework: Optional[str] = Query(None),
        ai_system_id: Optional[str] = Query(None),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> IncidentListResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        if status is not None and status not in INCIDENT_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(INCIDENT_STATUSES)}")
        q = db.query(ComplianceIncident).filter(ComplianceIncident.tenant_id == tenant)
        if status:
            q = q.filter(ComplianceIncident.status == status)
        if ai_system_id:
            q = q.filter(ComplianceIncident.ai_system_id == ai_system_id)
        rows = q.order_by(ComplianceIncident.became_aware_at.desc()).all()
        if framework:
            rows = [r for r in rows if framework in (r.frameworks or [])]
        return IncidentListResponse(items=[_out(r) for r in rows], total=len(rows))

    @router.post("", response_model=IncidentChangeResponse, status_code=201, summary="Open an incident")
    def create_incident(
        request: Request,
        body: IncidentCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> IncidentChangeResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        change = _guard(
            svc.create_incident, db, tenant,
            title=body.title, description=body.description, classification=body.classification,
            severity=body.severity, became_aware_at=body.became_aware_at, frameworks=body.frameworks,
            ai_system_id=body.ai_system_id, linked_run_ids=body.linked_run_ids,
            linked_inbox_item_ids=body.linked_inbox_item_ids, classified_at=body.classified_at,
            actor=actor_name(admin),
        )
        db.commit()
        _audit(audit, db, request, admin, admin_type, tenant, "compliance_incident_create", change.incident, change)
        return _respond(change)

    @router.get("/{incident_id}", response_model=IncidentDetail, summary="One incident with its history")
    def get_incident(
        incident_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> IncidentDetail:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, incident_id)
        return IncidentDetail(
            **svc.incident_view(row), events=[_event_out(e) for e in _events(db, row.id)]
        )

    @router.patch("/{incident_id}", response_model=IncidentChangeResponse, summary="Change an incident")
    def update_incident(
        request: Request,
        incident_id: str,
        body: IncidentUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> IncidentChangeResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, incident_id)
        changes = body.model_dump(exclude_unset=True)
        change = _guard(svc.update_incident, db, row, changes, actor=actor_name(admin))
        db.commit()
        if change is None:
            return IncidentChangeResponse(
                incident=_out(row), event=None, seal=seal_status(None, record_id=row.last_record_id)
            )
        _audit(audit, db, request, admin, admin_type, tenant, "compliance_incident_update", row, change)
        return _respond(change)

    @router.post("/{incident_id}/report", response_model=IncidentChangeResponse,
                 summary="Record that the incident was reported to an authority")
    def report_incident(
        request: Request,
        incident_id: str,
        body: IncidentReportRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> IncidentChangeResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, incident_id)
        change = _guard(
            svc.report_incident, db, row, authority=body.authority, reported_at=body.reported_at,
            external_reference=body.external_reference, actor=actor_name(admin),
        )
        db.commit()
        _audit(audit, db, request, admin, admin_type, tenant, "compliance_incident_report", row, change)
        return _respond(change)

    @router.post("/{incident_id}/submissions", response_model=IncidentChangeResponse,
                 summary="Record the submission of one stage of a staged report (DORA, NIS2)")
    def record_submission(
        request: Request,
        incident_id: str,
        body: IncidentSubmissionRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> IncidentChangeResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, incident_id)
        change = _guard(
            svc.record_submission, db, row, framework_id=body.framework_id, stage=body.stage,
            submitted_at=body.submitted_at, actor=actor_name(admin),
        )
        db.commit()
        _audit(audit, db, request, admin, admin_type, tenant, "compliance_incident_submission", row, change)
        return _respond(change)

    @router.post("/{incident_id}/close", response_model=IncidentChangeResponse, summary="Close an incident")
    def close_incident(
        request: Request,
        incident_id: str,
        body: IncidentCloseRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> IncidentChangeResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, incident_id)
        change = _guard(svc.close_incident, db, row, note=body.note, actor=actor_name(admin))
        db.commit()
        _audit(audit, db, request, admin, admin_type, tenant, "compliance_incident_close", row, change)
        return _respond(change)

    @router.get("/{incident_id}/events", response_model=IncidentEventListResponse,
                summary="The incident's sealed history")
    def list_events(
        incident_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_READ)),
    ) -> IncidentEventListResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, incident_id)
        events = _events(db, row.id)
        return IncidentEventListResponse(items=[_event_out(e) for e in events], total=len(events))

    @router.post("/{incident_id}/events", response_model=IncidentChangeResponse, status_code=201,
                 summary="Add a note to the incident's history")
    def add_note(
        request: Request,
        incident_id: str,
        body: IncidentNoteRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.COMPLIANCE_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> IncidentChangeResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        row = _load(db, tenant, incident_id)
        change = _guard(svc.add_note, db, row, note=body.note, actor=actor_name(admin))
        db.commit()
        _audit(audit, db, request, admin, admin_type, tenant, "compliance_incident_note", row, change)
        return _respond(change)

    return router


__all__ = ["create_compliance_incidents_router"]

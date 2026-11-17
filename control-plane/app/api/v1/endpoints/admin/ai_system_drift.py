# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Drift admin API

    GET   /v1/admin/ai-systems/{id}/baselines   — what normal looks like
    GET   /v1/admin/ai-systems/{id}/drift       — detected changes
    GET   /v1/admin/drift                       — tenant-wide feed
    GET   /v1/admin/ai-systems/{id}/baseline-epochs — baseline epochs
    POST  /v1/admin/drift/{id}/ack
    POST  /v1/admin/drift/{id}/resolve          — kind + note REQUIRED
    POST  /v1/admin/drift/{id}/false-positive   — kind false_positive + note

Read/triage only. Detection runs in the Rust core alongside the run
sweeper and the liveness detector.

Closing a finding requires a KIND, and each kind has one effect
the detector honours (`app/services/drift_resolution.py`): accepted_change
writes a baseline epoch at the change point, cause_fixed an incident
window, false_positive an epoch at the detection time. A close without a
kind is a 422: the platform decides nothing silently. `ack` is unchanged
and never closes.

`false-positive` is a first-class outcome rather than a variant of
"resolved". A detector that cannot be told it was wrong gets ignored
inside a month, and the distinction is what lets per-system thresholds be
tuned from real operator judgement rather than guesswork.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.admin_user.dependencies import get_current_admin, require_permission
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.api.v1.endpoints.admin._compliance_http import SealStatus, seal_status
from app.api.v1.models.admin.drift_resolution import (
    BaselineEpochListResponse,
    BaselineEpochResponse,
    CloseDriftRequest,
    IncidentWindowResponse,
    ResolutionKind,
)
from app.constants.drift_resolution import KIND_FALSE_POSITIVE
from app.database.models.ai_system_drift import (
    DRIFT_ACKNOWLEDGED,
    DRIFT_ACTIVE,
    DRIFT_CLASSES,
    DRIFT_STATUSES,
    AISystemBaseline,
    AISystemDriftEvent,
)
from app.database.models.ai_system_drift_resolution import AISystemBaselineEpoch
from app.services import drift_resolution
from app.services.ai_system_judgement_exclusion import (
    epoch_covers,
    window_findings,
    window_run_count,
)


class BaselineResponse(BaseModel):
    id: str
    ai_system_id: str
    metric_key: str
    bucket: str
    actor_id: Optional[str] = None
    basis: str = "runs"
    caller_system_id: str = "all"
    median: Optional[float] = None
    mad: Optional[float] = None
    p50: Optional[float] = None
    p90: Optional[float] = None
    p99: Optional[float] = None
    distribution: dict = Field(default_factory=dict)
    sample_count: int = 0
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    state: str
    is_ready: bool = False
    computed_at: Optional[str] = None
    epoch: Optional[BaselineEpochResponse] = Field(
        None,
        description=(
            "The latest baseline epoch in force for this baseline's "
            "(metric, basis, caller | 'all'): samples before it are excluded and the "
            "baseline re-learns from it ('re-learning since … — accepted change by …')."
        ),
    )


class BaselineListResponse(BaseModel):
    baselines: List[BaselineResponse]
    total: int
    learning: int = Field(
        0,
        description=(
            "Baselines still gathering runs. These never alert — the UI "
            "should say 'ready in ~N runs' rather than render a confident "
            "chart from too little data."
        ),
    )


class DriftEventResponse(BaseModel):
    id: str
    ai_system_id: str
    actor_id: Optional[str] = None
    basis: str = "runs"
    caller_system_id: Optional[str] = None
    drift_class: str
    metric_key: str
    detected_at: Optional[str] = None
    change_point_at: Optional[str] = Field(
        None,
        description=(
            "When the behaviour moved, not when we noticed. This is what "
            "the cause ladder matched candidates against."
        ),
    )
    baseline_value: Optional[float] = None
    observed_value: Optional[float] = None
    effect_size: Optional[float] = None
    direction: Optional[str] = None
    severity: str
    attributed_cause: str
    cause_confidence: Optional[float] = None
    cause_evidence: dict = Field(default_factory=dict)
    correlated_metrics: list = Field(default_factory=list)
    response_taken: dict = Field(default_factory=dict)
    system_alert_id: Optional[str] = None
    status: str
    resolution_note: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_kind: Optional[ResolutionKind] = Field(
        None,
        description=(
            "How it was closed: accepted_change | cause_fixed | "
            "false_positive (a human) or auto_cleared (the control plane). Null while open."
        ),
    )
    is_open: bool = True
    is_explained: bool = False


class DriftResolutionResponse(DriftEventResponse):
    """The closed finding and the one effect its kind has on detection."""

    baseline_epoch: Optional[BaselineEpochResponse] = None
    incident_window: Optional[IncidentWindowResponse] = None
    seal: SealStatus


class DriftListResponse(BaseModel):
    events: List[DriftEventResponse]
    total: int
    open_count: int = 0
    unexplained_count: int = Field(
        0,
        description=(
            "Open findings the cause ladder could not explain. A high "
            "proportion here is a signal about the *detector*, not the "
            "systems — it means alerts are arriving without the context "
            "that makes them actionable."
        ),
    )


class AckRequest(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _epochs(db: Session, tenant_id: str, ai_system_id: str) -> List[AISystemBaselineEpoch]:
    return (
        db.query(AISystemBaselineEpoch)
        .filter(
            AISystemBaselineEpoch.tenant_id == tenant_id,
            AISystemBaselineEpoch.ai_system_id == ai_system_id,
        )
        .order_by(AISystemBaselineEpoch.starts_at.desc(), AISystemBaselineEpoch.id.asc())
        .all()
    )


def _epoch_in_force(
    epochs: List[AISystemBaselineEpoch], baseline: AISystemBaseline
) -> Optional[BaselineEpochResponse]:
    """The latest epoch on the baseline's (metric, basis) for its caller or
    'all' — the same lookup as `ai_system_judgement_exclusion.epoch_floor`."""
    for e in epochs:  # newest first
        if epoch_covers(e, baseline.metric_key, baseline.basis, baseline.caller_system_id):
            return BaselineEpochResponse(**e.to_dict())
    return None


def create_ai_system_drift_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(tags=["AI System Drift"])

    # ------------------------------------------------------------------
    # Baselines
    # ------------------------------------------------------------------

    @router.get(
        "/admin/ai-systems/{ai_system_id}/baselines",
        response_model=BaselineListResponse,
        summary="What normal looks like for this AI System",
    )
    def list_baselines(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> BaselineListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)

        rows = (
            db.query(AISystemBaseline)
            .filter(
                AISystemBaseline.tenant_id == resolved,
                AISystemBaseline.ai_system_id == ai_system_id,
            )
            .order_by(AISystemBaseline.metric_key.asc())
            .all()
        )
        epochs = _epochs(db, resolved, ai_system_id)
        return BaselineListResponse(
            baselines=[
                BaselineResponse(**b.to_dict(), epoch=_epoch_in_force(epochs, b)) for b in rows
            ],
            total=len(rows),
            learning=sum(1 for b in rows if not b.is_ready),
        )

    @router.get(
        "/admin/ai-systems/{ai_system_id}/baseline-epochs",
        response_model=BaselineEpochListResponse,
        summary="Baseline epochs declared for this AI System",
    )
    def list_baseline_epochs(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> BaselineEpochListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        epochs = _epochs(db, resolved, ai_system_id)
        return BaselineEpochListResponse(
            epochs=[BaselineEpochResponse(**e.to_dict()) for e in epochs], total=len(epochs)
        )

    # ------------------------------------------------------------------
    # Drift events
    # ------------------------------------------------------------------

    def _query(db, tenant_id, ai_system_id, status, drift_class, period_hours):
        q = db.query(AISystemDriftEvent).filter(
            AISystemDriftEvent.tenant_id == tenant_id
        )
        if ai_system_id:
            q = q.filter(AISystemDriftEvent.ai_system_id == ai_system_id)
        if status:
            q = q.filter(AISystemDriftEvent.status == status)
        if drift_class:
            q = q.filter(AISystemDriftEvent.drift_class == drift_class)
        if period_hours:
            q = q.filter(
                AISystemDriftEvent.detected_at
                >= datetime.now(timezone.utc) - timedelta(hours=period_hours)
            )
        return q.order_by(AISystemDriftEvent.detected_at.desc())

    def _validate_filters(status: Optional[str], drift_class: Optional[str]) -> None:
        if status is not None and status not in DRIFT_STATUSES:
            raise HTTPException(
                status_code=422, detail=f"status must be one of {sorted(DRIFT_STATUSES)}"
            )
        if drift_class is not None and drift_class not in DRIFT_CLASSES:
            raise HTTPException(
                status_code=422,
                detail=f"drift_class must be one of {sorted(DRIFT_CLASSES)}",
            )

    def _list_response(rows: List[AISystemDriftEvent]) -> DriftListResponse:
        return DriftListResponse(
            events=[DriftEventResponse(**e.to_dict()) for e in rows],
            total=len(rows),
            open_count=sum(1 for e in rows if e.is_open),
            unexplained_count=sum(1 for e in rows if e.is_open and not e.is_explained),
        )

    @router.get(
        "/admin/ai-systems/{ai_system_id}/drift",
        response_model=DriftListResponse,
        summary="Drift findings for one AI System",
    )
    def list_system_drift(
        ai_system_id: str,
        tenant_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        drift_class: Optional[str] = Query(None),
        period_hours: int = Query(720, ge=0, le=8760),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> DriftListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _validate_filters(status, drift_class)
        return _list_response(
            _query(db, resolved, ai_system_id, status, drift_class, period_hours).all()
        )

    @router.get(
        "/admin/drift",
        response_model=DriftListResponse,
        summary="Tenant-wide drift feed",
    )
    def list_drift(
        tenant_id: Optional[str] = Query(None),
        status: Optional[str] = Query(DRIFT_ACTIVE),
        drift_class: Optional[str] = Query(None),
        period_hours: int = Query(720, ge=0, le=8760),
        limit: int = Query(100, ge=1, le=500),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_READ)),
    ) -> DriftListResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        _validate_filters(status, drift_class)
        return _list_response(
            _query(db, resolved, None, status, drift_class, period_hours).limit(limit).all()
        )

    # ------------------------------------------------------------------
    # Triage
    # ------------------------------------------------------------------

    def _load(db, tenant_id: str, event_id: str) -> AISystemDriftEvent:
        e = (
            db.query(AISystemDriftEvent)
            .filter(
                AISystemDriftEvent.id == event_id,
                AISystemDriftEvent.tenant_id == tenant_id,
            )
            .first()
        )
        if e is None:
            raise HTTPException(status_code=404, detail=f"Drift event {event_id!r} not found")
        return e

    def _close(db, admin, tenant_id, event_id, payload: CloseDriftRequest) -> DriftResolutionResponse:
        event = _load(db, tenant_id, event_id)
        try:
            outcome = drift_resolution.resolve_finding(
                db,
                event,
                kind=payload.kind,
                by=getattr(admin, "username", None),
                note=payload.note,
                window_starts_at=payload.window_starts_at,
                window_ends_at=payload.window_ends_at,
                cause_category=payload.cause_category,
                tenant_wide=payload.tenant_wide,
                incident_window_id=payload.incident_window_id,
            )
        except drift_resolution.ResolutionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except drift_resolution.ResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        epoch = outcome.baseline_epoch
        window = outcome.incident_window
        return DriftResolutionResponse(
            **outcome.event.to_dict(),
            baseline_epoch=BaselineEpochResponse(**epoch.to_dict()) if epoch else None,
            incident_window=(
                IncidentWindowResponse(
                    **window.to_dict(),
                    runs_excluded=window_run_count(db, window),
                    drift_event_ids=window_findings(db, [window.id])[window.id],
                )
                if window else None
            ),
            seal=seal_status(outcome.seal),
        )

    @router.post(
        "/admin/drift/{event_id}/ack",
        response_model=DriftEventResponse,
        summary="Acknowledge a drift finding",
    )
    def ack(
        event_id: str,
        tenant_id: Optional[str] = Query(None),
        payload: AckRequest = Body(default=AckRequest()),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> DriftEventResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        event = _load(db, resolved, event_id)
        if not event.is_open:
            raise HTTPException(
                status_code=409, detail=f"Drift event {event_id!r} is already closed"
            )
        event.status = DRIFT_ACKNOWLEDGED
        if payload.note:
            event.resolution_note = payload.note
        db.commit()
        db.refresh(event)
        return DriftEventResponse(**event.to_dict())

    @router.post(
        "/admin/drift/{event_id}/resolve",
        response_model=DriftResolutionResponse,
        summary="Close a drift finding with a kind",
        description=(
            "`kind` and `note` are required. accepted_change → a baseline epoch at the "
            "change point; cause_fixed → an incident window (default [change point, now]); "
            "false_positive → status false_positive and an epoch at detection time. The "
            "resolution is sealed as a declaration."
        ),
    )
    def resolve(
        event_id: str,
        payload: CloseDriftRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> DriftResolutionResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        return _close(db, admin, resolved, event_id, payload)

    @router.post(
        "/admin/drift/{event_id}/false-positive",
        response_model=DriftResolutionResponse,
        summary="Mark a drift finding as a false positive",
        description=(
            "Distinct from 'resolved' on purpose. Resolved means the drift "
            "was real and has been dealt with; false-positive means the "
            "detector was wrong. Only the second should influence future "
            "thresholds, and a detector that cannot be told it was wrong "
            "gets ignored inside a month. `kind` must be false_positive and a "
            "note is required; the finding's evidence up to its "
            "detection is not read again for that metric."
        ),
    )
    def false_positive(
        event_id: str,
        payload: CloseDriftRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.RESOURCE_GROUP_UPDATE)),
    ) -> DriftResolutionResponse:
        admin, admin_type = admin_and_type
        resolved = _resolve_tenant_id(admin, admin_type, tenant_id)
        if payload.kind != KIND_FALSE_POSITIVE:
            raise HTTPException(
                status_code=422,
                detail="kind must be false_positive on this route; use /resolve for the others",
            )
        result = _close(db, admin, resolved, event_id, payload)
        logger.info(
            "Drift finding marked false positive",
            context={
                "tenant_id": resolved,
                "event_id": event_id,
                "metric_key": result.metric_key,
                "drift_class": result.drift_class,
            },
        )
        return result

    return router

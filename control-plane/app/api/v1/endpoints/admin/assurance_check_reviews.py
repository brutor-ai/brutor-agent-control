# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Blind human reviews of judged verdicts

    GET  /admin/assurance-checks/{check_id}/reviews?status=pending   — the queue (blind while pending)
    POST /admin/assurance-checks/{check_id}/reviews/{review_id}      — grade one → adjudication record
    POST /admin/assurance-checks/{check_id}/reviews/sample?week=     — draw a week's sample now (idempotent)
    GET  /admin/assurance-checks/{check_id}/agreement                — human agreement, current fingerprint

While a review is pending its `judged_result` / `judged_score` are withheld
(the reviewer grades blind); they are revealed once graded. The reviewer
identity stays on the review row and never enters the record.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin._compliance_http import (
    SealStatus,
    client_info,
    parse_day,
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
from app.database.models.assurance_check import CHECK_TIER_SEMANTIC, AssuranceCheck
from app.database.models.assurance_check_calibration import REVIEW_STATUSES, AssuranceCheckReview
from app.database.models.resource_mixins import actor_name
from app.services.compliance import judged


class ReviewOut(BaseModel):
    id: str
    tenant_id: str
    check_id: str
    evaluator_fingerprint: str
    week: Optional[str] = None
    sample_key: str
    assessment_record_id: Optional[str] = None
    judged_result: Optional[str] = Field(None, description="Withheld while the review is pending (blind)")
    judged_score: Optional[float] = None
    human_result: Optional[str] = None
    human_score: Optional[float] = None
    status: str
    blind: bool = True
    reviewer_id: Optional[str] = None
    reviewed_at: Optional[str] = None
    adjudication_record_id: Optional[str] = None
    created_at: Optional[str] = None


class AgreementOut(BaseModel):
    """Judge–human agreement for `(check, evaluator_fingerprint)` over 30
    days of graded blind reviews (`compliance.agreement.summarize`)."""

    check_id: str
    evaluator_fingerprint: Optional[str] = None
    n: int = 0
    agreements: int = 0
    observed_agreement: Optional[float] = None
    kappa: Optional[float] = None
    ci95: Optional[List[Optional[float]]] = Field(None, description="Wilson 95 % interval")
    mae: Optional[float] = None
    numeric_n: int = 0
    blind: bool = True
    insufficient_sample: bool = True
    below_threshold: bool = False
    threshold: float = 0.8
    window_days: int = judged.AGREEMENT_WINDOW_DAYS
    label: Optional[str] = Field(None, description="e.g. 'human agreement 0.92 [0.83–0.97], n = 34, blind'")


class ReviewListResponse(BaseModel):
    items: List[ReviewOut]
    total: int
    counts: Dict[str, int]
    agreement: AgreementOut


class ReviewGradeRequest(BaseModel):
    human_result: str = Field(..., description="met | not_met | n/a")
    human_score: Optional[float] = Field(None, ge=0.0, le=1.0)


class ReviewGradeResponse(BaseModel):
    review: ReviewOut
    agrees_with_judge: bool
    seal: SealStatus
    agreement: AgreementOut


class SampleResponse(BaseModel):
    check_id: str
    week: str
    created: int
    items: List[ReviewOut]


def agreement_out(stats: Dict[str, Any]) -> AgreementOut:
    known = set(AgreementOut.model_fields)
    return AgreementOut(**{k: v for k, v in stats.items() if k in known})


def create_assurance_check_reviews_router(logger: BrutorLogger) -> APIRouter:
    router = APIRouter(prefix="/admin/assurance-checks", tags=["Assurance Checks"])

    def _check(db: Session, tenant: str, check_id: str) -> AssuranceCheck:
        row = db.get(AssuranceCheck, check_id)
        if row is None or row.tenant_id != tenant:
            raise HTTPException(status_code=404, detail="Check not found")
        return row

    @router.get("/{check_id}/reviews", response_model=ReviewListResponse,
                summary="Blind human-review queue for a judged check")
    def list_reviews(
        check_id: str,
        status: Optional[str] = Query(None, description="pending | graded | skipped"),
        limit: int = Query(100, ge=1, le=500),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> ReviewListResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        check = _check(db, tenant, check_id)
        if status is not None and status not in REVIEW_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {sorted(REVIEW_STATUSES)}")
        q = db.query(AssuranceCheckReview).filter(
            AssuranceCheckReview.tenant_id == tenant, AssuranceCheckReview.check_id == check.id
        )
        if status:
            q = q.filter(AssuranceCheckReview.status == status)
        rows = q.order_by(AssuranceCheckReview.week.desc(), AssuranceCheckReview.id).limit(limit).all()
        counts = judged.queue_counts(db, tenant, [check.id]).get(
            check.id, {"pending": 0, "graded": 0, "skipped": 0}
        )
        return ReviewListResponse(
            items=[ReviewOut(**r.to_dict(reveal_judged=False)) for r in rows],
            total=len(rows),
            counts=counts,
            agreement=agreement_out(judged.agreement_for(db, check)),
        )

    @router.post("/{check_id}/reviews/sample", response_model=SampleResponse,
                 summary="Draw a week's deterministic blind sample now (idempotent)")
    def draw_sample(
        check_id: str,
        week: Optional[str] = Query(None, description="Any day of the week (default: last week)"),
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
    ) -> SampleResponse:
        from datetime import datetime, timedelta, timezone

        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        check = _check(db, tenant, check_id)
        if check.tier != CHECK_TIER_SEMANTIC:
            raise HTTPException(status_code=422, detail="Only judged (semantic) checks are sampled for review")
        day = parse_day(week, "week") or (
            judged.week_start(datetime.now(timezone.utc).date()) - timedelta(days=7)
        )
        monday = judged.week_start(day)
        created = judged.sample_week(db, check, monday)
        db.commit()
        return SampleResponse(
            check_id=check.id,
            week=monday.isoformat(),
            created=len(created),
            items=[ReviewOut(**r.to_dict(reveal_judged=False)) for r in created],
        )

    @router.post("/{check_id}/reviews/{review_id}", response_model=ReviewGradeResponse,
                 summary="Grade one sampled verdict (seals an adjudication record)")
    def grade_review(
        request: Request,
        check_id: str,
        review_id: str,
        body: ReviewGradeRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_MANAGE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> ReviewGradeResponse:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        check = _check(db, tenant, check_id)
        review = db.get(AssuranceCheckReview, review_id)
        if review is None or review.tenant_id != tenant or review.check_id != check.id:
            raise HTTPException(status_code=404, detail="Review not found")
        try:
            grade = judged.grade_review(
                db, check, review, human_result=body.human_result, human_score=body.human_score,
                reviewer=actor_name(admin),
            )
        except judged.ReviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        # A grade can move agreement across the threshold either way.
        judged.evaluate_calibration(db, check)
        db.commit()
        audit.log(
            action="assurance_check_review_grade",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant,
            success=True,
            resource_type="assurance_check_review",
            resource_id=review.id,
            details={
                "check_id": check.id,
                "human_result": body.human_result,
                "agrees_with_judge": grade.agrees,
                "record_id": grade.seal.record_id,
            },
            db=db,
            **client_info(request),
        )
        return ReviewGradeResponse(
            review=ReviewOut(**review.to_dict(reveal_judged=True)),
            agrees_with_judge=grade.agrees,
            seal=seal_status(grade.seal),
            agreement=agreement_out(judged.agreement_for(db, check, fingerprint=review.evaluator_fingerprint)),
        )

    @router.get("/{check_id}/agreement", response_model=AgreementOut,
                summary="Human agreement with the judge over 30 days (current fingerprint)")
    def get_agreement(
        check_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.ASSURANCE_CHECK_READ)),
    ) -> AgreementOut:
        admin, admin_type = admin_and_type
        tenant = resolve_tenant_id(admin, admin_type, tenant_id)
        return agreement_out(judged.agreement_for(db, _check(db, tenant, check_id)))

    return router


__all__ = ["create_assurance_check_reviews_router"]

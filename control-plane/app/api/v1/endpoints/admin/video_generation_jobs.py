# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Video Generation Jobs Admin Endpoints

Read + management endpoints for the `video_generation_jobs` table.
Job rows are created and progressed by the Rust proxy's polling
loop (`services/video_pipeline.rs`); this module gives the admin
UI a way to inspect, cancel, and delete them tenant-scoped.

Endpoint surface intentionally mirrors `batch_jobs.py` — same
query/list/detail/cancel/stats shapes — so the admin UI's filter
bar / pagination / detail panel can reuse the patterns it
already has for batch.
"""

import math
from typing import Optional, Tuple
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Depends, Query, Request, status
from sqlalchemy import func as sa_func, desc, asc, case
from sqlalchemy.orm import Session

from app.database import get_db
from app.database.models.video_generation_job import VideoGenerationJob
from app.database.models.llm_model import LLModel
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    acting_tenant_id,
    require_permission,
    get_audit_logger,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.video_generation_job import (
    VideoJobQueryRequest,
    VideoJobListResponse,
    VideoJobSummary,
    VideoJobDetail,
    VideoJobDetailResponse,
    VideoJobCancelResponse,
    VideoJobDeleteResponse,
    VideoStatisticsResponse,
    VideoStatusCount,
    VideoModelStats,
    VideoJobPaginationInfo,
    VIDEO_JOB_TERMINAL_STATUSES,
)
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


# Cap the error preview at this many chars in list responses. Long
# fal stack traces would balloon a 200-row page to hundreds of KB
# otherwise; the detail endpoint serves the full string when the
# operator clicks through.
_ERROR_PREVIEW_CHARS = 160


def _resolve_model_name(db: Session, model_id: Optional[str]) -> Optional[str]:
    """Cheap denormalisation — list rows want the model display
    name without a per-row JOIN. Cached at request scope by the
    caller via a dict.
    """
    if not model_id:
        return None
    row = db.query(LLModel.name).filter(LLModel.id == model_id).first()
    return row[0] if row else None


def _row_to_summary(
    row: VideoGenerationJob,
    model_name_cache: dict,
    db: Session,
) -> VideoJobSummary:
    if row.model_id and row.model_id not in model_name_cache:
        model_name_cache[row.model_id] = _resolve_model_name(db, row.model_id)
    err = row.error or None
    err_preview = None
    if err:
        err_preview = err[:_ERROR_PREVIEW_CHARS]
        if len(err) > _ERROR_PREVIEW_CHARS:
            err_preview = err_preview.rstrip() + "…"
    return VideoJobSummary(
        id=row.id,
        tenant_id=row.tenant_id,
        model_id=row.model_id,
        model_name=model_name_cache.get(row.model_id) if row.model_id else None,
        actor_id=row.actor_id,
        status=row.status,
        resolution=row.resolution,
        duration_seconds=row.duration_seconds,
        cost_usd=row.cost_usd,
        has_output=bool(row.output_file_id),
        error_preview=err_preview,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _row_to_detail(
    row: VideoGenerationJob,
    db: Session,
) -> VideoJobDetail:
    summary = _row_to_summary(row, {}, db)
    return VideoJobDetail(
        **summary.model_dump(),
        prompt=row.prompt,
        provider_request_id=row.provider_request_id,
        output_file_id=row.output_file_id,
        chat_id=row.chat_id,
        error=row.error,
    )


def _get_or_404(db: Session, tenant_id: str, job_id: str) -> VideoGenerationJob:
    row = db.query(VideoGenerationJob).filter(
        VideoGenerationJob.id == job_id,
        VideoGenerationJob.tenant_id == tenant_id,
    ).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video job {job_id} not found",
        )
    return row


def create_video_generation_jobs_admin_router(logger: BrutorLogger) -> APIRouter:
    """Create the admin router for video-generation job management."""

    router = APIRouter(
        prefix="/admin/video-generation-jobs",
        tags=["Video Generation Jobs"],
    )

    # =========================================================================
    # QUERY (list)
    # =========================================================================

    @router.post(
        "/query",
        response_model=VideoJobListResponse,
        summary="Query video generation jobs",
    )
    def query_video_jobs(
        request: Request,
        body: VideoJobQueryRequest,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.AI_MODEL_READ)),
    ):
        tenant_id = acting_tenant_id(request, admin)

        query = db.query(VideoGenerationJob).filter(
            VideoGenerationJob.tenant_id == tenant_id
        )

        # Time range. `hours_ago` wins over explicit start/end so the
        # admin UI's "last N hours" quick-filter doesn't conflict
        # with stale start_time state.
        if body.hours_ago:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=body.hours_ago)
            query = query.filter(VideoGenerationJob.created_at >= cutoff)
        else:
            if body.start_time:
                query = query.filter(VideoGenerationJob.created_at >= body.start_time)
            if body.end_time:
                query = query.filter(VideoGenerationJob.created_at <= body.end_time)

        if body.status:
            query = query.filter(VideoGenerationJob.status.in_(body.status))
        if body.model_id:
            query = query.filter(VideoGenerationJob.model_id == body.model_id)
        if body.actor_id:
            query = query.filter(VideoGenerationJob.actor_id == body.actor_id)

        total = query.count()

        sort_col = {
            "created_at": VideoGenerationJob.created_at,
            "updated_at": VideoGenerationJob.updated_at,
            "completed_at": VideoGenerationJob.completed_at,
            "cost_usd": VideoGenerationJob.cost_usd,
            "duration_seconds": VideoGenerationJob.duration_seconds,
        }[body.sort_by]
        order = desc(sort_col) if body.sort_order == "desc" else asc(sort_col)

        rows = (
            query.order_by(order)
            .offset((body.page - 1) * body.page_size)
            .limit(body.page_size)
            .all()
        )

        # Bulk-resolve model names in one round-trip rather than
        # N+1.
        model_ids = {r.model_id for r in rows if r.model_id}
        model_name_cache: dict = {}
        if model_ids:
            for mid, mname in db.query(LLModel.id, LLModel.name).filter(
                LLModel.id.in_(model_ids)
            ).all():
                model_name_cache[mid] = mname

        jobs = [_row_to_summary(r, model_name_cache, db) for r in rows]

        return VideoJobListResponse(
            jobs=jobs,
            pagination=VideoJobPaginationInfo(
                page=body.page,
                page_size=body.page_size,
                total=total,
                total_pages=max(1, math.ceil(total / body.page_size)) if total else 0,
            ),
        )

    # =========================================================================
    # DETAIL
    # =========================================================================

    @router.get(
        "/{job_id}",
        response_model=VideoJobDetailResponse,
        summary="Get a single video generation job",
    )
    def get_video_job(
        request: Request,
        job_id: str,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.AI_MODEL_READ)),
    ):
        row = _get_or_404(db, acting_tenant_id(request, admin), job_id)
        return VideoJobDetailResponse(job=_row_to_detail(row, db))

    # =========================================================================
    # CANCEL — flips a non-terminal row to `cancelled`. Idempotent.
    # =========================================================================

    @router.post(
        "/{job_id}/cancel",
        response_model=VideoJobCancelResponse,
        summary="Cancel an in-flight video job",
    )
    def cancel_video_job(
        job_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.AI_MODEL_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        # Note: this only flips the gateway's row to `cancelled` — it
        # does NOT call the upstream provider's cancel API. For fal
        # / Sora that means the upstream may still bill for the
        # clip; we just stop polling and unblock the caller. The
        # Rust proxy's DELETE endpoint has the same contract — see
        # `routes/video.rs::cancel_job`.
        row = _get_or_404(db, acting_tenant_id(request, admin), job_id)

        if row.status in VIDEO_JOB_TERMINAL_STATUSES:
            return VideoJobCancelResponse(
                job_id=row.id,
                new_status=row.status,
                message=f"Job already in terminal state: {row.status}",
            )

        row.status = "cancelled"
        row.error = (row.error or "cancelled by admin")
        row.updated_at = datetime.now(timezone.utc)
        row.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)

        audit.log(
            action="video_job_cancel",
            db=db,
            admin=admin,
            admin_type=admin.admin_type if hasattr(admin, "admin_type") else None,
            success=True,
            resource_type="video_generation_job",
            resource_id=row.id,
            tenant_id=row.tenant_id,
            details={"prior_status": row.status},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        return VideoJobCancelResponse(
            job_id=row.id,
            new_status=row.status,
            message="Cancelled by admin",
        )

    # =========================================================================
    # DELETE — permanently removes a row. Restricted to terminal
    # rows so we don't orphan an in-flight upstream job.
    # =========================================================================

    @router.delete(
        "/{job_id}",
        response_model=VideoJobDeleteResponse,
        summary="Delete a terminal video generation job",
    )
    def delete_video_job(
        job_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.AI_MODEL_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        row = _get_or_404(db, acting_tenant_id(request, admin), job_id)

        if row.status not in VIDEO_JOB_TERMINAL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Refusing to delete non-terminal job (status="
                    f"{row.status!r}). Cancel first, then delete."
                ),
            )

        prior_status = row.status
        db.delete(row)
        db.commit()

        audit.log(
            action="video_job_delete",
            db=db,
            admin=admin,
            admin_type=admin.admin_type if hasattr(admin, "admin_type") else None,
            success=True,
            resource_type="video_generation_job",
            resource_id=job_id,
            tenant_id=acting_tenant_id(request, admin),
            details={"prior_status": prior_status},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        return VideoJobDeleteResponse(job_id=job_id, deleted=True)

    # =========================================================================
    # STATISTICS
    # =========================================================================

    @router.get(
        "/stats/summary",
        response_model=VideoStatisticsResponse,
        summary="Aggregate stats over the recent window",
    )
    def get_video_statistics(
        request: Request,
        hours_ago: Optional[int] = Query(
            None,
            ge=1,
            le=24 * 90,
            description="Limit aggregates to the last N hours.",
        ),
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.AI_MODEL_READ)),
    ):
        tenant_id = acting_tenant_id(request, admin)
        base = db.query(VideoGenerationJob).filter(
            VideoGenerationJob.tenant_id == tenant_id
        )
        if hours_ago:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
            base = base.filter(VideoGenerationJob.created_at >= cutoff)

        total = base.count()

        # Counts per status
        status_rows: list[Tuple[str, int]] = (
            base.with_entities(
                VideoGenerationJob.status,
                sa_func.count(VideoGenerationJob.id),
            )
            .group_by(VideoGenerationJob.status)
            .all()
        )
        by_status = [VideoStatusCount(status=s, count=c) for s, c in status_rows]
        status_counts = {s: c for s, c in status_rows}

        # Total cost
        total_cost = (
            base.with_entities(sa_func.coalesce(sa_func.sum(VideoGenerationJob.cost_usd), 0.0))
            .scalar()
            or 0.0
        )

        # Avg generated-clip duration (across rows with a recorded
        # duration — covers most succeeded ones).
        avg_dur = (
            base.with_entities(sa_func.avg(VideoGenerationJob.duration_seconds))
            .scalar()
        )

        # Avg wall-clock to completion across succeeded rows. Useful
        # for spotting upstream degradation across providers.
        avg_completion = (
            base
            .with_entities(
                sa_func.avg(
                    sa_func.extract(
                        "epoch",
                        VideoGenerationJob.completed_at - VideoGenerationJob.created_at,
                    )
                )
            )
            .filter(
                VideoGenerationJob.status == "succeeded",
                VideoGenerationJob.completed_at.isnot(None),
            )
            .scalar()
        )

        # Per-model breakdown — JOIN to llm_model for display name.
        # Empty list if there are no rows; the UI hides the section
        # in that case.
        model_rows = (
            base
            .join(LLModel, LLModel.id == VideoGenerationJob.model_id, isouter=True)
            .with_entities(
                VideoGenerationJob.model_id,
                LLModel.name.label("model_name"),
                sa_func.count(VideoGenerationJob.id).label("job_count"),
                sa_func.sum(
                    case((VideoGenerationJob.status == "succeeded", 1), else_=0)
                ).label("succeeded_count"),
                sa_func.sum(
                    case((VideoGenerationJob.status == "failed", 1), else_=0)
                ).label("failed_count"),
                sa_func.coalesce(sa_func.sum(VideoGenerationJob.cost_usd), 0.0).label("total_cost_usd"),
            )
            .group_by(VideoGenerationJob.model_id, LLModel.name)
            .all()
        )
        by_model = [
            VideoModelStats(
                model_id=r.model_id,
                model_name=r.model_name,
                job_count=int(r.job_count or 0),
                succeeded_count=int(r.succeeded_count or 0),
                failed_count=int(r.failed_count or 0),
                total_cost_usd=float(r.total_cost_usd or 0.0),
            )
            for r in model_rows
        ]

        return VideoStatisticsResponse(
            total_jobs=total,
            by_status=by_status,
            total_cost_usd=float(total_cost),
            succeeded_count=int(status_counts.get("succeeded", 0)),
            failed_count=int(status_counts.get("failed", 0)),
            cancelled_count=int(status_counts.get("cancelled", 0)),
            in_flight_count=int(status_counts.get("pending", 0))
            + int(status_counts.get("submitted", 0)),
            avg_duration_seconds=float(avg_dur) if avg_dur is not None else None,
            avg_completion_seconds=float(avg_completion) if avg_completion is not None else None,
            by_model=by_model,
        )

    return router

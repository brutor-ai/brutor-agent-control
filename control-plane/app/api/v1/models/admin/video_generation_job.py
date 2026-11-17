# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Video Generation Jobs — Admin API Pydantic Models

Wire shapes for the admin video-job dashboard (list, detail, cancel,
delete, stats). Mirrors `batch_job.py`'s style so the admin UI can
re-use the same query/pagination conventions.
"""

from typing import Optional, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field


# Lifecycle states the Rust proxy writes to `video_generation_jobs.status`.
VideoJobStatus = Literal[
    "pending",
    "submitted",
    "succeeded",
    "failed",
    "cancelled",
]

VIDEO_JOB_STATUSES: List[str] = [
    "pending",
    "submitted",
    "succeeded",
    "failed",
    "cancelled",
]

# Terminal states — won't transition further. Used by the UI to
# decide whether to show Cancel / Retry actions.
VIDEO_JOB_TERMINAL_STATUSES: List[str] = ["succeeded", "failed", "cancelled"]


class VideoJobPaginationInfo(BaseModel):
    """Standard pagination envelope, copied from the batch-jobs API."""
    page: int
    page_size: int
    total: int
    total_pages: int


class VideoJobSummary(BaseModel):
    """Row in the admin list view. Trimmed to the columns the table
    actually renders to keep the payload small under heavy filtering
    (the proxy logs every poll error to the row's `error` column,
    which can be hundreds of chars per row)."""
    id: str
    tenant_id: str
    model_id: Optional[str] = None
    model_name: Optional[str] = None
    actor_id: Optional[str] = None
    status: VideoJobStatus
    resolution: Optional[str] = None
    duration_seconds: Optional[int] = None
    cost_usd: Optional[float] = None
    has_output: bool = False
    error_preview: Optional[str] = Field(
        None,
        description=(
            "First ~160 chars of the `error` column. Full text is "
            "available via the detail endpoint — we truncate in the "
            "list response so a 500-row page doesn't ship 500 stack "
            "traces."
        ),
    )
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class VideoJobDetail(VideoJobSummary):
    """Full row for the detail view. Adds the prompt + provider
    request id + full error string."""
    prompt: str
    provider_request_id: Optional[str] = None
    output_file_id: Optional[str] = None
    chat_id: Optional[str] = None
    error: Optional[str] = None


class VideoJobQueryRequest(BaseModel):
    """Filter / sort / paginate input for the admin list endpoint.

    Mirrors `BatchJobQueryRequest` so the admin UI's filter bar can
    re-use the same controls. `status` accepts a list because
    operators commonly want "everything that's not done" (i.e.
    `pending` + `submitted`) in one query.
    """
    status: Optional[List[str]] = Field(
        None,
        description="Filter by status (OR within the list).",
    )
    model_id: Optional[str] = Field(
        None,
        description="Limit to one model — useful for spotting a "
                    "specific provider's failure pattern.",
    )
    actor_id: Optional[str] = Field(
        None,
        description="Limit to one user (useful for support cases).",
    )
    hours_ago: Optional[int] = Field(
        None,
        ge=1,
        le=24 * 90,  # 90 days
        description="Convenience filter for 'last N hours'. Mutually "
                    "exclusive with start_time / end_time; this one wins.",
    )
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)
    sort_by: Literal[
        "created_at",
        "updated_at",
        "completed_at",
        "cost_usd",
        "duration_seconds",
    ] = "created_at"
    sort_order: Literal["asc", "desc"] = "desc"


class VideoJobListResponse(BaseModel):
    jobs: List[VideoJobSummary]
    pagination: VideoJobPaginationInfo


class VideoJobDetailResponse(BaseModel):
    job: VideoJobDetail


class VideoJobCancelResponse(BaseModel):
    job_id: str
    new_status: VideoJobStatus
    message: str


class VideoJobDeleteResponse(BaseModel):
    job_id: str
    deleted: bool


class VideoStatusCount(BaseModel):
    status: VideoJobStatus
    count: int


class VideoModelStats(BaseModel):
    """Aggregate cost / count per model. Helps an admin spot a
    runaway model burning the budget."""
    model_id: Optional[str] = None
    model_name: Optional[str] = None
    job_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    total_cost_usd: float = 0.0


class VideoStatisticsResponse(BaseModel):
    total_jobs: int
    by_status: List[VideoStatusCount]
    total_cost_usd: float = 0.0
    succeeded_count: int = 0
    failed_count: int = 0
    cancelled_count: int = 0
    in_flight_count: int = Field(
        0,
        description="`pending` + `submitted` rows. The poller is "
                    "still working on these.",
    )
    avg_duration_seconds: Optional[float] = None
    avg_completion_seconds: Optional[float] = Field(
        None,
        description=(
            "Average wall-clock from `created_at` to `completed_at` "
            "across succeeded rows. Useful to spot when an upstream "
            "is degrading."
        ),
    )
    by_model: List[VideoModelStats] = []

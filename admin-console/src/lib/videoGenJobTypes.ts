/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Mirrors the Pydantic shapes in
// `app/api/v1/models/admin/video_generation_job.py` on the
// gateway. Keep these in sync when the Python side changes.

export type VideoJobStatus =
    | 'pending'
    | 'submitted'
    | 'succeeded'
    | 'failed'
    | 'cancelled';

export const VIDEO_JOB_STATUSES: VideoJobStatus[] = [
    'pending',
    'submitted',
    'succeeded',
    'failed',
    'cancelled',
];

export const VIDEO_JOB_TERMINAL_STATUSES: VideoJobStatus[] = [
    'succeeded',
    'failed',
    'cancelled',
];

// Shared between BatchJob and VideoGenJob admin views.
export interface VideoJobPaginationInfo {
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
}

export interface VideoJobSummary {
    id: string;
    tenant_id: string;
    model_id?: string;
    model_name?: string;
    actor_id?: string;
    status: VideoJobStatus;
    resolution?: string;
    duration_seconds?: number;
    cost_usd?: number;
    has_output: boolean;
    /** First ~160 chars of the row's `error` column. */
    error_preview?: string;
    created_at: string;
    updated_at: string;
    completed_at?: string;
}

export interface VideoJobDetail extends VideoJobSummary {
    prompt: string;
    provider_request_id?: string;
    output_file_id?: string;
    chat_id?: string;
    error?: string;
}

export interface VideoJobQueryRequest {
    status?: VideoJobStatus[];
    model_id?: string;
    actor_id?: string;
    hours_ago?: number;
    start_time?: string;
    end_time?: string;
    page?: number;
    page_size?: number;
    sort_by?: 'created_at' | 'updated_at' | 'completed_at' | 'cost_usd' | 'duration_seconds';
    sort_order?: 'asc' | 'desc';
}

export interface VideoJobListResponse {
    jobs: VideoJobSummary[];
    pagination: VideoJobPaginationInfo;
}

export interface VideoJobDetailResponse {
    job: VideoJobDetail;
}

export interface VideoJobCancelResponse {
    job_id: string;
    new_status: VideoJobStatus;
    message: string;
}

export interface VideoJobDeleteResponse {
    job_id: string;
    deleted: boolean;
}

export interface VideoStatusCount {
    status: VideoJobStatus;
    count: number;
}

export interface VideoModelStats {
    model_id?: string;
    model_name?: string;
    job_count: number;
    succeeded_count: number;
    failed_count: number;
    total_cost_usd: number;
}

export interface VideoStatisticsResponse {
    total_jobs: number;
    by_status: VideoStatusCount[];
    total_cost_usd: number;
    succeeded_count: number;
    failed_count: number;
    cancelled_count: number;
    in_flight_count: number;
    avg_duration_seconds?: number;
    avg_completion_seconds?: number;
    by_model: VideoModelStats[];
}

// Status → Tailwind class. Same palette as batch jobs so the two
// admin views feel consistent.
export const VIDEO_STATUS_COLORS: Record<VideoJobStatus, string> = {
    pending: 'bg-gray-100 text-gray-700',
    submitted: 'bg-blue-100 text-blue-700',
    succeeded: 'bg-green-100 text-green-700',
    failed: 'bg-red-100 text-red-700',
    cancelled: 'bg-amber-100 text-amber-700',
};

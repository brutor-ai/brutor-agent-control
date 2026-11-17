/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Video generation jobs.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type {
    VideoJobQueryRequest,
    VideoJobListResponse,
    VideoJobDetailResponse,
    VideoJobCancelResponse,
    VideoJobDeleteResponse,
    VideoStatisticsResponse,
} from '../videoGenJobTypes';
import { GatewayApiBase } from './base';

export class VideoJobsApi extends GatewayApiBase {

    // =========================================================================
    // Video Generation Jobs Admin API
    // =========================================================================

    async queryVideoGenJobs(query: VideoJobQueryRequest): Promise<VideoJobListResponse> {
        return this.request<VideoJobListResponse>('/v1/admin/video-generation-jobs/query', {
            method: 'POST',
            body: JSON.stringify(query),
        });
    }

    async getVideoGenJobDetail(jobId: string): Promise<VideoJobDetailResponse> {
        return this.request<VideoJobDetailResponse>(
            `/v1/admin/video-generation-jobs/${encodeURIComponent(jobId)}`,
            { method: 'GET' },
        );
    }

    async cancelVideoGenJob(jobId: string): Promise<VideoJobCancelResponse> {
        return this.request<VideoJobCancelResponse>(
            `/v1/admin/video-generation-jobs/${encodeURIComponent(jobId)}/cancel`,
            { method: 'POST' },
        );
    }

    async deleteVideoGenJob(jobId: string): Promise<VideoJobDeleteResponse> {
        return this.request<VideoJobDeleteResponse>(
            `/v1/admin/video-generation-jobs/${encodeURIComponent(jobId)}`,
            { method: 'DELETE' },
        );
    }

    async getVideoGenStatistics(params?: { hours_ago?: number }): Promise<VideoStatisticsResponse> {
        const queryStr = params && params.hours_ago
            ? `?hours_ago=${params.hours_ago}`
            : '';
        return this.request<VideoStatisticsResponse>(
            `/v1/admin/video-generation-jobs/stats/summary${queryStr}`,
            { method: 'GET' },
        );
    }
}

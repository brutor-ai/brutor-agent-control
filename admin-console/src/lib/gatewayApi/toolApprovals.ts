/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tool approval requests: the queue of agent calls waiting for a person, and
 * the decision.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type {
    ToolApprovalDetail,
    ToolApprovalFilter,
    ToolApprovalListResponse,
    ToolApprovalResolveResponse,
} from '../toolApprovalTypes';
import { GatewayApiBase } from './base';

export class ToolApprovalsApi extends GatewayApiBase {
    /**
     * List approval requests of this tenant.
     * GET /v1/admin/approvals
     */
    async listToolApprovals(params?: {
        status_filter?: ToolApprovalFilter;
        /** Only the holds raised by the call with this trace root. */
        trace_root_id?: string;
        page?: number;
        page_size?: number;
    }): Promise<ToolApprovalListResponse> {
        const search = new URLSearchParams();
        if (params?.status_filter) search.append('status_filter', params.status_filter);
        if (params?.trace_root_id) search.append('trace_root_id', params.trace_root_id);
        if (params?.page) search.append('page', String(params.page));
        if (params?.page_size) search.append('page_size', String(params.page_size));
        const query = search.toString();
        return await this.request<ToolApprovalListResponse>(
            `/v1/admin/approvals${query ? `?${query}` : ''}`,
        );
    }

    /**
     * How many requests a person can still decide (pending, inside their window).
     * GET /v1/admin/approvals/pending-count
     */
    async getToolApprovalsPendingCount(): Promise<number> {
        const response = await this.request<{ pending_count: number }>(
            '/v1/admin/approvals/pending-count',
        );
        return response.pending_count;
    }

    /**
     * One request, with the arguments the agent wants to call the tool with.
     * GET /v1/admin/approvals/{approval_id}
     */
    async getToolApproval(approvalId: string): Promise<ToolApprovalDetail> {
        const response = await this.request<{ approval: ToolApprovalDetail }>(
            `/v1/admin/approvals/${encodeURIComponent(approvalId)}`,
        );
        return response.approval;
    }

    /**
     * Approve: the agent's poll then returns the one-time token and it retries.
     * POST /v1/admin/approvals/{approval_id}/approve
     */
    async approveToolApproval(approvalId: string, note?: string): Promise<ToolApprovalResolveResponse> {
        return await this.request<ToolApprovalResolveResponse>(
            `/v1/admin/approvals/${encodeURIComponent(approvalId)}/approve`,
            { method: 'POST', body: JSON.stringify({ note: note || null }) },
        );
    }

    /**
     * Reject: the agent's call is refused for good.
     * POST /v1/admin/approvals/{approval_id}/reject
     */
    async rejectToolApproval(approvalId: string, note?: string): Promise<ToolApprovalResolveResponse> {
        return await this.request<ToolApprovalResolveResponse>(
            `/v1/admin/approvals/${encodeURIComponent(approvalId)}/reject`,
            { method: 'POST', body: JSON.stringify({ note: note || null }) },
        );
    }
}

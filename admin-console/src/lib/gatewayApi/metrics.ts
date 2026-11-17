/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Proxy metrics, cost, governance posture, agent identities, semantic policies and audit logs.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiResponse } from '../api/controlPlaneTypes';
import type { AgentReleaseListResponse } from '../agentIdentityTypes';
import { GatewayApiBase } from './base';

export class MetricsApi extends GatewayApiBase {

    // =============================================================================
    // Proxy Metrics Operations (Admin Level)
    // =============================================================================

    /**
     * Get comprehensive performance metrics for production monitoring
     */
    async fetchPerformanceMetrics(params: {
        period_hours?: number;
        start?: string;
        end?: string;
        server_id?: string;
        user_type?: string;
        plane?: string;
    }): Promise<{
        performance_metrics: any;
        timestamp: string;
        accessed_by: any;
        query_parameters: any;
    }> {
        try {
            const searchParams = new URLSearchParams();

            if (params.period_hours !== undefined) searchParams.append('period_hours', params.period_hours.toString());
            if (params.start) searchParams.append('start', params.start);
            if (params.end) searchParams.append('end', params.end);
            if (params.server_id) searchParams.append('server_id', params.server_id);
            if (params.user_type) searchParams.append('user_type', params.user_type);
            if (params.plane) searchParams.append('plane', params.plane);

            return await this.request(`/v1/admin/metrics/performance?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_performance_metrics',
                resource: 'performance_metrics',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get real-time metrics for monitoring dashboards
     */
    async fetchRealTimeMetrics(params: {
        last_minutes?: number;
    } = {}): Promise<{
        real_time_metrics: any;
        refresh_recommended_seconds: number;
        accessed_by: any;
    }> {
        try {
            const searchParams = new URLSearchParams();

            if (params.last_minutes !== undefined) searchParams.append('last_minutes', params.last_minutes.toString());

            return await this.request(`/v1/admin/metrics/real-time?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_real_time_metrics',
                resource: 'real_time_metrics',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get detailed health metrics for a specific server
     */
    async fetchServerHealthMetrics(params: {
        server_id: string;
        hours?: number;
    }): Promise<{
        server_health: any;
        timestamp: string;
        accessed_by: any;
    }> {
        try {
            const searchParams = new URLSearchParams();

            if (params.hours !== undefined) searchParams.append('hours', params.hours.toString());

            return await this.request(`/v1/admin/metrics/servers/${encodeURIComponent(params.server_id)}/health?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_server_health_metrics',
                resource: 'server_health_metrics',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get comprehensive dashboard metrics (for Mission Control)
     * GET /v1/admin/metrics/dashboard
     */
    async fetchDashboardMetrics(params: {
        period_hours?: number;
    } = {}): Promise<{
        dashboard: {
            requests_last_hour: number;
            requests_last_day: number;
            active_connections: number;
            error_rate: number;
            avg_response_time_ms: number;
            uptime: number;
            cpu_usage_percent?: number;
            memory_usage_percent?: number;
        };
        timestamp: string;
        tenant_id: string;
        accessed_by: any;
    }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.period_hours !== undefined) {
                searchParams.append('period_hours', params.period_hours.toString());
            }

            const url = searchParams.toString()
                ? `/v1/admin/metrics/dashboard?${searchParams}`
                : '/v1/admin/metrics/dashboard';

            return await this.request(url);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_dashboard_metrics',
                resource: 'dashboard_metrics'
            });
            throw error;
        }
    }

    /**
     * Get usage metrics for billing and quota tracking
     * GET /v1/admin/metrics/usage
     */
    async fetchUsageMetrics(params: {
        start_date?: string;
        end_date?: string;
        group_by?: 'hour' | 'day' | 'week' | 'month';
    } = {}): Promise<{
        usage: {
            total_requests: number;
            successful_requests: number;
            total_data_transfer_mb: number;
            total_data_in_mb: number;
            total_data_out_mb: number;
            by_server: Record<string, any>;
            by_user_type: Record<string, any>;
            by_period: Array<any>;
        };
        period: {
            start: string;
            end: string;
            group_by: string;
        };
        tenant_id: string;
        accessed_by: any;
    }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.start_date) searchParams.append('start_date', params.start_date);
            if (params.end_date) searchParams.append('end_date', params.end_date);
            if (params.group_by) searchParams.append('group_by', params.group_by);

            const url = searchParams.toString()
                ? `/v1/admin/metrics/usage?${searchParams}`
                : '/v1/admin/metrics/usage';

            return await this.request(url);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_usage_metrics',
                resource: 'usage_metrics',
                ...params
            });
            throw error;
        }
    }

    /**
     * Export metrics data
     * GET /v1/admin/metrics/export
     */
    async exportMetricsData(params: {
        format?: 'json' | 'csv';
        period_hours?: number;
        server_id?: string;
        plane?: string;
    }): Promise<any> {
        try {
            const searchParams = new URLSearchParams();
            if (params.format) searchParams.append('format', params.format);
            if (params.period_hours !== undefined) searchParams.append('period_hours', params.period_hours.toString());
            if (params.server_id) searchParams.append('server_id', params.server_id);
            if (params.plane) searchParams.append('plane', params.plane);

            return await this.request(`/v1/admin/metrics/export?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'export_metrics_data',
                resource: 'metrics_export',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get request volume time-series data
     * GET /v1/admin/metrics/timeseries/requests
     */
    async fetchRequestsTimeSeries(params: {
        period_hours?: number;
        start?: string;
        end?: string;
        interval_minutes?: number;
        server_id?: string;
        plane?: string;
    } = {}): Promise<{
        period_hours: number;
        interval_minutes: number;
        server_id?: string;
        tenant_id: string;
        data_points: number;
        data: Array<{
            time: string;
            totalRequests: number;
            successfulRequests: number;
            failedRequests: number;
            successRate: number;
            avgDuration: number;
        }>;
    }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.period_hours !== undefined) searchParams.append('period_hours', params.period_hours.toString());
            if (params.start) searchParams.append('start', params.start);
            if (params.end) searchParams.append('end', params.end);
            if (params.interval_minutes !== undefined) searchParams.append('interval_minutes', params.interval_minutes.toString());
            if (params.server_id) searchParams.append('server_id', params.server_id);
            if (params.plane) searchParams.append('plane', params.plane);

            return await this.request(`/v1/admin/metrics/timeseries/requests?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_requests_timeseries',
                resource: 'timeseries_requests',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get latency time-series data
     * GET /v1/admin/metrics/timeseries/latency
     */
    async fetchLatencyTimeSeries(params: {
        period_hours?: number;
        start?: string;
        end?: string;
        interval_minutes?: number;
        server_id?: string;
        plane?: string;
    } = {}): Promise<any> {
        try {
            const searchParams = new URLSearchParams();
            if (params.period_hours !== undefined) searchParams.append('period_hours', params.period_hours.toString());
            if (params.start) searchParams.append('start', params.start);
            if (params.end) searchParams.append('end', params.end);
            if (params.interval_minutes !== undefined) searchParams.append('interval_minutes', params.interval_minutes.toString());
            if (params.server_id) searchParams.append('server_id', params.server_id);
            if (params.plane) searchParams.append('plane', params.plane);

            return await this.request(`/v1/admin/metrics/timeseries/latency?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_latency_timeseries',
                resource: 'timeseries_latency',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get error rate time-series data
     * GET /v1/admin/metrics/timeseries/error-rate
     */
    async fetchErrorRateTimeSeries(params: {
        period_hours?: number;
        start?: string;
        end?: string;
        interval_minutes?: number;
        server_id?: string;
        plane?: string;
    } = {}): Promise<any> {
        try {
            const searchParams = new URLSearchParams();
            if (params.period_hours !== undefined) searchParams.append('period_hours', params.period_hours.toString());
            if (params.start) searchParams.append('start', params.start);
            if (params.end) searchParams.append('end', params.end);
            if (params.interval_minutes !== undefined) searchParams.append('interval_minutes', params.interval_minutes.toString());
            if (params.server_id) searchParams.append('server_id', params.server_id);
            if (params.plane) searchParams.append('plane', params.plane);

            return await this.request(`/v1/admin/metrics/timeseries/error-rate?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_error_rate_timeseries',
                resource: 'timeseries_error_rate',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get hourly heatmap data
     * GET /v1/admin/metrics/heatmap/hourly
     */
    async fetchHourlyHeatmap(params: {
        days?: number;
        plane?: string;
    } = {}): Promise<any> {
        try {
            const searchParams = new URLSearchParams();
            if (params.days !== undefined) searchParams.append('days', params.days.toString());
            if (params.plane) searchParams.append('plane', params.plane);

            return await this.request(`/v1/admin/metrics/heatmap/hourly?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_hourly_heatmap',
                resource: 'heatmap_hourly',
                ...params
            });
            throw error;
        }
    }

    /**
     * FinOps cost breakdown (by provider/model/team/user + governed/observed split)
     * GET /v1/admin/metrics/cost/breakdown
     */

    /**
     * FOCUS-conformant cost export (FinOps Open Cost & Usage Specification).
     * Returns the raw CSV text — the caller downloads it as a file.
     * GET /v1/admin/metrics/cost/export/focus
     */
    async fetchCostFocusCsv(params: {
        period_hours?: number;
        start?: string;
        end?: string;
        plane?: string;
        group_id?: string;
    } = {}): Promise<string> {
        const sp = new URLSearchParams();
        if (params.period_hours !== undefined) sp.append('period_hours', params.period_hours.toString());
        if (params.start) sp.append('start', params.start);
        if (params.end) sp.append('end', params.end);
        if (params.plane) sp.append('plane', params.plane);
        if (params.group_id) sp.append("group_id", params.group_id);
        return this.request<string>(`/v1/admin/metrics/cost/export/focus?${sp}`, {
            method: 'GET',
            responseType: 'text',
        });
    }

    /**
     * Monthly per-system statement (FinOps + assurance), YYYY-MM.
     * GET /v1/admin/ai-systems/{id}/statement
     */
    async fetchAISystemStatement(aiSystemId: string, month: string): Promise<any> {
        return this.request(`/v1/admin/ai-systems/${aiSystemId}/statement?month=${month}`);
    }

    async fetchCostBreakdown(params: {
        period_hours?: number;
        /** Explicit calendar window (ISO) — overrides period_hours. */
        start?: string;
        end?: string;
        /** Also return the preceding equal-length window for deltas. */
        compare?: boolean;
        plane?: string;
        top_n?: number;
        group_id?: string;
    } = {}): Promise<any> {
        const sp = new URLSearchParams();
        if (params.period_hours !== undefined) sp.append('period_hours', params.period_hours.toString());
        if (params.start) sp.append('start', params.start);
        if (params.end) sp.append('end', params.end);
        if (params.compare) sp.append('compare', 'true');
        if (params.plane) sp.append('plane', params.plane);
        if (params.top_n !== undefined) sp.append('top_n', params.top_n.toString());
        if (params.group_id) sp.append("group_id", params.group_id);
        return await this.request(`/v1/admin/metrics/cost/breakdown?${sp}`);
    }

    /**
     * FinOps cost composition over time (daily; client rolls up the grain)
     * GET /v1/admin/metrics/cost/series
     */
    async fetchCostSeries(params: {
        dimension?: 'provider' | 'model' | 'team';
        period_hours?: number;
        start?: string;
        end?: string;
        plane?: string;
        group_id?: string;
        top_n?: number;
    } = {}): Promise<{
        dimension: string; grain: string; plane: string;
        keys: Array<{ key: string; name: string }>;
        window: { start: string | null; end: string | null };
        data: Array<{ date: string; values: Record<string, { cost: number; requests: number }> }>;
        currency: string;
    }> {
        const sp = new URLSearchParams();
        if (params.dimension) sp.append('dimension', params.dimension);
        if (params.period_hours !== undefined) sp.append('period_hours', params.period_hours.toString());
        if (params.start) sp.append('start', params.start);
        if (params.end) sp.append('end', params.end);
        if (params.plane) sp.append('plane', params.plane);
        if (params.group_id) sp.append("group_id", params.group_id);
        if (params.top_n !== undefined) sp.append('top_n', params.top_n.toString());
        return await this.request(`/v1/admin/metrics/cost/series?${sp}`);
    }

    /**
     * FinOps cost avoided by the reducers — gateway cache (estimated,
     * basis included) + provider prompt cache (measured)
     * GET /v1/admin/metrics/cost/savings
     */
    async fetchCostSavings(params: {
        period_hours?: number;
        start?: string;
        end?: string;
        group_id?: string;
    } = {}): Promise<any> {
        const sp = new URLSearchParams();
        if (params.period_hours !== undefined) sp.append('period_hours', params.period_hours.toString());
        if (params.start) sp.append('start', params.start);
        if (params.end) sp.append('end', params.end);
        if (params.group_id) sp.append("group_id", params.group_id);
        return await this.request(`/v1/admin/metrics/cost/savings?${sp}`);
    }

    /**
     * FinOps monthly LLM economics series (cost / requests / unit cost per 1k)
     * GET /v1/admin/metrics/cost/monthly
     */
    async fetchCostMonthly(params: {
        months?: number;
        plane?: string;
        group_id?: string;
    } = {}): Promise<{
        months: number; plane: string; group_id: string | null; currency: string;
        data: Array<{ month: string; cost_usd: number; tokens: number;
                      requests: number; unit_cost_per_1k_usd: number }>;
    }> {
        const sp = new URLSearchParams();
        if (params.months !== undefined) sp.append('months', params.months.toString());
        if (params.plane) sp.append('plane', params.plane);
        if (params.group_id) sp.append("group_id", params.group_id);
        return await this.request(`/v1/admin/metrics/cost/monthly?${sp}`);
    }

    /**
     * FinOps budget burn-down vs limits + month-end projection
     * GET /v1/admin/metrics/cost/budgets
     */
    async fetchCostBudgets(): Promise<ApiResponse<'/v1/admin/metrics/cost/budgets', 'get'>> {
        return await this.request(`/v1/admin/metrics/cost/budgets`);
    }

    /**
     * Governance posture (guardrail blocks / policy denials / PII + shadow-AI
     * + discovery coverage)
     * GET /v1/admin/metrics/governance/posture
     */
    async fetchGovernancePosture(params: {
        period_hours?: number;
        start?: string;
        end?: string;
        top_n?: number;
        group_id?: string;
    } = {}): Promise<ApiResponse<'/v1/admin/metrics/governance/posture', 'get'>> {
        const sp = new URLSearchParams();
        if (params.period_hours !== undefined) sp.append('period_hours', params.period_hours.toString());
        if (params.start) sp.append('start', params.start);
        if (params.end) sp.append('end', params.end);
        if (params.top_n !== undefined) sp.append('top_n', params.top_n.toString());
        if (params.group_id) sp.append("group_id", params.group_id);
        return await this.request(`/v1/admin/metrics/governance/posture?${sp}`);
    }

    /** Agent Gateway — per-agent runtime-authz activity (top agents, deny/approval rates). */
    async fetchAgentActivity(params: {
        period_hours?: number;
        top_n?: number;
        group_id?: string;
    } = {}): Promise<any> {
        const sp = new URLSearchParams();
        if (params.period_hours !== undefined) sp.append('period_hours', params.period_hours.toString());
        if (params.top_n !== undefined) sp.append('top_n', params.top_n.toString());
        if (params.group_id) sp.append("group_id", params.group_id);
        return await this.request(`/v1/admin/metrics/agents/activity?${sp}`);
    }

    /**
     * AI Asset coverage — governed assets + the discovered-but-ungoverned
     * (shadow-AI) inventory and the "N seen / M governed" scorecard. Shown
     * on the AI Assets page.
     */
    async fetchAssetCoverage(): Promise<import('../assetCoverageTypes').AssetCoverageResponse> {
        return await this.request(`/v1/admin/agent-registry`);
    }

    // ── Agent Identity ──
    async listAgentIdentities(params: { status?: string; q?: string } = {}): Promise<ApiResponse<'/v1/admin/agent-identities', 'get'>> {
        const sp = new URLSearchParams();
        if (params.status) sp.append('status', params.status);
        if (params.q) sp.append('q', params.q);
        return await this.request(`/v1/admin/agent-identities?${sp}`);
    }
    async createAgentIdentity(body: Record<string, unknown>): Promise<ApiResponse<'/v1/admin/agent-identities', 'post'>> {
        return await this.request(`/v1/admin/agent-identities`, { method: 'POST', body: JSON.stringify(body) });
    }
    async getAgentIdentity(id: string): Promise<ApiResponse<'/v1/admin/agent-identities/{agent_id}', 'get'>> {
        return await this.request(`/v1/admin/agent-identities/${id}`);
    }
    async updateAgentIdentity<Sent>(id: string, body: ApiBodyExactly<'/v1/admin/agent-identities/{agent_id}', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/agent-identities/{agent_id}', 'patch'>> {
        return await this.request(`/v1/admin/agent-identities/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
    }
    async revokeAgentIdentity(id: string): Promise<ApiResponse<'/v1/admin/agent-identities/{agent_id}/revoke', 'post'>> {
        return await this.request(`/v1/admin/agent-identities/${id}/revoke`, { method: 'POST' });
    }
    /** Every observed release of the identity's software, most
     *  recently seen first, with trust and derived approval. */
    async listAgentReleases(id: string): Promise<AgentReleaseListResponse> {
        return await this.request(`/v1/admin/agent-identities/${id}/releases`);
    }
    async listAgentGrants(id: string): Promise<ApiResponse<'/v1/admin/agent-identities/{agent_id}/grants', 'get'>> {
        return await this.request(`/v1/admin/agent-identities/${id}/grants`);
    }
    async addAgentGrant(id: string, body: Record<string, unknown>): Promise<any> {
        return await this.request(`/v1/admin/agent-identities/${id}/grants`, { method: 'POST', body: JSON.stringify(body) });
    }
    async deleteAgentGrant(id: string, grantId: string): Promise<void> {
        await this.request(`/v1/admin/agent-identities/${id}/grants/${grantId}`, { method: 'DELETE' });
    }
    async listAgentMemberships(id: string): Promise<ApiResponse<'/v1/admin/agent-identities/{agent_id}/memberships', 'get'>> {
        return await this.request(`/v1/admin/agent-identities/${id}/memberships`);
    }
    async addAgentMembership(id: string, body: Record<string, unknown>): Promise<any> {
        return await this.request(`/v1/admin/agent-identities/${id}/memberships`, { method: 'POST', body: JSON.stringify(body) });
    }
    async removeAgentMembership(id: string, memberId: number): Promise<void> {
        await this.request(`/v1/admin/agent-identities/${id}/memberships/${memberId}`, { method: 'DELETE' });
    }

    // ── Semantic Governance Policy ──
    async listSemanticPolicies(params: { mode?: string; enabled?: boolean; q?: string } = {}): Promise<ApiResponse<'/v1/admin/semantic-policies', 'get'>> {
        const sp = new URLSearchParams();
        if (params.mode) sp.append('mode', params.mode);
        if (params.enabled !== undefined) sp.append('enabled', String(params.enabled));
        if (params.q) sp.append('q', params.q);
        return await this.request(`/v1/admin/semantic-policies?${sp}`);
    }
    async createSemanticPolicy(body: Record<string, unknown>): Promise<ApiResponse<'/v1/admin/semantic-policies', 'post'>> {
        return await this.request(`/v1/admin/semantic-policies`, { method: 'POST', body: JSON.stringify(body) });
    }
    async updateSemanticPolicy<Sent>(id: string, body: ApiBodyExactly<'/v1/admin/semantic-policies/{policy_id}', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/semantic-policies/{policy_id}', 'patch'>> {
        return await this.request(`/v1/admin/semantic-policies/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
    }
    async deleteSemanticPolicy(id: string): Promise<void> {
        await this.request(`/v1/admin/semantic-policies/${id}`, { method: 'DELETE' });
    }

    /**
     * Get servers status snapshot
     * GET /v1/admin/metrics/servers/status
     */
    async fetchServersStatus(): Promise<{
        timestamp: string;
        tenant_id: string;
        window_minutes: number;
        servers: Array<{
            server_id: string;
            status: 'healthy' | 'unhealthy' | 'degraded' | 'idle' | 'unknown';
            request_count: number;
            requests_per_minute: number;
            avg_latency_ms: number;
            success_rate: number;
            last_request: string | null;
        }>;
    }> {
        try {
            return await this.request('/v1/admin/metrics/servers/status');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_servers_status',
                resource: 'servers_status'
            });
            throw error;
        }
    }

    /**
     * Fetch tenant admin audit logs (NOT proxy logs)
     * These are logs of admin actions within the gateway admin UI
     */
    async fetchTenantAuditLogs(params?: {
        limit?: number;
        user_filter?: string;
        action_filter?: string;
        success_only?: boolean;
    }): Promise<any> {
        const queryParams = new URLSearchParams();

        if (params?.limit) queryParams.set('limit', params.limit.toString());
        if (params?.user_filter) queryParams.set('user_filter', params.user_filter);
        if (params?.action_filter) queryParams.set('action_filter', params.action_filter);
        if (params?.success_only !== undefined) queryParams.set('success_only', params.success_only.toString());

        const query = queryParams.toString();
        return await this.request(`/v1/admin/users/audit-logs${query ? '?' + query : ''}`);
    }
}

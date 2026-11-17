/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Proxy logs: queries, traces, facets, saved views, exports and threads.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import {
    AuditLogsExportParams,
    ProxyLogsQueryParams,
    ProxyLogsResponse,
    ProxySystemHealthResponse,
    ProxyLogsCleanupResponse,
    UserActivityResponse,
    RequestThreadResponse,
    ProxyStatsResponse,
    ProxyStatsQueryParams,
    SavedLogView,
} from '../types';
import type { ApiBodyExactly, ApiRequestBody } from '../api/controlPlaneTypes';
import { GatewayApiBase } from './base';

/**
 * The filter keys of the list, forwarded as given: the fields of
 * `ProxyLogsQueryParams`, which are fields of the Control Plane's
 * `LogQueryRequest`. A filter that takes several values has one name here,
 * the list. A key that is not in this list is not sent, so a filter added to
 * the Control Plane is added here and to `ProxyLogsQueryParams`.
 */
const PROXY_LOG_FILTER_KEYS = [
    // Time
    'start_time', 'end_time', 'hours_ago',
    // Proxy type
    'proxy_types', 'proxy_subtypes',
    // Server / IdP
    'server_ids',
    'idp_server_id',
    // Users
    'gateway_user_ids',
    'idp_user_ids',
    'idp_user_usernames',
    'idp_user_email',
    'session_id',
    // Status
    'success',
    // Correlation
    'correlation_id', 'parent_request_id', 'request_id',
    // MCP
    'mcp_method', 'mcp_capability_names',
    // LLM
    'llm_providers',
    'llm_model_requested', 'llm_models_used',
    'min_tokens', 'max_cost_usd',
    'llm_streaming', 'llm_function_calling', 'llm_used_fallback',
    // OAuth
    'oauth_flow_type',
    // Network + routing
    'client_ip',
    'target_urls',
    'http_status_codes',
    'region_routed_to',
    'residency_class',
    // Free-text + compliance
    'q',
    'compliance_tags_any',
    // Performance
    'min_duration_ms', 'max_duration_ms',
] as const satisfies ReadonlyArray<keyof ProxyLogsQueryParams & keyof ApiRequestBody<'/v1/admin/proxy-logs/query', 'post'>>;

/**
 * The body the list and the export share: the sort (by time, newest first,
 * unless one is given) and each filter that is set. An empty list is a
 * filter that is not set: sent, it would match nothing.
 */
function proxyLogsQueryFilters(params: ProxyLogsQueryParams): Record<string, unknown> {
    const body: Record<string, unknown> = {
        sort_by: params.sort_by ?? 'timestamp',
        sort_order: params.sort_order ?? 'desc',
    };
    for (const key of PROXY_LOG_FILTER_KEYS) {
        const value: unknown = params[key];
        if (value === undefined || value === null) continue;
        if (Array.isArray(value) && value.length === 0) continue;
        body[key] = value;
    }
    return body;
}

export class ProxyLogsApi extends GatewayApiBase {

    // ============================================================================
    // Multi-step agent traces
    // ============================================================================

    async listProxyLogTraces(params?: {
        since?: string;
        until?: string;
        root_kind?: string;
        user_id?: string;
        limit?: number;
        offset?: number;
    }) {
        const qs = new URLSearchParams();
        if (params?.since) qs.set('since', params.since);
        if (params?.until) qs.set('until', params.until);
        if (params?.root_kind) qs.set('root_kind', params.root_kind);
        if (params?.user_id) qs.set('user_id', params.user_id);
        if (params?.limit != null) qs.set('limit', String(params.limit));
        if (params?.offset != null) qs.set('offset', String(params.offset));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<{
            tenant_id: string;
            total: number;
            limit: number;
            offset: number;
            traces: Array<{
                root_task_id: string;
                tenant_id: string;
                started_at: string | null;
                ended_at: string | null;
                span_count: number;
                max_depth: number;
                overall_success: boolean;
                root_kind: string | null;
                gateway_user_id: string | null;
                worst_continuity: string | null;
                proxy_types: string[];
            }>;
        }>(`/v1/admin/proxy-logs/traces${suffix}`, { method: 'GET' });
    }

    async getProxyLogTrace(rootTaskId: string) {
        return this.request<{
            root_task_id: string;
            tenant_id: string;
            total_spans: number;
            max_depth: number;
            proxy_types: string[];
            spans: Array<{
                id: string;
                timestamp: string | null;
                proxy_type: string;
                proxy_subtype: string | null;
                request_id: string | null;
                parent_task_id: string | null;
                current_task_id: string | null;
                delegation_depth: number | null;
                trace_continuity: string | null;
                trace_root_kind: string | null;
                endpoint: string | null;
                http_method: string | null;
                http_status_code: number | null;
                duration_ms: number | null;
                success: boolean | null;
                error_message: string | null;
                gateway_user_id: string | null;
                idp_user_email: string | null;
                server_id: string | null;
                mcp_capability_name: string | null;
                llm_model_used: string | null;
                llm_provider: string | null;
            }>;
        }>(`/v1/admin/proxy-logs/traces/${encodeURIComponent(rootTaskId)}`, {
            method: 'GET',
        });
    }

    // =============================================================================
    // MCP Proxy Logs Operations
    // =============================================================================

    /**
     * Query proxy logs with filtering, pagination, and sorting
     *
     * @param params Query parameters for filtering and pagination
     * @returns Promise with logs and pagination info
     */
    async fetchProxyLogs(
        params: ProxyLogsQueryParams,
    ): Promise<ProxyLogsResponse> {
        try {
            const url = '/v1/admin/proxy-logs/query';

            const requestBody = {
                limit: params.limit ?? 100,
                offset: params.offset ?? 0,
                ...proxyLogsQueryFilters(params),
            };

            // Make the API request with POST method
            const response = await this.request<ProxyLogsResponse>(url, {
                method: 'POST',
                body: JSON.stringify(requestBody)
            });

            return response;

        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_proxy_logs',
                resource: 'proxy_logs',
                ...params
            });
            throw error;
        }
    }

    // ------------------------------------------------------------------
    // Faceted filter discovery — Datadog-style.
    //
    // `fetchProxyLogFacets()` returns top-N values + counts per facetable
    // column, scoped to the same filter envelope passed to /search. The
    // counts always match the table view so the FacetMultiSelect
    // dropdown never offers a value that would return zero rows.
    //
    // `suggestProxyLogValues()` is the prefix-typeahead used when the
    // operator types into a single facet input — cheaper than refetching
    // the full /facets response on every keystroke.
    // ------------------------------------------------------------------

    async fetchProxyLogFacets(
        body: Record<string, any>,
    ): Promise<{
        facets: Record<string, Array<{ value: any; count: number }>>;
        /**
         * Per-field id→name map for ID-shaped facets (server_id,
         * gateway_user_id, llm_model_used). The UI keeps the
         * facet `value` as the filter key and renders the looked-up
         * name for the label. Missing field / missing ID → fall
         * back to the raw value.
         */
        display_names?: Record<string, Record<string, string>>;
    }> {
        return await this.request(
            `/v1/admin/proxy-logs/facets`,
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    async suggestProxyLogValues(
        body: { field: string; prefix: string; suggest_limit?: number } & Record<string, any>,
    ): Promise<{ suggestions: Array<{ value: any; count: number }> }> {
        return await this.request(
            `/v1/admin/proxy-logs/suggest`,
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    // Mirrors for admin-audit-logs — same shape, different prefix.
    async searchAdminAuditLogs(
        body: Record<string, any>,
    ): Promise<{
        logs: any[];
        count: number;
        total: number;
        limit: number;
        offset: number;
        has_more: boolean;
    }> {
        return await this.request(
            '/v1/admin/users/audit-logs/search',
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    async fetchAdminAuditFacets(
        body: Record<string, any>,
    ): Promise<{ facets: Record<string, Array<{ value: any; count: number }>> }> {
        return await this.request(
            '/v1/admin/users/audit-logs/facets',
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    async suggestAdminAuditValues(
        body: { field: string; prefix: string; suggest_limit?: number } & Record<string, any>,
    ): Promise<{ suggestions: Array<{ value: any; count: number }> }> {
        return await this.request(
            '/v1/admin/users/audit-logs/suggest',
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    // ------------------------------------------------------------------
    // Saved log views — per-tenant named filter presets shared by the
    // proxy log + admin audit log viewers. `scope` namespaces views per
    // viewer; the same CRUD surface serves both. The dropdown calls
    // `listSavedLogViews(scope)` on mount and re-calls it after every
    // mutation to keep the menu fresh.
    // ------------------------------------------------------------------

    async listSavedLogViews(
        scope: 'proxy_logs' | 'admin_audit_logs',
    ): Promise<{ items: SavedLogView[] }> {
        const qs = new URLSearchParams({ scope });
        return await this.request(`/v1/admin/saved-log-views?${qs.toString()}`);
    }

    async createSavedLogView(
        body: {
            scope: 'proxy_logs' | 'admin_audit_logs';
            name: string;
            description?: string;
            filters: Record<string, any>;
            is_shared?: boolean;
        },
    ): Promise<SavedLogView> {
        return await this.request(`/v1/admin/saved-log-views`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    async updateSavedLogView<Sent>(
        viewId: string,
        body: ApiBodyExactly<'/v1/admin/saved-log-views/{view_id}', 'patch', Sent>,
    ): Promise<SavedLogView> {
        return await this.request(
            `/v1/admin/saved-log-views/${encodeURIComponent(viewId)}`,
            { method: 'PATCH', body: JSON.stringify(body) },
        );
    }

    async deleteSavedLogView(
        viewId: string,
    ): Promise<void> {
        await this.request(
            `/v1/admin/saved-log-views/${encodeURIComponent(viewId)}`,
            { method: 'DELETE' },
        );
    }

    /**
     * Export proxy logs (streamed, CSV or JSONL).
     *
     * The export takes the body of the list (`/query`) with the same filters
     * and sort, so the file holds the rows the list shows; `limit` and
     * `offset` are not sent: the export is the whole matching set. More than
     * 50,000 matching rows are refused (413) with a sentence saying to narrow
     * the filters.
     *
     * @param params The filters and sort of the list
     * @param format Output format
     */
    async exportProxyLogs(
        params: ProxyLogsQueryParams,
        format: 'csv' | 'jsonl' = 'csv',
    ): Promise<{ blob: Blob; filename?: string }> {
        const query = new URLSearchParams({ format });
        return this.streamExport(
            `/v1/admin/proxy-logs/export?${query.toString()}`,
            'export_proxy_logs',
            proxyLogsQueryFilters(params),
        );
    }

    /**
     * Export admin audit logs (streamed, CSV or JSONL).
     *
     * The export route takes the filters of the list
     * (`AuditLogsExportParams`): a list filter is sent once per value.
     */
    async exportAuditLogs(
        params: AuditLogsExportParams = {},
        format: 'csv' | 'jsonl' = 'csv',
    ): Promise<{ blob: Blob; filename?: string }> {
        const q = new URLSearchParams();
        q.append('format', format);
        for (const key of ['admin_ids', 'actions', 'resource_types', 'ip_addresses', 'request_methods'] as const) {
            for (const value of params[key] ?? []) q.append(key, value);
        }
        if (params.success !== undefined) q.append('success', String(params.success));
        if (params.hours_ago !== undefined) q.append('hours_ago', String(params.hours_ago));
        if (params.start_time) q.append('start_time', params.start_time);
        if (params.end_time) q.append('end_time', params.end_time);
        if (params.q) q.append('q', params.q);
        return this.streamExport(
            `/v1/admin/users/audit-logs/export?${q.toString()}`,
            'export_audit_logs'
        );
    }

    /**
     * Get proxy statistics and analytics
     *
     * @param params Query parameters for filtering statistics
     * @returns Promise with aggregated statistics
     */
    async fetchProxyStats(
        params: ProxyStatsQueryParams = {},
    ): Promise<ProxyStatsResponse> {
        try {
            const searchParams = new URLSearchParams();

            // Time filtering
            if (params.hours_ago !== undefined) {
                searchParams.append('hours_ago', params.hours_ago.toString());
            } else {
                searchParams.append('hours_ago', '24'); // Default to last 24 hours
            }

            // Proxy type filtering (comma-separated)
            if (params.proxy_types && params.proxy_types.length > 0) {
                searchParams.append('proxy_types', params.proxy_types.join(','));
            }

            return await this.request<ProxyStatsResponse>(
                `/v1/admin/proxy-logs/statistics?${searchParams.toString()}`
            );

        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_proxy_stats',
                resource: 'proxy_stats',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get all logs in a request thread (by correlation ID)
     *
     * @param correlationId The correlation ID linking related requests
     * @returns Promise with thread information and related logs
     */
    async fetchRequestThread(
        correlationId: string,
    ): Promise<RequestThreadResponse> {
        try {
            return await this.request<RequestThreadResponse>(`/v1/admin/proxy-logs/thread/${correlationId}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_request_thread',
                resource: 'proxy_logs',
                correlation_id: correlationId,
            });
            throw error;
        }
    }

    /**
     * Get activity logs for a specific user
     *
     * @param userId The user ID
     * @param params Optional parameters (hours_ago, proxy_types)
     * @returns Promise with user activity information
     */
    async fetchUserActivity(
        userId: string,
        params?: {
            hours_ago?: number;
            proxy_types?: Array<'mcp' | 'virtual-mcp' | 'llm' | 'skill' | 'oauth' | 'a2a' | 'batch' | 'portal'>;
        },
    ): Promise<UserActivityResponse> {
        try {
            const searchParams = new URLSearchParams();

            if (params?.hours_ago !== undefined) {
                searchParams.append('hours_ago', params.hours_ago.toString());
            } else {
                searchParams.append('hours_ago', '24'); // Default to last 24 hours
            }

            if (params?.proxy_types && params.proxy_types.length > 0) {
                searchParams.append('proxy_types', params.proxy_types.join(','));
            }

            return await this.request<UserActivityResponse>(
                `/v1/admin/proxy-logs/user/${userId}/activity?${searchParams.toString()}`
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_user_activity',
                resource: 'proxy_logs',
                user_id: userId,
                ...params
            });
            throw error;
        }
    }

    /**
     * Cleanup old proxy logs
     *
     * @param daysToKeep Number of days of logs to retain (1-365)
     * @returns Promise with cleanup result
     */
    async cleanupProxyLogs(
        daysToKeep: number,
    ): Promise<ProxyLogsCleanupResponse> {
        try {
            const response = await this.request<ProxyLogsCleanupResponse>('/v1/admin/proxy-logs/cleanup', {
                method: 'POST',
                body: JSON.stringify({ days_to_keep: daysToKeep })
            });

            return response;
        } catch (error) {
            this.errorReporter(error, {
                operation: 'cleanup_proxy_logs',
                resource: 'proxy_logs',
                days_to_keep: daysToKeep,
            });
            throw error;
        }
    }

    /**
     * Get proxy system health and status
     * Public endpoint - no authentication required
     *
     * @returns Promise with system health information
     */
    async fetchProxySystemHealth(): Promise<ProxySystemHealthResponse> {
        try {
            return await this.request<ProxySystemHealthResponse>(
                '/v1/admin/proxy-logs/health'
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_proxy_system_health',
                resource: 'proxy_system'
            });
            throw error;
        }
    }
}

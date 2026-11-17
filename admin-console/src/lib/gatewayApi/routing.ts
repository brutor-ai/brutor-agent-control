/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLM routing groups, tiers and routing tests.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import {
    RouterStatusResponse,
    RouterHealthCheckResponse,
    RouterTestRequest,
    RouterTestResponse,
    RouterTestHistoryResponse,
    DeploymentListQueryParams,
    ModelDeployment,
    RouterStatisticsQueryParams,
    RouterStatistics,
    ModelDeploymentListResponse,
    RoutingGroup,
    RoutingGroupCreate,
    RoutingGroupListResponse,
    RoutingGroupDeleteResponse,
    RoutingGroupTiersResponse,
    RoutingGroupTierAssignment,
} from '../llmTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly, ApiRequestBody } from '../api/controlPlaneTypes';

export class RoutingApi extends GatewayApiBase {

    // ============================================================================
    // Router Management Methods
    // ============================================================================

    /**
     * List all routing groups
     * @param params - Optional filters
     * @returns List of routing groups
     */
    async listRoutingGroups(params?: {
        enabled_only?: boolean;
    }): Promise<RoutingGroupListResponse> {
        const queryParams = new URLSearchParams();
        if (params?.enabled_only) {
            queryParams.append('enabled_only', 'true');
        }

        const url = `/v1/admin/llm-router/routing-groups${queryParams.toString() ? `?${queryParams}` : ''}`;
        return this.request<RoutingGroupListResponse>(url, {
            method: 'GET',
        });
    }

    /**
     * Get routing group by ID
     * @param groupId - Routing group ID
     * @returns Full routing group details
     */
    async getRoutingGroup(groupId: string): Promise<RoutingGroup> {
        return this.request<RoutingGroup>(
            `/v1/admin/llm-router/routing-groups/${groupId}`,
            { method: 'GET' }
        );
    }

    /**
     * Create new routing group
     * @param data - Routing group creation data: the id, the name and the mode, and whichever of the other fields are set (the Control Plane has a default for each)
     * @returns Created routing group
     */
    async createRoutingGroup(data: Partial<RoutingGroupCreate> & Pick<RoutingGroupCreate, 'id' | 'name' | 'mode'>): Promise<RoutingGroup> {
        return this.request<RoutingGroup>(
            '/v1/admin/llm-router/routing-groups',
            {
                method: 'POST',
                body: JSON.stringify(data),
            }
        );
    }

    /**
     * Update routing group
     * @param groupId - Routing group ID
     * @param updates - Fields to update
     * @returns Updated routing group
     */
    async updateRoutingGroup<Sent>(
        groupId: string,
        updates: ApiBodyExactly<'/v1/admin/llm-router/routing-groups/{group_id}', 'patch', Sent>
    ): Promise<RoutingGroup> {
        return this.request<RoutingGroup>(
            `/v1/admin/llm-router/routing-groups/${groupId}`,
            {
                method: 'PATCH',
                body: JSON.stringify(updates),
            }
        );
    }

    /**
     * Delete routing group
     * @param groupId - Routing group ID
     * @returns Delete response with success status
     * @throws Error if group has models assigned
     */
    async deleteRoutingGroup(groupId: string): Promise<RoutingGroupDeleteResponse> {
        return this.request<RoutingGroupDeleteResponse>(
            `/v1/admin/llm-router/routing-groups/${groupId}`,
            { method: 'DELETE' }
        );
    }

    /**
     * Read the complexity tier -> model map for a routing group.
     * An empty list means `complexity-based-routing` is inert for
     * the group: it scores nothing and falls back to ordinary
     * usage-based balancing.
     */
    async getRoutingGroupTiers(groupId: string): Promise<RoutingGroupTiersResponse> {
        return this.request<RoutingGroupTiersResponse>(
            `/v1/admin/llm-router/routing-groups/${groupId}/tiers`
        );
    }

    /**
     * Replace the whole tier map for a routing group (PUT-replace).
     * An empty array clears it. Every model named must already be a
     * member of the group — a tier map may only narrow what the group
     * grants, never reach outside it.
     */
    async replaceRoutingGroupTiers(
        groupId: string,
        tiers: RoutingGroupTierAssignment[]
    ): Promise<RoutingGroupTiersResponse> {
        return this.request<RoutingGroupTiersResponse>(
            `/v1/admin/llm-router/routing-groups/${groupId}/tiers`,
            { method: 'PUT', body: JSON.stringify({ tiers } satisfies ApiRequestBody<'/v1/admin/llm-router/routing-groups/{group_id}/tiers', 'put'>) }
        );
    }

    /**
     * Test routing against a specific routing group. Sends a
     * real chat-completion through the proxy and returns the
     * member model the router actually selected. There is no
     * `model_id` to target a model directly — that would bypass
     * routing entirely; for direct-model testing use the LLM
     * Models page.
     */
    async testRoutingWithGroup(params: {
        routing_group_id: string;
        message?: string;
    }): Promise<RouterTestResponse> {
        return this.request<RouterTestResponse>('/v1/admin/llm-router/test', {
            method: 'POST',
            body: JSON.stringify(params),
        });
    }

    /**
     * List deployments filtered by routing group
     * @param params - Filter parameters
     * @returns Filtered deployment list
     */
    async listDeploymentsByRoutingGroup(params: {
        routing_group_id: string;
        enabled_only?: boolean;
        sort_by?: string;
    }): Promise<ModelDeploymentListResponse> {
        const queryParams = new URLSearchParams();
        queryParams.append('routing_group_id', params.routing_group_id);
        if (params.enabled_only) {
            queryParams.append('enabled_only', 'true');
        }
        if (params.sort_by) {
            queryParams.append('sort_by', params.sort_by);
        }

        return this.request<ModelDeploymentListResponse>(
            `/v1/admin/llm-router/deployments?${queryParams}`,
            { method: 'GET' }
        );
    }

    /**
     * Get router status
     * @returns Router status with comprehensive metrics
     */
    async getRouterStatus(): Promise<RouterStatusResponse> {
        return this.request<RouterStatusResponse>('/v1/admin/llm-router/status', {
            method: 'GET'
        });
    }

    /**
     * Get router health check
     * @returns Quick health status for monitoring
     */
    async getRouterHealth(): Promise<RouterHealthCheckResponse> {
        return this.request<RouterHealthCheckResponse>('/v1/admin/llm-router/health', {
            method: 'GET'
        });
    }

    /**
     * Test routing with a sample message
     * @param params - Test parameters with message and optional model
     * @returns Test results with selected model and routing details
     *
     * @example
     * const result = await client.testRouting({
     *   message: "Test message",
     *   model: undefined  // Let router decide
     * });
     */
    async testRouting(params: RouterTestRequest): Promise<RouterTestResponse> {
        return this.request<RouterTestResponse>('/v1/admin/llm-router/test', {
            method: 'POST',
            body: JSON.stringify(params)
        });
    }

    /**
     * Recent router-test history for the tenant.
     * Filtered by `routing_group_id` when supplied so the Status
     * tab can show only that group's results. Bounded server-side
     * to ~50 entries per tenant; not persisted across control-
     * plane restarts.
     */
    async getRouterTestHistory(params?: {
        routing_group_id?: string;
        limit?: number;
    }): Promise<RouterTestHistoryResponse> {
        const qs = new URLSearchParams();
        if (params?.routing_group_id) qs.set('routing_group_id', params.routing_group_id);
        if (typeof params?.limit === 'number') qs.set('limit', String(params.limit));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<RouterTestHistoryResponse>(
            `/v1/admin/llm-router/test-history${suffix}`,
            { method: 'GET' },
        );
    }

    /**
     * List all model deployments in the router
     * @param params - Optional filters for status, enabled, and sorting
     * @returns List of model deployments
     */
    async listRouterDeployments(params?: DeploymentListQueryParams): Promise<ModelDeploymentListResponse> {
        const searchParams = new URLSearchParams();

        if (params?.filter_status) {
            searchParams.append('filter_status', params.filter_status);
        }
        if (params?.enabled_only !== undefined) {
            searchParams.append('enabled_only', String(params.enabled_only));
        }
        if (params?.sort_by) {
            searchParams.append('sort_by', params.sort_by);
        }

        const queryString = searchParams.toString();
        const url = queryString
            ? `/v1/admin/llm-router/deployments?${queryString}`
            : '/v1/admin/llm-router/deployments';

        return this.request<ModelDeploymentListResponse>(url, {
            method: 'GET'
        });
    }

    /**
     * Get specific model deployment details
     * @param modelId - Model ID
     * @returns Deployment details
     */
    async getRouterDeployment(modelId: string): Promise<ModelDeployment> {
        return this.request<ModelDeployment>(`/v1/admin/llm-router/deployments/${modelId}`, {
            method: 'GET'
        });
    }

    /**
     * Get router statistics
     * @param params - Optional time range and grouping parameters
     * @returns Aggregated router statistics
     */
    async getRouterStatistics(params?: RouterStatisticsQueryParams): Promise<RouterStatistics> {
        const searchParams = new URLSearchParams();

        if (params?.start_time) {
            searchParams.append('start_time', params.start_time);
        }
        if (params?.end_time) {
            searchParams.append('end_time', params.end_time);
        }
        if (params?.group_by_model !== undefined) {
            searchParams.append('group_by_model', String(params.group_by_model));
        }

        const queryString = searchParams.toString();
        const url = queryString
            ? `/v1/admin/llm-router/statistics?${queryString}`
            : '/v1/admin/llm-router/statistics';

        return this.request<RouterStatistics>(url, {
            method: 'GET'
        });
    }
}

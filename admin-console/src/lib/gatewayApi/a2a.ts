/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent-to-agent: agent cards, outbound rules, delegation graphs and tasks.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import { GatewayApiBase } from './base';
import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';

export class A2aApi extends GatewayApiBase {

    // ========================================================================
    // A2A — Agent Cards, Outbound Rules, Delegation
    // Graphs. All endpoints are tenant-scoped by the admin auth
    // token. The server also takes a `tenant_id` query parameter from a
    // system administrator; nothing here sends one.
    // ========================================================================

    // ---- Agent Cards ------------------------------------------------

    async listA2AAgentCards(params?: {
        enabled_only?: boolean;
    }): Promise<import('../a2aTypes').AgentCardListResponse> {
        const sp = new URLSearchParams();
        if (params?.enabled_only !== undefined) sp.append('enabled_only', String(params.enabled_only));
        const qs = sp.toString();
        return this.request(`/v1/admin/agent-cards${qs ? `?${qs}` : ''}`);
    }

    async getA2AAgentCard(
        cardId: string,
    ): Promise<import('../a2aTypes').AgentCardDetailResponse> {
        return this.request(`/v1/admin/agent-cards/${cardId}`);
    }

    async createA2AAgentCard(
        data: import('../a2aTypes').AgentCardCreateRequest,
    ): Promise<import('../a2aTypes').AgentCardDetailResponse> {
        return this.request(`/v1/admin/agent-cards`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    async updateA2AAgentCard<Sent>(
        cardId: string,
        data: ApiBodyExactly<'/v1/admin/agent-cards/{card_id}', 'patch', Sent>,
    ): Promise<import('../a2aTypes').AgentCardDetailResponse> {
        return this.request(`/v1/admin/agent-cards/${cardId}`, {
            method: 'PATCH',
            body: JSON.stringify(data),
        });
    }

    async deleteA2AAgentCard(cardId: string): Promise<void> {
        return this.request(`/v1/admin/agent-cards/${cardId}`, {
            method: 'DELETE',
        });
    }

    async signA2AAgentCard(
        cardId: string,
        data?: import('../a2aTypes').AgentCardSignRequest,
    ): Promise<import('../a2aTypes').AgentCardDetailResponse> {
        return this.request(`/v1/admin/agent-cards/${cardId}/sign`, {
            method: 'POST',
            body: JSON.stringify(data ?? {}),
        });
    }

    async revokeA2AAgentCard(
        cardId: string,
        data: import('../a2aTypes').AgentCardRevokeRequest,
    ): Promise<import('../a2aTypes').AgentCardDetailResponse> {
        return this.request(`/v1/admin/agent-cards/${cardId}/revoke`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    async importA2AAgentCard(
        data: import('../a2aTypes').AgentCardImportRequest,
    ): Promise<import('../a2aTypes').AgentCardDetailResponse> {
        return this.request(`/v1/admin/agent-cards/import`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /** Connect step of the Add-Card wizard — fetches the well-known
     *  agent card from the URL, parses skills, and returns a preview.
     *  Never persists. */
    async discoverA2AAgentCard(
        data: import('../a2aTypes').AgentCardDiscoverRequest,
    ): Promise<import('../a2aTypes').AgentCardDiscoverResponse> {
        return this.request(`/v1/admin/agent-cards/discover`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /** Bulk-import N agent cards from external well-known URLs. Per-row outcomes. */
    async bulkImportA2AAgentCards(
        data: import('../a2aTypes').AgentCardBulkImportRequest,
    ): Promise<import('../a2aTypes').AgentCardBulkImportResponse> {
        return this.request(`/v1/admin/agent-cards/import-bulk`, {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    /** Probe a single agent card and record liveness. */
    async healthCheckA2AAgentCard(
        cardId: string,
        timeoutSecs?: number,
    ): Promise<import('../a2aTypes').AgentCardHealthResult> {
        const sp = new URLSearchParams();
        if (timeoutSecs) sp.append('timeout', String(timeoutSecs));
        const qs = sp.toString();
        return this.request(`/v1/admin/agent-cards/${cardId}/health-check${qs ? `?${qs}` : ''}`, {
            method: 'POST',
        });
    }

    /** Probe every registered agent card in parallel. */
    async bulkHealthCheckA2AAgentCards(params?: {
        enabled_only?: boolean;
        timeout?: number;
    }): Promise<import('../a2aTypes').AgentCardBulkHealthResponse> {
        const sp = new URLSearchParams();
        if (params?.enabled_only) sp.append('enabled_only', 'true');
        if (params?.timeout) sp.append('timeout', String(params.timeout));
        const qs = sp.toString();
        return this.request(`/v1/admin/agent-cards/health-check/bulk${qs ? `?${qs}` : ''}`, {
            method: 'POST',
        });
    }

    /** Fetch the public a2aregistry.org catalog (cached server-side ~5 min). */
    async fetchA2ARegistryCatalog(params?: {
        force_refresh?: boolean;
    }): Promise<import('../a2aTypes').CatalogResponse> {
        const sp = new URLSearchParams();
        if (params?.force_refresh) sp.append('force_refresh', 'true');
        const qs = sp.toString();
        return this.request(`/v1/admin/agent-cards/catalog${qs ? `?${qs}` : ''}`);
    }

    async getA2ATenantPublicKey(): Promise<import('../a2aTypes').TenantA2APublicKeyResponse> {
        return this.request(`/v1/admin/agent-cards/tenant-key`);
    }

    /** Replace the set of resource groups bound to an agent card. PUT semantics. */
    async setA2AAgentCardGroups(
        cardId: string,
        groupIds: string[],
    ): Promise<import('../a2aTypes').AgentCardDetailResponse> {
        return this.request(`/v1/admin/agent-cards/${cardId}/resource-groups`, {
            method: 'PUT',
            body: JSON.stringify({ group_ids: groupIds } satisfies ApiRequestBody<'/v1/admin/agent-cards/{card_id}/resource-groups', 'put'>),
        });
    }

    /** Get effective agent cards (direct + inherited) for a resource group. */
    async getEffectiveAgentCards(
        groupId: string,
    ): Promise<{
        group_id: string;
        group_name: string;
        direct_cards: Array<{
            id: string;
            name: string;
            version: string;
            description?: string | null;
            trust_tier: string;
            is_trusted: boolean;
            enabled: boolean;
            capability_count: number;
            is_inherited: boolean;
            source_group_id?: string | null;
            source_group_name?: string | null;
            group_ids: string[];
        }>;
        direct_count: number;
        inherited_cards: Array<{
            id: string;
            name: string;
            version: string;
            description?: string | null;
            trust_tier: string;
            is_trusted: boolean;
            enabled: boolean;
            capability_count: number;
            is_inherited: boolean;
            source_group_id?: string | null;
            source_group_name?: string | null;
            group_ids: string[];
        }>;
        inherited_count: number;
        effective_cards: Array<{
            id: string;
            name: string;
            version: string;
            description?: string | null;
            trust_tier: string;
            is_trusted: boolean;
            enabled: boolean;
            capability_count: number;
            is_inherited: boolean;
            source_group_id?: string | null;
            source_group_name?: string | null;
            group_ids: string[];
        }>;
        effective_count: number;
    }> {
        return this.request(`/v1/admin/resource-groups/${groupId}/effective-agent-cards`);
    }

    /** Replace the set of agent cards bound directly to a resource group. */
    async setGroupAgentCards(
        groupId: string,
        cardIds: string[],
    ): Promise<{ group_id: string; agent_card_ids: string[] }> {
        return this.request(`/v1/admin/resource-groups/${groupId}/agent-cards`, {
            method: 'PUT',
            body: JSON.stringify({ agent_card_ids: cardIds } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/agent-cards', 'put'>),
        });
    }

    /** Read per-group A2A global limits (with inheritance). */
    async getGroupA2ALimits(groupId: string): Promise<{
        group_id: string;
        group_name: string;
        a2a_global_limits: Record<string, unknown> | null;
        effective_a2a_limits: Record<string, unknown> | null;
        inherited_a2a_limits: Record<string, unknown> | null;
    }> {
        return this.request(`/v1/admin/resource-groups/${groupId}/a2a-global-limits`);
    }

    /**
     * A resource group's A2A governance: what the group states, what applies
     * from the groups above, and the outbound allow-list of every level that
     * states one (`remote_allow_lists`, root first, the group's own last).
     */
    async getGroupA2AGovernance(groupId: string): Promise<ApiResponse<'/v1/admin/resource-groups/{group_id}/a2a-global-governance', 'get'>> {
        return this.request(`/v1/admin/resource-groups/${groupId}/a2a-global-governance`);
    }

    // ---- Delegation Graphs -----------------------------------------

    async listA2ADelegationGraphs(
        params?: import('../a2aTypes').ListDelegationGraphsParams,
    ): Promise<import('../a2aTypes').A2ADelegationGraphListResponse> {
        const sp = new URLSearchParams();
        if (params?.limit !== undefined) sp.append('limit', String(params.limit));
        if (params?.offset !== undefined) sp.append('offset', String(params.offset));
        if (params?.since) sp.append('since', params.since);
        if (params?.until) sp.append('until', params.until);
        if (params?.success !== undefined) sp.append('success', String(params.success));
        const qs = sp.toString();
        return this.request(`/v1/admin/a2a/delegation-graphs${qs ? `?${qs}` : ''}`);
    }

    async getA2ADelegationGraph(
        rootTaskId: string,
    ): Promise<import('../a2aTypes').A2ADelegationGraphDetailResponse> {
        return this.request(
            `/v1/admin/a2a/delegation-graphs/${encodeURIComponent(rootTaskId)}`,
        );
    }

    async getA2ADelegationStats(
        params?: import('../a2aTypes').DelegationStatsParams,
    ): Promise<import('../a2aTypes').A2ADelegationStats> {
        const sp = new URLSearchParams();
        if (params?.since) sp.append('since', params.since);
        if (params?.until) sp.append('until', params.until);
        if (params?.top_n_cards !== undefined) sp.append('top_n_cards', String(params.top_n_cards));
        const qs = sp.toString();
        return this.request(`/v1/admin/a2a/delegation-graphs/stats${qs ? `?${qs}` : ''}`);
    }

    // V1.0 task browser (read-only)
    async listA2ATasks(
        params?: import('../a2aTypes').ListA2ATasksParams,
    ): Promise<import('../a2aTypes').A2ATaskListResponse> {
        const sp = new URLSearchParams();
        if (params?.state) sp.append('state', params.state);
        if (params?.context_id) sp.append('context_id', params.context_id);
        if (params?.resource_group_id) sp.append('resource_group_id', params.resource_group_id);
        if (params?.since) sp.append('since', params.since);
        if (params?.until) sp.append('until', params.until);
        if (params?.limit !== undefined) sp.append('limit', String(params.limit));
        if (params?.offset !== undefined) sp.append('offset', String(params.offset));
        const qs = sp.toString();
        return this.request(`/v1/admin/a2a-tasks${qs ? `?${qs}` : ''}`);
    }

    async getA2ATask(
        taskId: string,
    ): Promise<import('../a2aTypes').A2ATaskDetailResponse> {
        return this.request(`/v1/admin/a2a-tasks/${encodeURIComponent(taskId)}`);
    }

    async listA2ATaskPushConfigs(
        taskId: string,
    ): Promise<import('../a2aTypes').A2APushConfigListResponse> {
        return this.request(
            `/v1/admin/a2a-tasks/${encodeURIComponent(taskId)}/push-configs`,
        );
    }
}

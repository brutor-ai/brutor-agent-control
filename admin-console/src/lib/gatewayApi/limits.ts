/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Limits and governance: tenant-wide, per resource group and per item.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';
import {
    LLMGlobalLimits,
    LLMGlobalLimitsResponse,
    LLMGovernance,
    LLMModelGovernanceResponse,
    LLMGlobalGovernanceResponse,
    MCPGlobalGovernance,
    MCPGlobalGovernanceResponse,
    SkillGlobalGovernance,
    SkillGlobalGovernanceResponse,
    SkillGovernance,
    SkillGovernanceResponse,
    MCPGlobalLimitsResponse,
    MCPGlobalLimits,
    SkillGlobalLimits,
    SkillGlobalLimitsResponse,
    LLMModelLimitsResponse,
    MCPServerLimitsResponse,
    SkillItemLimitsResponse,
    GroupUsageQueryParams,
    GroupUsageResponse,
} from '../groupTypes';
import { GatewayApiBase } from './base';

/**
 * The limits of one model, MCP server or skill of a group, as a save names
 * them. The contract types them as an open record (the three routes share
 * one body), so what is in it is the form's to answer for.
 */
export type ItemLimits = ApiRequestBody<'/v1/admin/resource-groups/{group_id}/llm-models/{model_id}/limits', 'patch'>['limits'];

/**
 * The media limits, and the media governance, of a group as a save names
 * them: one record keyed by mode. The contract types each as an open record,
 * so what is in it is the form's to answer for.
 */
export type MediaGlobalLimits = ApiRequestBody<'/v1/admin/resource-groups/{group_id}/media-global-limits', 'patch'>['media_global_limits'];
export type MediaGlobalGovernance = ApiRequestBody<'/v1/admin/resource-groups/{group_id}/media-global-governance', 'patch'>['media_global_governance'];
export type MediaGlobalLimitsResponse = ApiResponse<'/v1/admin/resource-groups/{group_id}/media-global-limits', 'get'>;
export type MediaGlobalGovernanceResponse = ApiResponse<'/v1/admin/resource-groups/{group_id}/media-global-governance', 'get'>;

export class LimitsApi extends GatewayApiBase {

    // ============================================
    // Limits Endpoints
    // ============================================

    /**
     * Get LLM global limits for a specific group
     *
     * @param groupId - The group ID
     * @returns LLMGlobalLimitsResponse with group's LLM global limits
     */
    async fetchLLMGlobalLimits(groupId: string): Promise<LLMGlobalLimitsResponse> {
        return this.request<LLMGlobalLimitsResponse>(
            `/v1/admin/resource-groups/${groupId}/llm-global-limits`
        );
    }

    /**
     * Update LLM global limits for a specific group
     *
     * This completely replaces any existing LLM global limits for the group.
     *
     * @param groupId - The group ID
     * @param limits - The LLM global limits to set
     * @returns LLMGlobalLimitsResponse with updated limits
     */
    async updateLLMGlobalLimits(
        groupId: string,
        limits: LLMGlobalLimits
    ): Promise<LLMGlobalLimitsResponse> {
        return this.request<LLMGlobalLimitsResponse>(`/v1/admin/resource-groups/${groupId}/llm-global-limits`, {
            method: 'PATCH',
            body: JSON.stringify({ llm_global_limits: { ...limits } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/llm-global-limits', 'patch'>)
        });
    }

    /**
     * Get MCP global limits for a specific group
     *
     * @param groupId - The group ID
     * @returns MCPGlobalLimitsResponse with group's MCP global limits
     */
    async fetchMCPGlobalLimits(groupId: string): Promise<MCPGlobalLimitsResponse> {
        return this.request<MCPGlobalLimitsResponse>(
            `/v1/admin/resource-groups/${groupId}/mcp-global-limits`
        );
    }

    /**
     * Update MCP global limits for a specific group
     *
     * This completely replaces any existing MCP global limits for the group.
     *
     * @param groupId - The group ID
     * @param limits - The MCP global limits to set
     * @returns MCPGlobalLimitsResponse with updated limits
     */
    async updateMCPGlobalLimits(
        groupId: string,
        limits: MCPGlobalLimits
    ): Promise<MCPGlobalLimitsResponse> {
        return this.request<MCPGlobalLimitsResponse>(`/v1/admin/resource-groups/${groupId}/mcp-global-limits`, {
            method: 'PATCH',
            body: JSON.stringify({ mcp_global_limits: { ...limits } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/mcp-global-limits', 'patch'>)
        });
    }

    /**
     * Get Skill global limits for a specific group
     */
    async fetchSkillGlobalLimits(groupId: string): Promise<SkillGlobalLimitsResponse> {
        return this.request<SkillGlobalLimitsResponse>(
            `/v1/admin/resource-groups/${groupId}/skill-global-limits`
        );
    }

    /**
     * Update Skill global limits for a specific group
     */
    async updateSkillGlobalLimits(
        groupId: string,
        limits: SkillGlobalLimits
    ): Promise<SkillGlobalLimitsResponse> {
        return this.request<SkillGlobalLimitsResponse>(`/v1/admin/resource-groups/${groupId}/skill-global-limits`, {
            method: 'PATCH',
            body: JSON.stringify({ skill_global_limits: { ...limits } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/skill-global-limits', 'patch'>)
        });
    }

    // ============================================
    // Media (multimodal) global limits + governance
    // ============================================

    //
    // Single JSONB column on the server, keyed by mode
    // (audio_speech / audio_transcription / image_generation /
    // video_generation / embedding). The PATCH replaces the
    // entire column — partial saves at the *mode* level need
    // the caller to merge client-side first. The Media tabs do
    // exactly that.

    async fetchMediaGlobalLimits(groupId: string): Promise<MediaGlobalLimitsResponse> {
        return this.request<MediaGlobalLimitsResponse>(`/v1/admin/resource-groups/${groupId}/media-global-limits`);
    }

    async updateMediaGlobalLimits(groupId: string, limits: MediaGlobalLimits): Promise<MediaGlobalLimitsResponse> {
        return this.request<MediaGlobalLimitsResponse>(`/v1/admin/resource-groups/${groupId}/media-global-limits`, {
            method: 'PATCH',
            body: JSON.stringify({ media_global_limits: limits } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/media-global-limits', 'patch'>),
        });
    }

    async fetchMediaGlobalGovernance(groupId: string): Promise<MediaGlobalGovernanceResponse> {
        return this.request<MediaGlobalGovernanceResponse>(`/v1/admin/resource-groups/${groupId}/media-global-governance`);
    }

    async updateMediaGlobalGovernance(groupId: string, governance: MediaGlobalGovernance): Promise<MediaGlobalGovernanceResponse> {
        return this.request<MediaGlobalGovernanceResponse>(`/v1/admin/resource-groups/${groupId}/media-global-governance`, {
            method: 'PATCH',
            body: JSON.stringify({ media_global_governance: governance } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/media-global-governance', 'patch'>),
        });
    }

    // ============================================
    // Per-Item Limits Operations
    // ============================================

    /**
     * Get limits for a specific LLM model in a group
     */
    async fetchLLMModelLimits(groupId: string, modelId: string): Promise<LLMModelLimitsResponse> {
        return this.request<LLMModelLimitsResponse>(
            `/v1/admin/resource-groups/${groupId}/llm-models/${modelId}/limits`
        );
    }

    /**
     * Update limits for a specific LLM model in a group
     */
    async updateLLMModelLimits(
        groupId: string,
        modelId: string,
        limits: ItemLimits
    ): Promise<LLMModelLimitsResponse> {
        return this.request<LLMModelLimitsResponse>(`/v1/admin/resource-groups/${groupId}/llm-models/${modelId}/limits`, {
            method: 'PATCH',
            body: JSON.stringify({ limits } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/llm-models/{model_id}/limits', 'patch'>)
        });
    }

    /**
     * Clear limits for a specific LLM model in a group
     */
    async clearLLMModelLimits(groupId: string, modelId: string): Promise<void> {
        return this.request<void>(`/v1/admin/resource-groups/${groupId}/llm-models/${modelId}/limits`, {
            method: 'DELETE'
        });
    }

    /**
     * Get limits for a specific MCP server in a group
     */
    async fetchMCPServerLimits(groupId: string, serverId: string): Promise<MCPServerLimitsResponse> {
        return this.request<MCPServerLimitsResponse>(
            `/v1/admin/resource-groups/${groupId}/mcp-servers/${serverId}/limits`
        );
    }

    /**
     * Update limits for a specific MCP server in a group
     */
    async updateMCPServerLimits(
        groupId: string,
        serverId: string,
        limits: ItemLimits
    ): Promise<MCPServerLimitsResponse> {
        return this.request<MCPServerLimitsResponse>(`/v1/admin/resource-groups/${groupId}/mcp-servers/${serverId}/limits`, {
            method: 'PATCH',
            body: JSON.stringify({ limits } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/mcp-servers/{server_id}/limits', 'patch'>)
        });
    }

    /**
     * Clear limits for a specific MCP server in a group
     */
    async clearMCPServerLimits(groupId: string, serverId: string): Promise<void> {
        return this.request<void>(`/v1/admin/resource-groups/${groupId}/mcp-servers/${serverId}/limits`, {
            method: 'DELETE'
        });
    }

    /**
     * Get limits for a specific skill in a group
     */
    async fetchSkillItemLimits(groupId: string, skillId: string): Promise<SkillItemLimitsResponse> {
        return this.request<SkillItemLimitsResponse>(
            `/v1/admin/resource-groups/${groupId}/agent-skills/${skillId}/limits`
        );
    }

    /**
     * Update limits for a specific skill in a group
     */
    async updateSkillItemLimits(
        groupId: string,
        skillId: string,
        limits: ItemLimits
    ): Promise<SkillItemLimitsResponse> {
        return this.request<SkillItemLimitsResponse>(`/v1/admin/resource-groups/${groupId}/agent-skills/${skillId}/limits`, {
            method: 'PATCH',
            body: JSON.stringify({ limits } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/agent-skills/{skill_id}/limits', 'patch'>)
        });
    }

    /**
     * Clear limits for a specific skill in a group
     */
    async clearSkillItemLimits(groupId: string, skillId: string): Promise<void> {
        return this.request<void>(`/v1/admin/resource-groups/${groupId}/agent-skills/${skillId}/limits`, {
            method: 'DELETE'
        });
    }

    /**
     * Get LLM governance for a specific model in a group
     */
    async getLLMModelGovernance(groupId: string, modelId: string): Promise<LLMModelGovernanceResponse> {
        return this.request<LLMModelGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/llm-models/${modelId}/governance`
        );
    }

    /**
     * Update LLM governance for a specific model in a group
     */
    async updateLLMModelGovernance(
        groupId: string,
        modelId: string,
        governance: LLMGovernance
    ): Promise<LLMModelGovernanceResponse> {
        return this.request<LLMModelGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/llm-models/${modelId}/governance`, {
            method: 'PATCH',
            body: JSON.stringify({ governance: { ...governance } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/llm-models/{model_id}/governance', 'patch'>)
        });
    }

    /**
     * Clear LLM governance for a specific model in a group
     */
    async clearLLMModelGovernance(groupId: string, modelId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/llm-models/${modelId}/governance`, {
            method: 'DELETE',
        });
    }

    // ========================================================================
    // LLM Global Governance
    // ========================================================================

    /**
     * Get LLM global governance for a group
     */
    async getLLMGlobalGovernance(groupId: string): Promise<LLMGlobalGovernanceResponse> {
        return this.request<LLMGlobalGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/llm-global-governance`
        );
    }

    /**
     * Update LLM global governance for a group
     */
    async updateLLMGlobalGovernance(
        groupId: string,
        governance: LLMGovernance
    ): Promise<LLMGlobalGovernanceResponse> {
        return this.request<LLMGlobalGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/llm-global-governance`, {
            method: 'PATCH',
            body: JSON.stringify({ governance: { ...governance } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/llm-global-governance', 'patch'>)
        });
    }

    /**
     * Clear LLM global governance for a group
     */
    async clearLLMGlobalGovernance(groupId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/llm-global-governance`, {
            method: 'DELETE',
        });
    }

    // ========================================================================
    // MCP Global Governance
    // ========================================================================

    /**
     * Get MCP global governance for a group
     */
    async getMCPGlobalGovernance(groupId: string): Promise<MCPGlobalGovernanceResponse> {
        return this.request<MCPGlobalGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/mcp-global-governance`
        );
    }

    /**
     * Update MCP global governance for a group
     */
    async updateMCPGlobalGovernance(
        groupId: string,
        governance: MCPGlobalGovernance
    ): Promise<MCPGlobalGovernanceResponse> {
        return this.request<MCPGlobalGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/mcp-global-governance`, {
            method: 'PATCH',
            body: JSON.stringify({ governance: { ...governance } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/mcp-global-governance', 'patch'>)
        });
    }

    /**
     * Clear MCP global governance for a group
     */
    async clearMCPGlobalGovernance(groupId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/mcp-global-governance`, {
            method: 'DELETE',
        });
    }

    // ========================================================================
    // Approval window
    // ========================================================================

    /** How long a hold raised for this group stays open when the policy
     *  that raised it names no window — stated, inherited, tenant, platform. */
    async getApprovalWindow(
        groupId: string,
    ): Promise<ApiResponse<'/v1/admin/resource-groups/{group_id}/approval-window', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/resource-groups/{group_id}/approval-window', 'get'>>(
            `/v1/admin/resource-groups/${groupId}/approval-window`,
        );
    }

    /** Set (or clear with null) the group's default approval window. The
     *  server refuses a window longer than the inherited one (400). */
    async updateApprovalWindow<Sent>(
        groupId: string,
        body: ApiBodyExactly<'/v1/admin/resource-groups/{group_id}/approval-window', 'put', Sent>,
    ): Promise<ApiResponse<'/v1/admin/resource-groups/{group_id}/approval-window', 'put'>> {
        return this.request<ApiResponse<'/v1/admin/resource-groups/{group_id}/approval-window', 'put'>>(
            `/v1/admin/resource-groups/${groupId}/approval-window`,
            { method: 'PUT', body: JSON.stringify(body) },
        );
    }

    // ========================================================================
    // Skill Global Governance
    // ========================================================================

    /**
     * Get skill global governance for a group
     */
    async getSkillGlobalGovernance(groupId: string): Promise<SkillGlobalGovernanceResponse> {
        return this.request<SkillGlobalGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/skill-global-governance`
        );
    }

    /**
     * Update skill global governance for a group
     */
    async updateSkillGlobalGovernance(
        groupId: string,
        governance: SkillGlobalGovernance
    ): Promise<SkillGlobalGovernanceResponse> {
        return this.request<SkillGlobalGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/skill-global-governance`, {
            method: 'PATCH',
            body: JSON.stringify({ governance: { ...governance } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/skill-global-governance', 'patch'>)
        });
    }

    /**
     * Clear skill global governance for a group
     */
    async clearSkillGlobalGovernance(groupId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/skill-global-governance`, {
            method: 'DELETE',
        });
    }

    // ========================================================================
    // Per-Skill Governance
    // ========================================================================

    /**
     * Get governance for a specific skill in a group
     */
    async getSkillGovernance(groupId: string, skillId: string): Promise<SkillGovernanceResponse> {
        return this.request<SkillGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/agent-skills/${skillId}/governance`
        );
    }

    /**
     * Update governance for a specific skill in a group
     */
    async updateSkillGovernance(
        groupId: string,
        skillId: string,
        governance: SkillGovernance
    ): Promise<SkillGovernanceResponse> {
        return this.request<SkillGovernanceResponse>(
            `/v1/admin/resource-groups/${groupId}/agent-skills/${skillId}/governance`, {
            method: 'PATCH',
            body: JSON.stringify({ governance: { ...governance } } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/agent-skills/{skill_id}/governance', 'patch'>)
        });
    }

    /**
     * Clear governance for a specific skill in a group
     */
    async clearSkillGovernance(groupId: string, skillId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/agent-skills/${skillId}/governance`, {
            method: 'DELETE',
        });
    }

    /**
     * Fetch usage analytics for a resource group
     * @param groupId - The group ID
     * @param params - Optional query parameters (period, include_hierarchy, resource_type)
     * @returns Usage analytics including LLM/MCP metrics and daily breakdown
     */
    async fetchGroupUsage(
        groupId: string,
        params?: GroupUsageQueryParams
    ): Promise<GroupUsageResponse> {
        const searchParams = new URLSearchParams();

        if (params?.period) {
            searchParams.append('period', params.period);
        }
        if (params?.include_hierarchy !== undefined) {
            searchParams.append('include_hierarchy', String(params.include_hierarchy));
        }
        if (params?.resource_type) {
            searchParams.append('resource_type', params.resource_type);
        }
        if (params?.user_id !== undefined) {
            searchParams.append('user_id', String(params.user_id));
        }

        const queryString = searchParams.toString();
        const endpoint = `/v1/admin/resource-groups/${groupId}/usage${queryString ? `?${queryString}` : ''}`;

        return this.request<GroupUsageResponse>(endpoint);
    }
}

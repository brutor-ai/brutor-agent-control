/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What a resource group is bound to: models, MCP servers, capability filters, skills and API keys.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import {
    GroupAPIKey,
    EffectiveLLModelsResponse,
    EffectiveServerConfigsResponse,
    EffectiveAgentSkillsResponse,
    GroupAPIKeyListResponse,
    GroupAPIKeyUsageResponse,
    CreateGroupAPIKeyRequest,
    MCPCapabilityFilterResponse,
    MCPCapabilityInfo,
} from '../groupTypes';
import { GatewayApiBase } from './base';
import type { ApiRequestBody } from '../api/controlPlaneTypes';

export class GroupResourcesApi extends GatewayApiBase {

    // ============================================
    // LLM Models Endpoints
    // ============================================

    /**
     * Fetch effective LLM models (direct + inherited)
     */
    async fetchEffectiveLLMModels(
        groupId: string
    ): Promise<EffectiveLLModelsResponse> {
        return this.request<EffectiveLLModelsResponse>(
            `/v1/admin/resource-groups/${groupId}/effective-llm-models`
        );
    }

    /**
     * Add LLM model to group
     */
    async addModelToGroup(groupId: string, modelId: string): Promise<void> {
        await this.request<void>(`/v1/admin/resource-groups/${groupId}/llm-models`, {
            method: 'POST',
            body: JSON.stringify({ llm_model_id: modelId }),
        });
    }

    /**
     * Remove LLM model from group
     */
    async removeModelFromGroup(groupId: string, modelId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/llm-models/${modelId}`,
            {
                method: 'DELETE',
            }
        );
    }

    // ============================================
    // MCP Server Endpoints
    // ============================================

    /**
     * Fetch effective server configs (direct + inherited + global)
     */
    async fetchEffectiveServerConfigs(
        groupId: string
    ): Promise<EffectiveServerConfigsResponse> {
        return this.request<EffectiveServerConfigsResponse>(
            `/v1/admin/resource-groups/${groupId}/effective-server-configs`
        );
    }

    /**
     * Add server config to group
     */
    async addServerToGroup(groupId: string, serverId: string): Promise<void> {
        await this.request<void>(`/v1/admin/resource-groups/${groupId}/server-configs`, {
            method: 'POST',
            body: JSON.stringify({ server_config_id: serverId }),
        });
    }

    /**
     * Remove server config from group
     */
    async removeServerFromGroup(groupId: string, serverId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/server-configs/${serverId}`,
            {
                method: 'DELETE',
            }
        );
    }

    // ============================================
    // MCP Capability Filters (Group) Endpoints
    // ============================================

    /**
     * Get capability filters for a group+server
     */
    async getMCPCapabilityFilters(groupId: string, configId: string): Promise<MCPCapabilityFilterResponse> {
        return this.request<MCPCapabilityFilterResponse>(
            `/v1/admin/resource-groups/${groupId}/server-configs/${configId}/capabilities`
        );
    }

    /**
     * Update capability filters for a group+server (full replace)
     */
    async updateMCPCapabilityFilters(
        groupId: string,
        configId: string,
        capabilities: MCPCapabilityInfo[]
    ): Promise<MCPCapabilityFilterResponse> {
        return this.request<MCPCapabilityFilterResponse>(
            `/v1/admin/resource-groups/${groupId}/server-configs/${configId}/capabilities`,
            {
                method: 'PUT',
                body: JSON.stringify({ capabilities } satisfies ApiRequestBody<'/v1/admin/resource-groups/{group_id}/server-configs/{config_id}/capabilities', 'put'>),
            }
        );
    }

    /**
     * Clear all capability filters for a group+server (restore full access)
     */
    async clearMCPCapabilityFilters(groupId: string, configId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/resource-groups/${groupId}/server-configs/${configId}/capabilities`,
            {
                method: 'DELETE',
            }
        );
    }

    // ============================================
    // Agent Skills (Group) Endpoints
    // ============================================

    /**
     * Fetch effective agent skills (direct + inherited)
     */
    async fetchEffectiveAgentSkills(
        groupId: string
    ): Promise<EffectiveAgentSkillsResponse> {
        return this.request<EffectiveAgentSkillsResponse>(
            `/v1/admin/resource-groups/${groupId}/effective-agent-skills`
        );
    }

    /**
     * Remove agent skill from group (via skill's group assignment)
     */
    async removeSkillFromGroup(groupId: string, skillId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/agent-skills/${skillId}/groups`,
            {
                method: 'DELETE',
                body: JSON.stringify({ group_ids: [groupId] }),
            }
        );
    }

    // ============================================
    // API Keys Endpoints
    // ============================================

    /**
     * Fetch group-scoped API keys
     */
    async fetchGroupAPIKeys(
        groupId: string
    ): Promise<GroupAPIKeyListResponse> {
        return this.request<GroupAPIKeyListResponse>(
            `/v1/admin/resource-groups/${groupId}/api-keys`
        );
    }

    /**
     * Fetch usage statistics for group keys
     */
    async fetchGroupAPIKeyUsage(
        groupId: string
    ): Promise<GroupAPIKeyUsageResponse> {
        return this.request<GroupAPIKeyUsageResponse>(
            `/v1/admin/resource-groups/${groupId}/api-keys/usage`
        );
    }

    /**
     * Create a new group-scoped API key
     */
    async createGroupAPIKey(
        groupId: string,
        data: CreateGroupAPIKeyRequest
    ): Promise<GroupAPIKey & { key_value: string }> {
        return this.request<GroupAPIKey & { key_value: string }>(
            `/v1/admin/resource-groups/${groupId}/api-keys`,
            {
                method: 'POST',
                body: JSON.stringify(data),
            }
        );
    }

    /**
     * Enable an API key
     */
    async enableAPIKey(keyId: string): Promise<void> {
        await this.request<void>(`/v1/admin/resource-groups/api-keys/${keyId}/enable`, {
            method: 'POST',
        });
    }

    /**
     * Disable an API key
     */
    async disableAPIKey(keyId: string): Promise<void> {
        await this.request<void>(`/v1/admin/resource-groups/api-keys/${keyId}/disable`, {
            method: 'POST',
        });
    }

    /**
     * Revoke an API key (permanent)
     */
    async revokeAPIKey(keyId: string): Promise<void> {
        await this.request<void>(`/v1/admin/resource-groups/api-keys/${keyId}/revoke`, {
            method: 'POST',
        });
    }
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What a resource group is bound to: models, MCP servers, capability filters, skills and API keys.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import {
    GroupAPIKeyListResponse,
    GroupAPIKeyUsageResponse,
    CreateGroupAPIKeyRequest,
    GroupAPIKey,
    EffectiveServerConfigsResponse,
    EffectiveAgentSkillsResponse,
    EffectiveLLModelsResponse,
    MCPCapabilityFilterResponse,
    MCPCapabilityInfo,
} from '../../lib/groupTypes';
import type { GatewayCore } from './core';


export function useGroupResourcesGateway(ctx: Pick<GatewayCore, 'gatewayClient'>) {
    const { gatewayClient } = ctx;

    // ============================================
    // LLM Models Operations
    // ============================================

    const fetchEffectiveLLMModels = useCallback(async (
        groupId: string
    ): Promise<EffectiveLLModelsResponse> => {
        return gatewayClient.fetchEffectiveLLMModels(groupId);
    }, []);

    const addModelToGroup = useCallback(async (
        groupId: string,
        modelId: string
    ): Promise<void> => {
        return gatewayClient.addModelToGroup(groupId, modelId);
    }, []);

    const removeModelFromGroup = useCallback(async (
        groupId: string,
        modelId: string
    ): Promise<void> => {
        return gatewayClient.removeModelFromGroup(groupId, modelId);
    }, []);

    // ============================================
    // MCP Server Operations
    // ============================================

    const fetchEffectiveServerConfigs = useCallback(async (
        groupId: string
    ): Promise<EffectiveServerConfigsResponse> => {
        return gatewayClient.fetchEffectiveServerConfigs(groupId);
    }, []);

    const addServerToGroup = useCallback(async (
        groupId: string,
        serverId: string
    ): Promise<void> => {
        return gatewayClient.addServerToGroup(groupId, serverId);
    }, []);

    const removeServerFromGroup = useCallback(async (
        groupId: string,
        serverId: string
    ): Promise<void> => {
        return gatewayClient.removeServerFromGroup(groupId, serverId);
    }, []);

    // ============================================
    // MCP Capability Filters (Group) Operations
    // ============================================

    const getMCPCapabilityFilters = useCallback(async (
        groupId: string,
        configId: string
    ): Promise<MCPCapabilityFilterResponse> => {
        return gatewayClient.getMCPCapabilityFilters(groupId, configId);
    }, []);

    const updateMCPCapabilityFilters = useCallback(async (
        groupId: string,
        configId: string,
        capabilities: MCPCapabilityInfo[]
    ): Promise<MCPCapabilityFilterResponse> => {
        return gatewayClient.updateMCPCapabilityFilters(groupId, configId, capabilities);
    }, []);

    const clearMCPCapabilityFilters = useCallback(async (
        groupId: string,
        configId: string
    ): Promise<void> => {
        return gatewayClient.clearMCPCapabilityFilters(groupId, configId);
    }, []);

    // ============================================
    // Agent Skills (Group) Operations
    // ============================================

    const fetchEffectiveAgentSkills = useCallback(async (
        groupId: string
    ): Promise<EffectiveAgentSkillsResponse> => {
        return gatewayClient.fetchEffectiveAgentSkills(groupId);
    }, []);

    const removeSkillFromGroup = useCallback(async (
        groupId: string,
        skillId: string
    ): Promise<void> => {
        return gatewayClient.removeSkillFromGroup(groupId, skillId);
    }, []);

    // ============================================
    // API Keys Operations
    // ============================================

    const fetchGroupAPIKeys = useCallback(async (
        groupId: string
    ): Promise<GroupAPIKeyListResponse> => {
        return gatewayClient.fetchGroupAPIKeys(groupId);
    }, []);

    const fetchGroupAPIKeyUsage = useCallback(async (
        groupId: string
    ): Promise<GroupAPIKeyUsageResponse> => {
        return gatewayClient.fetchGroupAPIKeyUsage(groupId);
    }, []);

    const createGroupAPIKey = useCallback(async (
        groupId: string,
        data: CreateGroupAPIKeyRequest
    ): Promise<GroupAPIKey & { key_value: string }> => {
        return gatewayClient.createGroupAPIKey(groupId, data);
    }, []);

    const enableAPIKey = useCallback(async (keyId: string): Promise<void> => {
        return gatewayClient.enableAPIKey(keyId);
    }, []);

    const disableAPIKey = useCallback(async (keyId: string): Promise<void> => {
        return gatewayClient.disableAPIKey(keyId);
    }, []);

    const revokeAPIKey = useCallback(async (keyId: string): Promise<void> => {
        return gatewayClient.revokeAPIKey(keyId);
    }, []);

    return {
        addModelToGroup,
        addServerToGroup,
        clearMCPCapabilityFilters,
        createGroupAPIKey,
        disableAPIKey,
        enableAPIKey,
        fetchEffectiveAgentSkills,
        fetchEffectiveLLMModels,
        fetchEffectiveServerConfigs,
        fetchGroupAPIKeyUsage,
        fetchGroupAPIKeys,
        getMCPCapabilityFilters,
        removeModelFromGroup,
        removeServerFromGroup,
        removeSkillFromGroup,
        revokeAPIKey,
        updateMCPCapabilityFilters,
    };
}

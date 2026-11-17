/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Limits, governance and usage alerts.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import {
    LLMGlobalLimits,
    MCPGlobalLimits,
    SkillGlobalLimits,
    LLMGlobalLimitsResponse,
    MCPGlobalLimitsResponse,
    SkillGlobalLimitsResponse,
    LLMModelLimitsResponse,
    MCPServerLimitsResponse,
    SkillItemLimitsResponse,
    LLMGovernance,
    LLMModelGovernanceResponse,
    LLMGlobalGovernanceResponse,
    MCPGlobalGovernance,
    MCPGlobalGovernanceResponse,
    SkillGlobalGovernance,
    SkillGlobalGovernanceResponse,
    SkillGovernance,
    SkillGovernanceResponse,
    GroupUsageQueryParams,
    GroupUsageResponse,
    TenantAlertsResponse,
    GroupAlertResponse,
    AlertAcknowledgeResponse,
    UsageAlertQueryParams,
} from '../../lib/groupTypes';
import type { GatewayCore } from './core';
import type { ItemLimits, MediaGlobalGovernance, MediaGlobalLimits } from '../../lib/gatewayApi/limits';


export function useLimitsGateway(ctx: Pick<GatewayCore, 'gatewayClient'>) {
    const { gatewayClient } = ctx;

    // ============================================
    // Limits Operations
    // ============================================

    const fetchLLMGlobalLimits = useCallback(async (
        groupId: string
    ): Promise<LLMGlobalLimitsResponse> => {
        return gatewayClient.fetchLLMGlobalLimits(groupId);
    }, []);

    const fetchMCPGlobalLimits = useCallback(async (
        groupId: string
    ): Promise<MCPGlobalLimitsResponse> => {
        return gatewayClient.fetchMCPGlobalLimits(groupId);
    }, []);

    const updateLLMGlobalLimits = useCallback(async (
        groupId: string,
        limits: LLMGlobalLimits
    ): Promise<LLMGlobalLimitsResponse> => {
        return gatewayClient.updateLLMGlobalLimits(groupId, limits);
    }, []);

    const updateMCPGlobalLimits = useCallback(async (
        groupId: string,
        limits: MCPGlobalLimits
    ): Promise<MCPGlobalLimitsResponse> => {
        return gatewayClient.updateMCPGlobalLimits(groupId, limits);
    }, []);

    const fetchSkillGlobalLimits = useCallback(async (
        groupId: string
    ): Promise<SkillGlobalLimitsResponse> => {
        return gatewayClient.fetchSkillGlobalLimits(groupId);
    }, []);

    const updateSkillGlobalLimits = useCallback(async (
        groupId: string,
        limits: SkillGlobalLimits
    ): Promise<SkillGlobalLimitsResponse> => {
        return gatewayClient.updateSkillGlobalLimits(groupId, limits);
    }, []);

    // Media (multimodal) limits + governance hooks. Single
    // JSONB column per category, keyed by mode. Used by the
    // GroupMediaLimitsTab and GroupMediaGovernanceTab.
    const fetchMediaGlobalLimits = useCallback(async (groupId: string) => {
        return gatewayClient.fetchMediaGlobalLimits(groupId);
    }, []);

    const updateMediaGlobalLimits = useCallback(async (
        groupId: string,
        limits: MediaGlobalLimits,
    ) => {
        return gatewayClient.updateMediaGlobalLimits(groupId, limits);
    }, []);

    const fetchMediaGlobalGovernance = useCallback(async (groupId: string) => {
        return gatewayClient.fetchMediaGlobalGovernance(groupId);
    }, []);

    const updateMediaGlobalGovernance = useCallback(async (
        groupId: string,
        governance: MediaGlobalGovernance,
    ) => {
        return gatewayClient.updateMediaGlobalGovernance(groupId, governance);
    }, []);

    // Per-item limits hooks

    const fetchLLMModelLimits = useCallback(async (
        groupId: string,
        modelId: string
    ): Promise<LLMModelLimitsResponse> => {
        return gatewayClient.fetchLLMModelLimits(groupId, modelId);
    }, []);

    const updateLLMModelLimits = useCallback(async (
        groupId: string,
        modelId: string,
        limits: ItemLimits
    ): Promise<LLMModelLimitsResponse> => {
        return gatewayClient.updateLLMModelLimits(groupId, modelId, limits);
    }, []);

    const clearLLMModelLimits = useCallback(async (
        groupId: string,
        modelId: string
    ): Promise<void> => {
        return gatewayClient.clearLLMModelLimits(groupId, modelId);
    }, []);

    const fetchMCPServerLimits = useCallback(async (
        groupId: string,
        serverId: string
    ): Promise<MCPServerLimitsResponse> => {
        return gatewayClient.fetchMCPServerLimits(groupId, serverId);
    }, []);

    const updateMCPServerLimits = useCallback(async (
        groupId: string,
        serverId: string,
        limits: ItemLimits
    ): Promise<MCPServerLimitsResponse> => {
        return gatewayClient.updateMCPServerLimits(groupId, serverId, limits);
    }, []);

    const clearMCPServerLimits = useCallback(async (
        groupId: string,
        serverId: string
    ): Promise<void> => {
        return gatewayClient.clearMCPServerLimits(groupId, serverId);
    }, []);

    const fetchSkillItemLimits = useCallback(async (
        groupId: string,
        skillId: string
    ): Promise<SkillItemLimitsResponse> => {
        return gatewayClient.fetchSkillItemLimits(groupId, skillId);
    }, []);

    const updateSkillItemLimits = useCallback(async (
        groupId: string,
        skillId: string,
        limits: ItemLimits
    ): Promise<SkillItemLimitsResponse> => {
        return gatewayClient.updateSkillItemLimits(groupId, skillId, limits);
    }, []);

    const clearSkillItemLimits = useCallback(async (
        groupId: string,
        skillId: string
    ): Promise<void> => {
        return gatewayClient.clearSkillItemLimits(groupId, skillId);
    }, []);

    const getLLMModelGovernance = useCallback(async (
        groupId: string,
        modelId: string
    ): Promise<LLMModelGovernanceResponse> => {
        return gatewayClient.getLLMModelGovernance(groupId, modelId);
    }, []);

    const updateLLMModelGovernance = useCallback(async (
        groupId: string,
        modelId: string,
        governance: LLMGovernance
    ): Promise<LLMModelGovernanceResponse> => {
        return gatewayClient.updateLLMModelGovernance(groupId, modelId, governance);
    }, []);

    const clearLLMModelGovernance = useCallback(async (
        groupId: string,
        modelId: string
    ): Promise<void> => {
        return gatewayClient.clearLLMModelGovernance(groupId, modelId);
    }, []);

    // LLM Global Governance
    const getLLMGlobalGovernance = useCallback(async (
        groupId: string
    ): Promise<LLMGlobalGovernanceResponse> => {
        return gatewayClient.getLLMGlobalGovernance(groupId);
    }, []);

    const updateLLMGlobalGovernance = useCallback(async (
        groupId: string,
        governance: LLMGovernance
    ): Promise<LLMGlobalGovernanceResponse> => {
        return gatewayClient.updateLLMGlobalGovernance(groupId, governance);
    }, []);

    const clearLLMGlobalGovernance = useCallback(async (
        groupId: string
    ): Promise<void> => {
        return gatewayClient.clearLLMGlobalGovernance(groupId);
    }, []);

    // MCP Global Governance
    const getMCPGlobalGovernance = useCallback(async (
        groupId: string
    ): Promise<MCPGlobalGovernanceResponse> => {
        return gatewayClient.getMCPGlobalGovernance(groupId);
    }, []);

    const updateMCPGlobalGovernance = useCallback(async (
        groupId: string,
        governance: MCPGlobalGovernance
    ): Promise<MCPGlobalGovernanceResponse> => {
        return gatewayClient.updateMCPGlobalGovernance(groupId, governance);
    }, []);

    const clearMCPGlobalGovernance = useCallback(async (
        groupId: string
    ): Promise<void> => {
        return gatewayClient.clearMCPGlobalGovernance(groupId);
    }, []);

    // Skill Global Governance
    const getSkillGlobalGovernance = useCallback(async (
        groupId: string
    ): Promise<SkillGlobalGovernanceResponse> => {
        return gatewayClient.getSkillGlobalGovernance(groupId);
    }, []);

    const updateSkillGlobalGovernance = useCallback(async (
        groupId: string,
        governance: SkillGlobalGovernance
    ): Promise<SkillGlobalGovernanceResponse> => {
        return gatewayClient.updateSkillGlobalGovernance(groupId, governance);
    }, []);

    const clearSkillGlobalGovernance = useCallback(async (
        groupId: string
    ): Promise<void> => {
        return gatewayClient.clearSkillGlobalGovernance(groupId);
    }, []);

    // Per-Skill Governance
    const getSkillGovernance = useCallback(async (
        groupId: string,
        skillId: string
    ): Promise<SkillGovernanceResponse> => {
        return gatewayClient.getSkillGovernance(groupId, skillId);
    }, []);

    const updateSkillGovernance = useCallback(async (
        groupId: string,
        skillId: string,
        governance: SkillGovernance
    ): Promise<SkillGovernanceResponse> => {
        return gatewayClient.updateSkillGovernance(groupId, skillId, governance);
    }, []);

    const clearSkillGovernance = useCallback(async (
        groupId: string,
        skillId: string
    ): Promise<void> => {
        return gatewayClient.clearSkillGovernance(groupId, skillId);
    }, []);

    const fetchGroupUsage = useCallback(async (
        groupId: string,
        params?: GroupUsageQueryParams
    ): Promise<GroupUsageResponse> => {
        return gatewayClient.fetchGroupUsage(groupId, params);
    }, []);

    const fetchAllUsageAlerts = useCallback(async (
        params?: UsageAlertQueryParams
    ): Promise<TenantAlertsResponse> => {
        return gatewayClient.fetchAllUsageAlerts(params);
    }, []);

    const fetchGroupAlerts = useCallback(async (
        groupId: string,
        params?: UsageAlertQueryParams
    ): Promise<GroupAlertResponse> => {
        return gatewayClient.fetchGroupAlerts(groupId, params);
    }, []);

    const acknowledgeUsageAlert = useCallback(async (
        alertId: string
    ): Promise<AlertAcknowledgeResponse> => {
        return gatewayClient.acknowledgeUsageAlert(alertId);
    }, []);

    return {
        acknowledgeUsageAlert,
        clearLLMGlobalGovernance,
        clearLLMModelGovernance,
        clearLLMModelLimits,
        clearMCPGlobalGovernance,
        clearMCPServerLimits,
        clearSkillGlobalGovernance,
        clearSkillGovernance,
        clearSkillItemLimits,
        fetchAllUsageAlerts,
        fetchGroupAlerts,
        fetchGroupUsage,
        fetchLLMGlobalLimits,
        fetchLLMModelLimits,
        fetchMCPGlobalLimits,
        fetchMCPServerLimits,
        fetchMediaGlobalGovernance,
        fetchMediaGlobalLimits,
        fetchSkillGlobalLimits,
        fetchSkillItemLimits,
        getLLMGlobalGovernance,
        getLLMModelGovernance,
        getMCPGlobalGovernance,
        getSkillGlobalGovernance,
        getSkillGovernance,
        updateLLMGlobalGovernance,
        updateLLMGlobalLimits,
        updateLLMModelGovernance,
        updateLLMModelLimits,
        updateMCPGlobalGovernance,
        updateMCPGlobalLimits,
        updateMCPServerLimits,
        updateMediaGlobalGovernance,
        updateMediaGlobalLimits,
        updateSkillGlobalGovernance,
        updateSkillGlobalLimits,
        updateSkillGovernance,
        updateSkillItemLimits,
    };
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Agent skill files: resources, scripts and assets.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import {
    AgentSkillResource,
    AgentSkillResourceCreateRequest,
    AgentSkillResourceListResponse,
    AgentSkillScript,
    AgentSkillScriptCreateRequest,
    AgentSkillScriptListResponse,
    AgentSkillAsset,
    AgentSkillAssetCreateRequest,
    AgentSkillAssetListResponse,
} from '../../lib/agentSkillTypes';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';


export function useAgentSkillsGateway(ctx: Pick<GatewayCore, 'gatewayClient'>) {
    const { gatewayClient } = ctx;

    // =============================================================================
    // Agent Skill Resources & Scripts
    // =============================================================================

    const listSkillResources = useCallback(async (skillId: string): Promise<AgentSkillResourceListResponse> => {
        return gatewayClient.listSkillResources(skillId);
    }, [gatewayClient]);

    const getSkillResource = useCallback(async (skillId: string, resourceId: string): Promise<AgentSkillResource> => {
        return gatewayClient.getSkillResource(skillId, resourceId);
    }, [gatewayClient]);

    const createSkillResource = useCallback(async (skillId: string, data: AgentSkillResourceCreateRequest): Promise<AgentSkillResource> => {
        return gatewayClient.createSkillResource(skillId, data);
    }, [gatewayClient]);

    const updateSkillResource = useCallback(async <Sent>(skillId: string, resourceId: string, data: ApiBodyExactly<'/v1/admin/agent-skills/{skill_id}/resources/{resource_id}', 'patch', Sent>): Promise<AgentSkillResource> => {
        return gatewayClient.updateSkillResource(skillId, resourceId, data);
    }, [gatewayClient]);

    const deleteSkillResource = useCallback(async (skillId: string, resourceId: string) => {
        return gatewayClient.deleteSkillResource(skillId, resourceId);
    }, [gatewayClient]);

    const listSkillScripts = useCallback(async (skillId: string): Promise<AgentSkillScriptListResponse> => {
        return gatewayClient.listSkillScripts(skillId);
    }, [gatewayClient]);

    const getSkillScript = useCallback(async (skillId: string, scriptId: string): Promise<AgentSkillScript> => {
        return gatewayClient.getSkillScript(skillId, scriptId);
    }, [gatewayClient]);

    const createSkillScript = useCallback(async (skillId: string, data: AgentSkillScriptCreateRequest): Promise<AgentSkillScript> => {
        return gatewayClient.createSkillScript(skillId, data);
    }, [gatewayClient]);

    const updateSkillScript = useCallback(async <Sent>(skillId: string, scriptId: string, data: ApiBodyExactly<'/v1/admin/agent-skills/{skill_id}/scripts/{script_id}', 'patch', Sent>): Promise<AgentSkillScript> => {
        return gatewayClient.updateSkillScript(skillId, scriptId, data);
    }, [gatewayClient]);

    const deleteSkillScript = useCallback(async (skillId: string, scriptId: string) => {
        return gatewayClient.deleteSkillScript(skillId, scriptId);
    }, [gatewayClient]);

    // =============================================================================
    // Agent Skill Assets
    // =============================================================================

    const listSkillAssets = useCallback(async (skillId: string): Promise<AgentSkillAssetListResponse> => {
        return gatewayClient.listSkillAssets(skillId);
    }, [gatewayClient]);

    const getSkillAsset = useCallback(async (skillId: string, assetId: string): Promise<AgentSkillAsset> => {
        return gatewayClient.getSkillAsset(skillId, assetId);
    }, [gatewayClient]);

    const createSkillAsset = useCallback(async (skillId: string, data: AgentSkillAssetCreateRequest): Promise<AgentSkillAsset> => {
        return gatewayClient.createSkillAsset(skillId, data);
    }, [gatewayClient]);

    const updateSkillAsset = useCallback(async <Sent>(skillId: string, assetId: string, data: ApiBodyExactly<'/v1/admin/agent-skills/{skill_id}/assets/{asset_id}', 'patch', Sent>): Promise<AgentSkillAsset> => {
        return gatewayClient.updateSkillAsset(skillId, assetId, data);
    }, [gatewayClient]);

    const deleteSkillAsset = useCallback(async (skillId: string, assetId: string) => {
        return gatewayClient.deleteSkillAsset(skillId, assetId);
    }, [gatewayClient]);

    return {
        createSkillAsset,
        createSkillResource,
        createSkillScript,
        deleteSkillAsset,
        deleteSkillResource,
        deleteSkillScript,
        getSkillAsset,
        getSkillResource,
        getSkillScript,
        listSkillAssets,
        listSkillResources,
        listSkillScripts,
        updateSkillAsset,
        updateSkillResource,
        updateSkillScript,
    };
}

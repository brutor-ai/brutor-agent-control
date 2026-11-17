/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Guardrail configurations and tests.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import { EffectiveGuardrailsResponse } from '../../lib/groupTypes';
import {
    GuardrailConfigDetailResponse,
    GuardrailConfigListResponse,
    GuardrailConfigCreateRequest,
    GuardrailTestRequest,
    GuardrailTestResponse,
    GuardrailProviderListResponse,
} from '../../lib/guardrailTypes';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';


export function useGuardrailsGateway(ctx: Pick<GatewayCore, 'gatewayClient'>) {
    const { gatewayClient } = ctx;

    // ============================================
    // Guardrails Operations
    // ============================================

    const fetchEffectiveGuardrails = useCallback(async (
        groupId: string
    ): Promise<EffectiveGuardrailsResponse> => {
        return gatewayClient.fetchEffectiveGuardrails(groupId);
    }, [gatewayClient]);

    const fetchGuardrailConfig = useCallback(async (): Promise<GuardrailConfigDetailResponse> => {
        return gatewayClient.getGuardrailConfig();
    }, [gatewayClient]);

    const updateGuardrailConfig = useCallback(async <Sent>(
        data: ApiBodyExactly<'/v1/admin/guardrails/config', 'patch', Sent>
    ): Promise<GuardrailConfigDetailResponse> => {
        return gatewayClient.updateGuardrailConfig(data);
    }, [gatewayClient]);

    const testGuardrails = useCallback(async (
        data: GuardrailTestRequest
    ): Promise<GuardrailTestResponse> => {
        return gatewayClient.testGuardrails(data);
    }, [gatewayClient]);

    const fetchGuardrailProviders = useCallback(async (): Promise<GuardrailProviderListResponse> => {
        return gatewayClient.listGuardrailProviders();
    }, [gatewayClient]);

    const listGuardrailConfigs = useCallback(async (): Promise<GuardrailConfigListResponse> => {
        return gatewayClient.listGuardrailConfigs();
    }, [gatewayClient]);

    const createGuardrailConfig = useCallback(async (
        data: GuardrailConfigCreateRequest
    ): Promise<GuardrailConfigDetailResponse> => {
        return gatewayClient.createGuardrailConfig(data);
    }, [gatewayClient]);

    const getGuardrailConfigById = useCallback(async (
        configId: string
    ): Promise<GuardrailConfigDetailResponse> => {
        return gatewayClient.getGuardrailConfigById(configId);
    }, [gatewayClient]);

    const updateGuardrailConfigById = useCallback(async <Sent>(
        configId: string,
        data: ApiBodyExactly<'/v1/admin/guardrails/configs/{config_id}', 'patch', Sent>
    ): Promise<GuardrailConfigDetailResponse> => {
        return gatewayClient.updateGuardrailConfigById(configId, data);
    }, [gatewayClient]);

    const deleteGuardrailConfig = useCallback(async (
        configId: string
    ): Promise<{ message: string; id: string }> => {
        return gatewayClient.deleteGuardrailConfig(configId);
    }, [gatewayClient]);

    const setGuardrailGroups = useCallback(async <Sent>(
        configId: string,
        data: ApiBodyExactly<'/v1/admin/guardrails/configs/{config_id}/groups', 'patch', Sent>
    ): Promise<GuardrailConfigDetailResponse> => {
        return gatewayClient.setGuardrailGroups(configId, data);
    }, [gatewayClient]);

    return {
        createGuardrailConfig,
        deleteGuardrailConfig,
        fetchEffectiveGuardrails,
        fetchGuardrailConfig,
        fetchGuardrailProviders,
        getGuardrailConfigById,
        listGuardrailConfigs,
        setGuardrailGroups,
        testGuardrails,
        updateGuardrailConfig,
        updateGuardrailConfigById,
    };
}

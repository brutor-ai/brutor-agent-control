/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useCallback } from 'react';
import { useGateway } from './useGateway';
import { useHealthCheckBase, type HealthCheckTrigger } from './useHealthCheckBase';

/**
 * AI Model Health Check Hook
 *
 * Manages periodic health checks for the AI Model Tab.
 *
 * - Fetches ai_settings from dedicated health check config API
 * - Checks AI Models ONLY
 * - Independent from other tabs
 * - Only runs when AI Model Tab is active (mount/unmount)
 * - Can be paused when forms/modals are open
 *
 * @param options - Configuration options
 * @param options.paused - If true, health checks are paused (for form editing)
 * @returns {Object} AI Model health check state and controls
 */
export const useAIModelHealthCheck = (options?: { paused?: boolean }) => {
    const { gatewayClient } = useGateway();

    // Define health check function using useCallback to maintain stable reference
    // Background (timer-driven) sweeps request the free catalog probe —
    // the provider's /models listing, no generation, no tokens. Only an
    // operator's explicit click pays for the real end-to-end endpoint
    // probe. The server falls back per model when a provider has no
    // catalog URL, and reports which probe actually ran (`probe_type`).
    const healthCheckFn = useCallback(async (trigger: HealthCheckTrigger) => {
        if (!gatewayClient) {
            console.warn('[AI Models] Gateway client not available');
            return;
        }
        await gatewayClient.bulkHealthCheckLLMs({
            probe: trigger === 'background' ? 'catalog' : 'endpoint',
        });
    }, [gatewayClient]);

    // Use base hook with AI-specific configuration
    return useHealthCheckBase({
        tabName: 'AI Models',
        configKey: 'ai_settings',
        healthCheckFn,
        paused: options?.paused,
        defaultInterval: 300  // 5 minutes
    });
};

export default useAIModelHealthCheck;
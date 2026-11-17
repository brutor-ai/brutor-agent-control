/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Gateway statistics and health checks.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import { GatewayStats } from '../../lib/types';
import type { GatewayCore } from './core';
import { isHttpError } from './core';
import { errorKind } from '../../lib/narrow';


export function useHealthGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    const fetchStats = useCallback(async (): Promise<GatewayStats> => {
        try {
            const statsData = await gatewayClient.fetchStats();
            return statsData;
        } catch (error) {
            console.error('Failed to fetch stats:', errorKind(error));
            if (isHttpError(error) && (error.status === 401 || error.status === 403)) {
                console.error('Authentication required to view statistics.');
                throw error;
            }
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch statistics',
                code: 'FETCH_STATS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_stats', resource: 'stats' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    // =============================================================================
    // Health Check Operations
    // =============================================================================

    const triggerServerHealthCheck = useCallback(async (serverId: string) => {
        try {
            const result = await gatewayClient.checkServerHealth(serverId);
            return result;
        } catch (error) {
            console.error(`Health check failed for server ${serverId}:`, errorKind(error));
            addError({
                message: error instanceof Error ? error.message : `Health check failed for server ${serverId}`,
                code: 'TRIGGER_SERVER_HEALTH_CHECK_ERROR',
                severity: 'medium',
                context: { operation: 'trigger_server_health_check', resource: 'health_check', serverId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const triggerAllHealthChecks = useCallback(async () => {
        try {
            const result = await gatewayClient.checkAllMCPServerHealth();
            return result;
        } catch (error) {
            console.error('Bulk health check failed:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Bulk health check failed',
                code: 'TRIGGER_ALL_HEALTH_CHECKS_ERROR',
                severity: 'medium',
                context: { operation: 'trigger_all_health_checks', resource: 'health_checks' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        fetchStats,
        triggerAllHealthChecks,
        triggerServerHealthCheck,
    };
}

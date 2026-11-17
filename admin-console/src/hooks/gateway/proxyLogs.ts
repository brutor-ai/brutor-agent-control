/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Proxy logs and their exports.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import type { AuditLogsExportParams, ProxyLogsQueryParams } from '../../lib/types';
import { errorKind } from '../../lib/narrow';


export function useProxyLogsGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    // =============================================================================
    // Proxy Logs Operations
    // =============================================================================

    const fetchProxyLogs = useCallback(async (params: ProxyLogsQueryParams) => {
        try {
            return await gatewayClient.fetchProxyLogs(params);
        } catch (error) {
            console.error('Failed to fetch proxy logs:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch proxy logs',
                code: 'FETCH_PROXY_LOGS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_proxy_logs', resource: 'proxy_logs', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchProxyStats = useCallback(async (params: {
        user_type?: string;
        username?: string;
        server_id?: string;
        date_from?: string;
        date_to?: string;
    }) => {
        try {
            return await gatewayClient.fetchProxyStats(params);
        } catch (error) {
            console.error('Failed to fetch proxy stats:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch proxy statistics',
                code: 'FETCH_PROXY_STATS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_proxy_stats', resource: 'proxy_stats', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const exportProxyLogs = useCallback(async (
        params: ProxyLogsQueryParams,
        format: 'csv' | 'jsonl' = 'csv',
    ) => {
        try {
            return await gatewayClient.exportProxyLogs(params, format);
        } catch (error) {
            console.error('Failed to export proxy logs:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to export proxy logs',
                code: 'EXPORT_PROXY_LOGS_ERROR',
                severity: 'medium',
                context: { operation: 'export_proxy_logs', resource: 'proxy_logs', format }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const exportAuditLogs = useCallback(async (
        params: AuditLogsExportParams = {},
        format: 'csv' | 'jsonl' = 'csv',
    ) => {
        try {
            return await gatewayClient.exportAuditLogs(params, format);
        } catch (error) {
            console.error('Failed to export audit logs:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to export audit logs',
                code: 'EXPORT_AUDIT_LOGS_ERROR',
                severity: 'medium',
                context: { operation: 'export_audit_logs', resource: 'audit_logs', format }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        exportAuditLogs,
        exportProxyLogs,
        fetchProxyLogs,
        fetchProxyStats,
    };
}

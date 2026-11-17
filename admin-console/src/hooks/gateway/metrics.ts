/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Proxy metrics, time series and audit logs.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import { errorKind } from '../../lib/narrow';


export function useMetricsGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    // =============================================================================
    // Enhanced Metrics Operations
    // =============================================================================

    const fetchPerformanceMetrics = useCallback(async (params: {
        period_hours?: number;
        start?: string;
        end?: string;
        server_id?: string;
        user_type?: string;
        plane?: string;
    }) => {
        try {
            return await gatewayClient.fetchPerformanceMetrics(params);
        } catch (error) {
            console.error('Failed to fetch performance metrics:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch performance metrics',
                code: 'FETCH_PERFORMANCE_METRICS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_performance_metrics', resource: 'performance_metrics', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchRealTimeMetrics = useCallback(async (params: { last_minutes?: number } = {}) => {
        try {
            return await gatewayClient.fetchRealTimeMetrics(params);
        } catch (error) {
            console.error('Failed to fetch real-time metrics:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch real-time metrics',
                code: 'FETCH_REAL_TIME_METRICS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_real_time_metrics', resource: 'real_time_metrics', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchServerHealthMetrics = useCallback(async (params: { server_id: string; hours?: number }) => {
        try {
            return await gatewayClient.fetchServerHealthMetrics(params);
        } catch (error) {
            console.error('Failed to fetch server health metrics:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch server health metrics',
                code: 'FETCH_SERVER_HEALTH_METRICS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_server_health_metrics', resource: 'server_health_metrics', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchDashboardMetrics = useCallback(async () => {
        try {
            return await gatewayClient.fetchDashboardMetrics();
        } catch (error) {
            console.error('Failed to fetch dashboard metrics:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch dashboard metrics',
                code: 'FETCH_DASHBOARD_METRICS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_dashboard_metrics', resource: 'dashboard_metrics' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const exportMetricsData = useCallback(async (params: {
        format: 'json' | 'csv';
        period_hours?: number;
        server_id?: string;
        plane?: string;
    }) => {
        try {
            return await gatewayClient.exportMetricsData(params);
        } catch (error) {
            console.error('Failed to export metrics data:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to export metrics data',
                code: 'EXPORT_METRICS_DATA_ERROR',
                severity: 'medium',
                context: { operation: 'export_metrics_data', resource: 'metrics_export', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchRequestsTimeSeries = useCallback(async (params?: {
        period_hours?: number;
        interval_minutes?: number;
        server_id?: string;
    }) => {
        return await gatewayClient.fetchRequestsTimeSeries(params);
    }, [gatewayClient]);

    const fetchLatencyTimeSeries = useCallback(async (params?: {
        period_hours?: number;
        interval_minutes?: number;
        server_id?: string;
    }) => {
        return await gatewayClient.fetchLatencyTimeSeries(params);
    }, [gatewayClient]);

    const fetchErrorRateTimeSeries = useCallback(async (params?: {
        period_hours?: number;
        start?: string;
        end?: string;
        interval_minutes?: number;
        server_id?: string;
        plane?: string;
    }) => {
        return await gatewayClient.fetchErrorRateTimeSeries(params);
    }, [gatewayClient]);

    const fetchHourlyHeatmap = useCallback(async (params?: {
        days?: number;
        server_id?: string;
    }) => {
        return await gatewayClient.fetchHourlyHeatmap(params);
    }, [gatewayClient]);

    /**
     * Fetch tenant admin audit logs
     * These are logs of admin actions in the gateway (login, config changes, etc.)
     */
    const fetchTenantAuditLogs = useCallback(async (params?: {
        limit?: number;
        user_filter?: string;
        action_filter?: string;
        success_only?: boolean;
    }) => {
        return await gatewayClient.fetchTenantAuditLogs(params);
    }, [gatewayClient]);

    return {
        exportMetricsData,
        fetchDashboardMetrics,
        fetchErrorRateTimeSeries,
        fetchHourlyHeatmap,
        fetchLatencyTimeSeries,
        fetchPerformanceMetrics,
        fetchRealTimeMetrics,
        fetchRequestsTimeSeries,
        fetchServerHealthMetrics,
        fetchTenantAuditLogs,
    };
}

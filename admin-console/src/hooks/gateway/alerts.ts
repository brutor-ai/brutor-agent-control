/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * System alerts and the Assurance Inbox counts.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import { errorKind } from '../../lib/narrow';


export function useAlertsGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    // ------------------------------------------------------------------
    // System alerts — downstream failures, health transitions, infra
    // thresholds, and internal errors. Mission Control reads this feed.
    // ------------------------------------------------------------------

    /** Sidebar badges + the alerts drawer's Inbox line. */
    const fetchAssuranceInboxCounts = useCallback(async () => {
        return gatewayClient.getAssuranceInboxCounts();
    }, [gatewayClient]);

    const fetchSystemAlerts = useCallback(async (params: Parameters<typeof gatewayClient.fetchSystemAlerts>[0] = {}) => {
        try {
            return await gatewayClient.fetchSystemAlerts(params);
        } catch (error) {
            console.error('Failed to fetch system alerts:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch system alerts',
                code: 'FETCH_SYSTEM_ALERTS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_system_alerts', resource: 'system_alerts', ...params },
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const acknowledgeSystemAlert = useCallback(async (alertId: string) => {
        try {
            return await gatewayClient.acknowledgeSystemAlert(alertId);
        } catch (error) {
            console.error('Failed to acknowledge system alert:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to acknowledge system alert',
                code: 'ACKNOWLEDGE_SYSTEM_ALERT_ERROR',
                severity: 'medium',
                context: { operation: 'acknowledge_system_alert', resource: 'system_alerts', alertId },
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const resolveSystemAlert = useCallback(async (alertId: string, resolutionNote?: string) => {
        try {
            return await gatewayClient.resolveSystemAlert(alertId, resolutionNote);
        } catch (error) {
            console.error('Failed to resolve system alert:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to resolve system alert',
                code: 'RESOLVE_SYSTEM_ALERT_ERROR',
                severity: 'medium',
                context: { operation: 'resolve_system_alert', resource: 'system_alerts', alertId },
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        acknowledgeSystemAlert,
        fetchAssuranceInboxCounts,
        fetchSystemAlerts,
        resolveSystemAlert,
    };
}

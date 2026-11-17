/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Audit-log export destinations.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';
import { errorKind } from '../../lib/narrow';


export function useAuditExportGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    // =============================================================================
    // Audit Export Destinations
    // =============================================================================

    const fetchAuditExportDestinations = useCallback(async () => {
        try {
            return await gatewayClient.fetchAuditExportDestinations();
        } catch (error) {
            console.error('Failed to fetch audit export destinations:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch audit export destinations',
                code: 'FETCH_AUDIT_EXPORT_DESTINATIONS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_audit_export_destinations', resource: 'audit_export_destinations' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchAuditExportDestination = useCallback(async (destinationId: string) => {
        try {
            return await gatewayClient.fetchAuditExportDestination(destinationId);
        } catch (error) {
            console.error('Failed to fetch audit export destination:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch audit export destination',
                code: 'FETCH_AUDIT_EXPORT_DESTINATION_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_audit_export_destination', destinationId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const createAuditExportDestination = useCallback(async (data: Parameters<typeof gatewayClient.createAuditExportDestination>[0]) => {
        try {
            return await gatewayClient.createAuditExportDestination(data);
        } catch (error) {
            console.error('Failed to create audit export destination:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to create audit export destination',
                code: 'CREATE_AUDIT_EXPORT_DESTINATION_ERROR',
                severity: 'medium',
                context: { operation: 'create_audit_export_destination', name: data.name }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const updateAuditExportDestination = useCallback(async <Sent>(destinationId: string, updates: ApiBodyExactly<'/v1/admin/audit-export-destinations/{destination_id}', 'patch', Sent>) => {
        try {
            return await gatewayClient.updateAuditExportDestination(destinationId, updates);
        } catch (error) {
            console.error('Failed to update audit export destination:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to update audit export destination',
                code: 'UPDATE_AUDIT_EXPORT_DESTINATION_ERROR',
                severity: 'medium',
                context: { operation: 'update_audit_export_destination', destinationId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteAuditExportDestination = useCallback(async (destinationId: string) => {
        try {
            return await gatewayClient.deleteAuditExportDestination(destinationId);
        } catch (error) {
            console.error('Failed to delete audit export destination:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete audit export destination',
                code: 'DELETE_AUDIT_EXPORT_DESTINATION_ERROR',
                severity: 'medium',
                context: { operation: 'delete_audit_export_destination', destinationId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const runAuditExportDestinationNow = useCallback(async (destinationId: string) => {
        try {
            return await gatewayClient.runAuditExportDestinationNow(destinationId);
        } catch (error) {
            console.error('Failed to run audit export destination:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to run audit export destination',
                code: 'RUN_AUDIT_EXPORT_DESTINATION_ERROR',
                severity: 'medium',
                context: { operation: 'run_audit_export_destination_now', destinationId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        createAuditExportDestination,
        deleteAuditExportDestination,
        fetchAuditExportDestination,
        fetchAuditExportDestinations,
        runAuditExportDestinationNow,
        updateAuditExportDestination,
    };
}

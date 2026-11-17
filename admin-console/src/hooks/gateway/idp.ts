/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Identity providers.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';
import { errorKind } from '../../lib/narrow';


export function useIdpGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    // =============================================================================
    // Identity Provider (IdP) Management Operations (Admin)
    // =============================================================================

    const fetchIdPConfigs = useCallback(async (params: {
        enabled_only?: boolean;
    } = {}) => {
        try {
            return await gatewayClient.fetchIdPConfigs(params);
        } catch (error) {
            console.error('Failed to fetch IdP configs:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch IdP configurations',
                code: 'FETCH_IDP_CONFIGS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_idp_configs', resource: 'idp_configs', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchIdPConfig = useCallback(async (idpId: string) => {
        try {
            return await gatewayClient.fetchIdPConfig(idpId);
        } catch (error) {
            console.error('Failed to fetch IdP config:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch IdP configuration',
                code: 'FETCH_IDP_CONFIG_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_idp_config', resource: 'idp_config', idpId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const createIdPConfig = useCallback(async (idpData: any) => {
        try {
            return await gatewayClient.createIdPConfig(idpData);
        } catch (error) {
            console.error('Failed to create IdP config:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to create IdP configuration',
                code: 'CREATE_IDP_CONFIG_ERROR',
                severity: 'medium',
                context: { operation: 'create_idp_config', resource: 'idp_config' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const updateIdPConfig = useCallback(async <Sent>(idpId: string, updates: ApiBodyExactly<'/v1/admin/idps/{idp_id}', 'patch', Sent>) => {
        try {
            return await gatewayClient.updateIdPConfig(idpId, updates);
        } catch (error) {
            console.error('Failed to update IdP config:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to update IdP configuration',
                code: 'UPDATE_IDP_CONFIG_ERROR',
                severity: 'medium',
                context: { operation: 'update_idp_config', resource: 'idp_config', idpId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteIdPConfig = useCallback(async (idpId: string) => {
        try {
            return await gatewayClient.deleteIdPConfig(idpId);
        } catch (error) {
            console.error('Failed to delete IdP config:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete IdP configuration',
                code: 'DELETE_IDP_CONFIG_ERROR',
                severity: 'medium',
                context: { operation: 'delete_idp_config', resource: 'idp_config', idpId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const discoverIdPEndpoints = useCallback(async (idpId: string, forceRefresh: boolean = false) => {
        try {
            return await gatewayClient.discoverIdPEndpoints(idpId, forceRefresh);
        } catch (error) {
            console.error('Failed to discover IdP endpoints:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to discover IdP endpoints',
                code: 'DISCOVER_IDP_ENDPOINTS_ERROR',
                severity: 'medium',
                context: { operation: 'discover_idp_endpoints', resource: 'idp_discovery', idpId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const testExistingIdP = useCallback(async (idpId: string) => {
        try {
            return await gatewayClient.testExistingIdP(idpId);
        } catch (error) {
            console.error('Failed to test existing IdP:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to test IdP',
                code: 'TEST_EXISTING_IDP_ERROR',
                severity: 'medium',
                context: { operation: 'test_existing_idp', resource: 'idp_test', idpId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        createIdPConfig,
        deleteIdPConfig,
        discoverIdPEndpoints,
        fetchIdPConfig,
        fetchIdPConfigs,
        testExistingIdP,
        updateIdPConfig,
    };
}

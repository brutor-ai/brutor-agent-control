/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuth tokens held for MCP servers and the OAuth app clients.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';
import { errorKind } from '../../lib/narrow';


export function useOAuthGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    // ------------------------------------------------------------------
    // OAuth tokens (read-only diagnostic view for the admin console).
    // ------------------------------------------------------------------

    const fetchOAuthTokens = useCallback(async (params: Parameters<typeof gatewayClient.fetchOAuthTokens>[0] = {}) => {
        try {
            return await gatewayClient.fetchOAuthTokens(params);
        } catch (error) {
            console.error('Failed to fetch oauth tokens:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch OAuth tokens',
                code: 'FETCH_OAUTH_TOKENS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_oauth_tokens', resource: 'oauth_tokens', ...params },
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const revokeOAuthToken = useCallback(async (tokenId: number) => {
        try {
            return await gatewayClient.revokeOAuthToken(tokenId);
        } catch (error) {
            console.error('Failed to revoke oauth token:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to revoke OAuth token',
                code: 'REVOKE_OAUTH_TOKEN_ERROR',
                severity: 'high',
                context: { operation: 'revoke_oauth_token', resource: 'oauth_tokens', tokenId },
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const revokeOAuthTokensBulk = useCallback(
        async (filter: Parameters<typeof gatewayClient.revokeOAuthTokensBulk>[0]) => {
            try {
                return await gatewayClient.revokeOAuthTokensBulk(filter);
            } catch (error) {
                console.error('Failed to bulk-revoke oauth tokens:', errorKind(error));
                addError({
                    message: error instanceof Error ? error.message : 'Failed to bulk-revoke OAuth tokens',
                    code: 'REVOKE_OAUTH_TOKENS_BULK_ERROR',
                    severity: 'high',
                    context: { operation: 'revoke_oauth_tokens_bulk', resource: 'oauth_tokens', filter },
                });
                throw error;
            }
        },
        [gatewayClient, addError],
    );

    // =============================================================================
    // OAuth App Client Management Operations (Admin)
    // =============================================================================

    const fetchOAuthAppClients = useCallback(async (params: {
        app_type?: string;
        idp_id?: string;
        active_only?: boolean;
    } = {}): Promise<{ clients: any[]; total: number }> => {
        try {
            return await gatewayClient.fetchOAuthAppClients(params);
        } catch (error) {
            console.error('Failed to fetch OAuth app clients:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch OAuth app clients',
                code: 'FETCH_OAUTH_APP_CLIENTS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_oauth_app_clients', resource: 'oauth_app_clients', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const createOAuthAppClient = useCallback(async (clientData: any): Promise<any> => {
        try {
            return await gatewayClient.createOAuthAppClient(clientData);
        } catch (error) {
            console.error('Failed to create OAuth app client:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to create OAuth app client',
                code: 'CREATE_OAUTH_APP_CLIENT_ERROR',
                severity: 'medium',
                context: { operation: 'create_oauth_app_client', resource: 'oauth_app_client' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const updateOAuthAppClient = useCallback(async <Sent>(clientId: string, updates: ApiBodyExactly<'/v1/admin/oauth-app-clients/{client_id}', 'patch', Sent>): Promise<any> => {
        try {
            return await gatewayClient.updateOAuthAppClient(clientId, updates);
        } catch (error) {
            console.error('Failed to update OAuth app client:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to update OAuth app client',
                code: 'UPDATE_OAUTH_APP_CLIENT_ERROR',
                severity: 'medium',
                context: { operation: 'update_oauth_app_client', resource: 'oauth_app_client', clientId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteOAuthAppClient = useCallback(async (clientId: string): Promise<any> => {
        try {
            return await gatewayClient.deleteOAuthAppClient(clientId);
        } catch (error) {
            console.error('Failed to delete OAuth app client:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete OAuth app client',
                code: 'DELETE_OAUTH_APP_CLIENT_ERROR',
                severity: 'medium',
                context: { operation: 'delete_oauth_app_client', resource: 'oauth_app_client', clientId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const getOAuthAppClientUsage = useCallback(async (clientId: string): Promise<any> => {
        try {
            return await gatewayClient.getOAuthAppClientUsage(clientId);
        } catch (error) {
            console.error('Failed to get OAuth app client usage:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to get OAuth app client usage',
                code: 'GET_OAUTH_APP_CLIENT_USAGE_ERROR',
                severity: 'medium',
                context: { operation: 'get_oauth_app_client_usage', resource: 'oauth_app_client', clientId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        createOAuthAppClient,
        deleteOAuthAppClient,
        fetchOAuthAppClients,
        fetchOAuthTokens,
        getOAuthAppClientUsage,
        revokeOAuthToken,
        revokeOAuthTokensBulk,
        updateOAuthAppClient,
    };
}

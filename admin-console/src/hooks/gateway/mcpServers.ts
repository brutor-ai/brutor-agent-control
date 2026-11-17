/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCP servers, their icons, virtual servers and server configurations.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import { MCPServerConfig, VirtualServerConfig } from '../../lib/types';
import type { GatewayCore } from './core';
import { isHttpError } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';
import { errorKind } from '../../lib/narrow';


export function useMcpServersGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;



    const fetchServers = useCallback(async (): Promise<MCPServerConfig[]> => {
        try {
            const serverList = await gatewayClient.fetchServers();
            return serverList;
        } catch (error) {
            console.error('Failed to fetch servers:', errorKind(error));
            if (isHttpError(error) && (error.status === 401 || error.status === 403)) {
                console.error('Authentication required to view servers.');
                throw error;
            }
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch servers',
                code: 'FETCH_SERVERS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_servers', resource: 'servers' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchVirtualServers = useCallback(async (): Promise<VirtualServerConfig[]> => {
        try {
            const vsList = await gatewayClient.fetchVirtualServers();
            return vsList;
        } catch (error) {
            console.error('Failed to fetch virtual servers:', errorKind(error));
            if (isHttpError(error) && (error.status === 401 || error.status === 403)) {
                console.error('Authentication required to view virtual servers.');
                throw error;
            }
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch virtual servers',
                code: 'FETCH_VIRTUAL_SERVERS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_virtual_servers', resource: 'virtual_servers' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    // =============================================================================
    // MCP Server Management Operations
    // =============================================================================

    const createServer = useCallback(async (server: Omit<MCPServerConfig, 'id'>) => {
        try {
            const result = await gatewayClient.createServer(server);
            return result;
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to create server',
                code: 'CREATE_SERVER_ERROR',
                severity: 'medium',
                context: { operation: 'create_server', resource: 'server' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const updateServer = useCallback(async (id: string, server: Partial<MCPServerConfig>) => {
        try {
            const result = await gatewayClient.updateServer(id, server);
            return result;
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to update server',
                code: 'UPDATE_SERVER_ERROR',
                severity: 'medium',
                context: { operation: 'update_server', resource: 'server', serverId: id }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const refreshServerCapabilities = useCallback(async (id: string) => {
        try {
            return await gatewayClient.refreshServerCapabilities(id);
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to refresh capabilities',
                code: 'REFRESH_CAPABILITIES_ERROR',
                severity: 'medium',
                context: { operation: 'refresh_server_capabilities', resource: 'server', serverId: id }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteServer = useCallback(async (id: string) => {
        try {
            await gatewayClient.deleteServer(id);
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete server',
                code: 'DELETE_SERVER_ERROR',
                severity: 'medium',
                context: { operation: 'delete_server', resource: 'server', serverId: id }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    // =============================================================================
    // Icon Management Operations
    // =============================================================================

    const uploadServerIcon = useCallback(async (serverId: string, file: File) => {
        try {
            const result = await gatewayClient.uploadServerIcon(serverId, file);
            return result;
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to upload server icon',
                code: 'UPLOAD_ICON_ERROR',
                severity: 'medium',
                context: { operation: 'upload_server_icon', resource: 'server_icon', serverId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteServerIcon = useCallback(async (serverId: string) => {
        try {
            await gatewayClient.deleteServerIcon(serverId);
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete server icon',
                code: 'DELETE_ICON_ERROR',
                severity: 'medium',
                context: { operation: 'delete_server_icon', resource: 'server_icon', serverId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const getServerIconUrl = useCallback((serverId: string): string => {
        return gatewayClient.getServerIconUrl(serverId);
    }, [gatewayClient]);

    // =============================================================================
    // Virtual Server Management Operations
    // =============================================================================

    const createVirtualServer = useCallback(async (virtualServer: Omit<VirtualServerConfig, 'created_at'>) => {
        try {
            const result = await gatewayClient.createVirtualServer(virtualServer);
            return result;
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to create virtual server',
                code: 'CREATE_VIRTUAL_SERVER_ERROR',
                severity: 'medium',
                context: { operation: 'create_virtual_server', resource: 'virtual_server' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const updateVirtualServer = useCallback(async <Sent>(id: string, virtualServer: ApiBodyExactly<'/v1/admin/virtual-servers/{server_id}', 'patch', Sent>) => {
        try {
            const result = await gatewayClient.updateVirtualServer(id, virtualServer);
            return result;
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to update virtual server',
                code: 'UPDATE_VIRTUAL_SERVER_ERROR',
                severity: 'medium',
                context: { operation: 'update_virtual_server', resource: 'virtual_server', serverId: id }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteVirtualServer = useCallback(async (id: string) => {
        try {
            await gatewayClient.deleteVirtualServer(id);
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete virtual server',
                code: 'DELETE_VIRTUAL_SERVER_ERROR',
                severity: 'medium',
                context: { operation: 'delete_virtual_server', resource: 'virtual_server', serverId: id }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const testServerConnection = useCallback(async (serverData: {
        url: string;
        auth_type: string;
        timeout?: number;
    }) => {
        try {
            return await gatewayClient.testServerConnection(serverData);
        } catch (error) {
            addError({
                message: error instanceof Error ? error.message : 'Server connection test failed',
                code: 'TEST_SERVER_CONNECTION_ERROR',
                severity: 'medium',
                context: { operation: 'test_server_connection', resource: 'server_connection', url: serverData.url }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

// =============================================================================
// Server Config Management Operations (Admin)
// =============================================================================

    const fetchServerConfigs = useCallback(async (params: {
        mcp_server_id?: string;
        oauth_client_id?: string;
        app_type?: string;
        enabled_only?: boolean;
    } = {}): Promise<{ configs: any[]; total: number }> => {
        try {
            return await gatewayClient.fetchServerConfigs(params);
        } catch (error) {
            console.error('Failed to fetch server configs:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch server configurations',
                code: 'FETCH_SERVER_CONFIGS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_server_configs', resource: 'server_configs', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const createServerConfig = useCallback(async (configData: any): Promise<any> => {
        try {
            return await gatewayClient.createServerConfig(configData);
        } catch (error) {
            console.error('Failed to create server config:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to create server configuration',
                code: 'CREATE_SERVER_CONFIG_ERROR',
                severity: 'medium',
                context: { operation: 'create_server_config', resource: 'server_config' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const updateServerConfig = useCallback(async <Sent>(configId: string, updates: ApiBodyExactly<'/v1/admin/server-configs/{config_id}', 'patch', Sent>): Promise<any> => {
        try {
            return await gatewayClient.updateServerConfig(configId, updates);
        } catch (error) {
            console.error('Failed to update server config:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to update server configuration',
                code: 'UPDATE_SERVER_CONFIG_ERROR',
                severity: 'medium',
                context: { operation: 'update_server_config', resource: 'server_config', configId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteServerConfig = useCallback(async (configId: string): Promise<any> => {
        try {
            return await gatewayClient.deleteServerConfig(configId);
        } catch (error) {
            console.error('Failed to delete server config:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete server configuration',
                code: 'DELETE_SERVER_CONFIG_ERROR',
                severity: 'medium',
                context: { operation: 'delete_server_config', resource: 'server_config', configId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        createServer,
        createServerConfig,
        createVirtualServer,
        deleteServer,
        deleteServerConfig,
        deleteServerIcon,
        deleteVirtualServer,
        fetchServerConfigs,
        fetchServers,
        fetchVirtualServers,
        getServerIconUrl,
        refreshServerCapabilities,
        testServerConnection,
        updateServer,
        updateServerConfig,
        updateVirtualServer,
        uploadServerIcon,
    };
}

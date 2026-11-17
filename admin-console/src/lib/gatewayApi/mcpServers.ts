/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCP servers, virtual servers, server configurations and the MCP catalog.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import {
    MCPServerConfig,
    VirtualServerConfig,
    ServerConnectionTestData,
    ServerConnectionTestResult,
    RefreshCapabilitiesResponse,
} from '../types';
import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';
import type { components } from '../generated/control-plane-api';
import { GatewayApiBase } from './base';

/** What an update of an MCP server may carry: the control plane's update model. */
type ServerUpdateRequest = components['schemas']['ServerUpdateRequest'];

/**
 * The fields of that model. An update sends these and nothing else: the
 * form holds the server as it was read, with what the control plane states
 * about it (who created it, its health, its capabilities, the identity
 * provider's name), and none of that is the administrator's to write.
 */
export const SERVER_UPDATE_FIELDS = [
    'name',
    'description',
    'version',
    'base_url',
    'mcp_endpoint_path',
    'auth_type',
    'auth_token',
    'idp_id',
    'timeout',
    'max_concurrent_requests',
    'enabled',
    'mcp_apps_enabled',
    'wants_listener',
    'tenant_limits',
    'tenant_governance',
    'region',
] as const satisfies readonly (keyof ServerUpdateRequest)[];

// Every field of the model is listed: a field the control plane adds does
// not compile until it is.
const EVERY_FIELD_IS_LISTED: Exclude<keyof ServerUpdateRequest, (typeof SERVER_UPDATE_FIELDS)[number]> extends never ? true : false = true;
void EVERY_FIELD_IS_LISTED;

type ServerUpdateField = (typeof SERVER_UPDATE_FIELDS)[number];

/**
 * `server` with the fields an update accepts, as far as it has them: each
 * under its own name and with the type the server holds it in, so the
 * request can hold them to the contract's.
 */
function serverUpdateBody(server: Partial<MCPServerConfig>): Pick<Partial<MCPServerConfig>, ServerUpdateField & keyof MCPServerConfig> {
    type Picked = Pick<Partial<MCPServerConfig>, ServerUpdateField & keyof MCPServerConfig>;
    const given: Partial<Record<string, unknown>> = server;
    // Each value is `server`'s own under the same name, which is what `Picked` says.
    return Object.fromEntries(SERVER_UPDATE_FIELDS.filter((field) => field in given).map((field) => [field, given[field]])) as Picked;
}

export class McpServersApi extends GatewayApiBase {

    // =============================================================================
    // MCP Server Operations
    // =============================================================================

    /**
     * Get all configured MCP servers
     */
    async fetchServers(): Promise<MCPServerConfig[]> {
        try {
            const { servers } = await this.request<{ servers: Record<string, MCPServerConfig>; total: number }>('/v1/admin/mcp-servers');
            return Object.values(servers);
        } catch (error) {
            this.errorReporter(error, { operation: 'fetch_servers', resource: 'mcp_servers' });
            throw error;
        }
    }

    /**
     * Get a specific MCP server by ID
     */
    async fetchServer(id: string): Promise<MCPServerConfig> {
        try {
            return await this.request<MCPServerConfig>(`/v1/admin/mcp-servers/${encodeURIComponent(id)}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_server',
                resource: 'mcp_server',
                serverId: id
            });
            throw error;
        }
    }

    /**
     * Create a new MCP server with optional capability discovery
     */
    async createServer(
        server: Omit<MCPServerConfig, 'id'>,
        discoverCapabilities: boolean = true
    ): Promise<any> {
        try {
            const url = `/v1/admin/mcp-servers${discoverCapabilities ? '?discover_capabilities=true' : '?discover_capabilities=false'}`;
            const result = await this.request(url, {
                method: 'POST',
                body: JSON.stringify(server),
            });
            return result;
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_server',
                resource: 'mcp_server',
                serverName: server.name
            });
            throw error;
        }
    }

    /**
     * Update an existing MCP server. `server` may be the whole server as it
     * was read: only the fields the update model accepts are sent.
     */
    async updateServer(id: string, server: Partial<MCPServerConfig>): Promise<MCPServerConfig> {
        try {
            return await this.request<MCPServerConfig>(`/v1/admin/mcp-servers/${encodeURIComponent(id)}`, {
                method: 'PATCH',
                body: JSON.stringify(serverUpdateBody(server) satisfies ApiRequestBody<'/v1/admin/mcp-servers/{server_id}', 'patch'>),
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_server',
                resource: 'mcp_server',
                serverId: id,
                serverName: server.name
            });
            throw error;
        }
    }

    /**
     * Re-discover an MCP server's tools/resources/prompts in one click.
     * Backs the "Refresh capabilities" Action-menu item — same code path
     * as create / update with discover_capabilities=true, but doesn't
     * require opening the edit form.
     */
    async refreshServerCapabilities(id: string): Promise<RefreshCapabilitiesResponse> {
        try {
            return await this.request<RefreshCapabilitiesResponse>(
                `/v1/admin/mcp-servers/${encodeURIComponent(id)}/discover-capabilities`,
                { method: 'POST' }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'refresh_server_capabilities',
                resource: 'mcp_server',
                serverId: id,
            });
            throw error;
        }
    }

    /**
     * Delete an MCP server
     */
    async deleteServer(id: string): Promise<void> {
        try {
            return await this.request(`/v1/admin/mcp-servers/${encodeURIComponent(id)}`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_server',
                resource: 'mcp_server',
                serverId: id
            });
            throw error;
        }
    }

    /**
     * Test connection to a new MCP server (before adding it)
     */
    async testServerConnection(serverData: ServerConnectionTestData): Promise<ServerConnectionTestResult> {
        try {
            return await this.request('/v1/admin/mcp-servers/test-connection', {
                method: 'POST',
                body: JSON.stringify({
                    url: serverData.url,
                    auth_type: serverData.auth_type,
                    timeout: serverData.timeout || 10.0
                }),
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'test_server_connection',
                resource: 'mcp_server'
            });
            throw error;
        }
    }

    /**
     * Upload server icon from file
     */
    async uploadServerIcon(serverId: string, file: File): Promise<any> {
        try {
            const formData = new FormData();
            formData.append('file', file);

            return await this.request(`/v1/admin/mcp-servers/${encodeURIComponent(serverId)}/icon`, {
                method: 'POST',
                body: formData,
                headers: {} // Let browser set Content-Type for FormData
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'upload_server_icon',
                resource: 'server_icon',
                serverId: serverId
            });
            throw error;
        }
    }

    /**
     * Upload server icon from URL
     */
    async uploadServerIconFromUrl(serverId: string, iconUrl: string): Promise<any> {
        try {
            const formData = new FormData();
            formData.append('icon_url', iconUrl);

            return await this.request(`/v1/admin/mcp-servers/${encodeURIComponent(serverId)}/icon`, {
                method: 'POST',
                body: formData,
                headers: {} // Let browser set Content-Type for FormData
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'upload_server_icon_from_url',
                resource: 'server_icon',
                serverId: serverId
            });
            throw error;
        }
    }

    /**
     * Upload server icon from base64 data
     */
    async uploadServerIconFromBase64(serverId: string, base64Data: string): Promise<any> {
        try {
            const formData = new FormData();
            formData.append('icon_base64', base64Data);

            return await this.request(`/v1/admin/mcp-servers/${encodeURIComponent(serverId)}/icon`, {
                method: 'POST',
                body: formData,
                headers: {} // Let browser set Content-Type for FormData
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'upload_server_icon_from_base64',
                resource: 'server_icon',
                serverId: serverId
            });
            throw error;
        }
    }

    /**
     * Delete server icon
     */
    async deleteServerIcon(serverId: string): Promise<void> {
        try {
            await this.request(`/v1/admin/mcp-servers/${encodeURIComponent(serverId)}/icon`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_server_icon',
                resource: 'server_icon',
                serverId: serverId
            });
            throw error;
        }
    }

    /**
     * Get server icon URL
     */
    getServerIconUrl(serverId: string): string {
        return `${this.baseUrl}/v1/admin/mcp-servers/${encodeURIComponent(serverId)}/icon`;
    }

    // =============================================================================
    // Virtual Server Operations
    // =============================================================================

    /**
     * Get all virtual servers
     */
    async fetchVirtualServers(): Promise<VirtualServerConfig[]> {
        try {
            // Backend returns { servers: [...], total: number }
            const response = await this.request<{ servers: VirtualServerConfig[]; total: number }>('/v1/admin/virtual-servers');

            // Return the servers array directly
            return response.servers || [];
        } catch (error) {
            this.errorReporter(error, { operation: 'fetch_virtual_servers', resource: 'virtual_servers' });
            throw error;
        }
    }

    /**
     * Get a specific virtual server by ID
     */
    async fetchVirtualServer(id: string): Promise<VirtualServerConfig> {
        try {
            // Backend returns server object directly
            const virtualServer = await this.request<VirtualServerConfig>(`/v1/admin/virtual-servers/${encodeURIComponent(id)}`);
            return virtualServer;
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_virtual_server',
                resource: 'virtual_server',
                serverId: id
            });
            throw error;
        }
    }

    /**
     * Create a new virtual server
     */
    async createVirtualServer(virtualServer: Omit<VirtualServerConfig, 'created_at'>): Promise<ApiResponse<'/v1/admin/virtual-servers', 'post'>> {
        try {
            return await this.request('/v1/admin/virtual-servers', {
                method: 'POST',
                body: JSON.stringify(virtualServer),
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_virtual_server',
                resource: 'virtual_server',
                serverName: virtualServer.name ?? undefined
            });
            throw error;
        }
    }

    /**
     * Update an existing virtual server
     */
    async updateVirtualServer<Sent>(id: string, virtualServer: ApiBodyExactly<'/v1/admin/virtual-servers/{server_id}', 'patch', Sent>): Promise<any> {
        try {
            return await this.request(`/v1/admin/virtual-servers/${encodeURIComponent(id)}`, {
                method: 'PATCH',
                body: JSON.stringify(virtualServer),
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_virtual_server',
                resource: 'virtual_server',
                serverId: id,
                serverName: virtualServer.name ?? undefined
            });
            throw error;
        }
    }

    /**
     * Delete a virtual server
     */
    async deleteVirtualServer(id: string): Promise<void> {
        try {
            await this.request(`/v1/admin/virtual-servers/${encodeURIComponent(id)}`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_virtual_server',
                resource: 'virtual_server',
                serverId: id
            });
            throw error;
        }
    }

    // =============================================================================
    // Server Config Management Operations (Admin)
    // =============================================================================

    /**
     * List all server configurations
     */
    async fetchServerConfigs(params: {
        mcp_server_id?: string;
        oauth_client_id?: string;
        app_type?: string;
        enabled_only?: boolean;
    } = {}): Promise<{ configs: any[]; total: number }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.mcp_server_id) searchParams.append('mcp_server_id', params.mcp_server_id);
            if (params.oauth_client_id) searchParams.append('oauth_client_id', params.oauth_client_id);
            if (params.app_type) searchParams.append('app_type', params.app_type);
            if (params.enabled_only) searchParams.append('enabled_only', 'true');

            return await this.request(`/v1/admin/server-configs?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_server_configs',
                resource: 'server_configs',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get a specific server configuration
     */
    async fetchServerConfig(configId: string): Promise<ApiResponse<'/v1/admin/server-configs/{config_id}', 'get'>> {
        try {
            return await this.request(`/v1/admin/server-configs/${encodeURIComponent(configId)}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_server_config',
                resource: 'server_config',
                configId
            });
            throw error;
        }
    }

    /**
     * Create a new server configuration
     */
    async createServerConfig(configData: {
        /** One of the two: the MCP server the config is for, or the virtual server. */
        mcp_server_id?: string;
        virtual_server_id?: string;
        oauth_client_id?: string;
        group_ids?: string[];
        name: string;
        description?: string;
        priority?: number;
        is_default?: boolean;
        enabled?: boolean;
        display_name?: string;
        display_description?: string;
        icon_url?: string;
    }): Promise<any> {
        try {
            return await this.request('/v1/admin/server-configs', {
                method: 'POST',
                body: JSON.stringify(configData)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_server_config',
                resource: 'server_config',
                config_name: configData.name
            });
            throw error;
        }
    }

    /**
     * Update an existing server configuration
     */
    async updateServerConfig<Sent>(configId: string, updates: ApiBodyExactly<'/v1/admin/server-configs/{config_id}', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/server-configs/{config_id}', 'patch'>> {
        try {
            return await this.request(`/v1/admin/server-configs/${encodeURIComponent(configId)}`, {
                method: 'PATCH',
                body: JSON.stringify(updates)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_server_config',
                resource: 'server_config',
                configId
            });
            throw error;
        }
    }

    /**
     * Delete a server configuration
     */
    async deleteServerConfig(configId: string): Promise<any> {
        try {
            return await this.request(`/v1/admin/server-configs/${encodeURIComponent(configId)}`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_server_config',
                resource: 'server_config',
                configId
            });
            throw error;
        }
    }

    /**
     * Get all configurations for a specific MCP server
     */
    async getServerConfigsByServer(mcpServerId: string, enabledOnly: boolean = false): Promise<ApiResponse<'/v1/admin/server-configs/stats', 'get'>> {
        try {
            const searchParams = new URLSearchParams();
            if (enabledOnly) searchParams.append('enabled_only', 'true');

            return await this.request(
                `/v1/admin/server-configs/by-server/${encodeURIComponent(mcpServerId)}?${searchParams}`
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'get_server_configs_by_server',
                resource: 'server_configs',
                mcpServerId
            });
            throw error;
        }
    }

    /**
     * Get all configurations using a specific OAuth client
     */
    async getServerConfigsByOAuthClient(oauthClientId: string): Promise<ApiResponse<'/v1/admin/server-configs/stats', 'get'>> {
        try {
            return await this.request(
                `/v1/admin/server-configs/by-oauth-client/${encodeURIComponent(oauthClientId)}`
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'get_server_configs_by_oauth_client',
                resource: 'server_configs',
                oauthClientId
            });
            throw error;
        }
    }

    /**
     * Get server configuration statistics
     */
    async getServerConfigStatistics(): Promise<ApiResponse<'/v1/admin/server-configs/stats', 'get'>> {
        try {
            return await this.request('/v1/admin/server-configs/stats');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'get_server_config_statistics',
                resource: 'server_config_stats'
            });
            throw error;
        }
    }

    // ========================================================================
    // MCP Server Catalog
    // ========================================================================

    async fetchMCPCatalogEntries(params?: {
        search?: string;
        runtime?: string;
        auth_pattern?: string;
        category?: string;
        source?: string;
        enabled_only?: boolean;
        offset?: number;
        limit?: number;
    }) {
        const query = new URLSearchParams();
        if (params?.search) query.set('search', params.search);
        if (params?.runtime) query.set('runtime', params.runtime);
        if (params?.auth_pattern) query.set('auth_pattern', params.auth_pattern);
        if (params?.category) query.set('category', params.category);
        if (params?.source) query.set('source', params.source);
        if (params?.enabled_only) query.set('enabled_only', 'true');
        if (params?.offset !== undefined) query.set('offset', String(params.offset));
        if (params?.limit !== undefined) query.set('limit', String(params.limit));
        const qs = query.toString();
        return this.request(`/v1/admin/mcp-catalog${qs ? `?${qs}` : ''}`);
    }

    async fetchMCPCatalogEntry(entryId: string) {
        return this.request(`/v1/admin/mcp-catalog/${entryId}`);
    }

    async createMCPCatalogEntry(data: Record<string, unknown>) {
        return this.request('/v1/admin/mcp-catalog', {
            method: 'POST',
            body: JSON.stringify(data),
        });
    }

    async updateMCPCatalogEntry<Sent>(entryId: string, data: ApiBodyExactly<'/v1/admin/mcp-catalog/{entry_id}', 'put', Sent>) {
        return this.request(`/v1/admin/mcp-catalog/${entryId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    async deleteMCPCatalogEntry(entryId: string) {
        return this.request(`/v1/admin/mcp-catalog/${entryId}`, { method: 'DELETE' });
    }

    // ---- Global MCP catalog curation (system admin only) -------------------
    // Browse uses the same GET /v1/admin/mcp-catalog route (it returns
    // global ∪ own-tenant). These methods
    // create/update/delete GLOBAL system entries (tenant_id NULL).

    async createGlobalMCPCatalogEntry(data: Record<string, unknown>) {
        // `is_global: true` makes the backend mint a global system entry
        // (id mcp-cat-system-{slug}, source=system) and gates on sysadmin.
        return this.request('/v1/admin/mcp-catalog', {
            method: 'POST',
            body: JSON.stringify({ ...data, is_global: true }),
        });
    }

    async updateGlobalMCPCatalogEntry<Sent>(entryId: string, data: ApiBodyExactly<'/v1/admin/mcp-catalog/{entry_id}', 'put', Sent>) {
        // Same PUT route; the backend enforces sysadmin when the target
        // entry is global (tenant_id NULL).
        return this.request(`/v1/admin/mcp-catalog/${entryId}`, {
            method: 'PUT',
            body: JSON.stringify(data),
        });
    }

    async deleteGlobalMCPCatalogEntry(entryId: string) {
        return this.request(`/v1/admin/mcp-catalog/${entryId}`, { method: 'DELETE' });
    }

    async fetchMCPCatalogCategories() {
        return this.request('/v1/admin/mcp-catalog/categories');
    }

    async importMCPCatalogEntry(
        entryId: string,
        importRequest: Record<string, unknown>,
        opts: { timeout?: number } = {},
    ) {
        // Catalog imports that flip `deploy_container: true` synchronously
        // pull the image from Docker Hub + start the container + probe its
        // /mcp endpoint. First-pull on a fresh trial-bundle host commonly
        // takes 60–180s — the default 30s `request()` timeout cuts the UI
        // off long before the backend finishes (and the backend's
        // gunicorn worker-timeout, default 120s, kills the worker shortly
        // after). The caller passes `timeout: 300_000` for the
        // containerised import flow; remote imports keep the default.
        return this.request(`/v1/admin/mcp-catalog/${entryId}/import`, {
            method: 'POST',
            body: JSON.stringify(importRequest),
            timeout: opts.timeout,
        });
    }
}

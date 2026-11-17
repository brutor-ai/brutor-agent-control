/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Gateway health, statistics and the health-check configuration.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import { GatewayStats } from '../types';
import {
    BulkHealthCheckResponse,
    HealthCheckConfig,
    HealthStatusResponse,
} from '../healthTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly } from '../api/controlPlaneTypes';

export class HealthApi extends GatewayApiBase {

    // =============================================================================
    // Gateway Stats and Health
    // =============================================================================

    /**
     * Get gateway service health (NOT MCP server health)
     * This checks if the gateway service itself is running properly
     */
    async fetchGatewayHealth(): Promise<any> {
        try {
            return await this.request('/health');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_gateway_health',
                resource: 'gateway_service'
            });
            throw error;
        }
    }

    /**
     * Get gateway statistics without triggering health checks
     */
    async fetchStats(): Promise<GatewayStats> {
        try {
            // Use gateway health endpoint for basic stats
            const healthResponse = await this.fetchGatewayHealth();

            return {
                total_servers: healthResponse.statistics?.total_servers || 0,
                enabled_servers: healthResponse.statistics?.enabled_servers || 0,
                virtual_servers: healthResponse.statistics?.virtual_servers || 0,
                active_connections: 0, // Gateway health doesn't include active connections
                total_requests: healthResponse.statistics?.total_requests || 0,
                error_rate: 0, // Gateway health doesn't calculate error rates
                uptime: healthResponse.uptime_seconds || 0
            };
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_stats',
                resource: 'gateway_stats'
            });
            throw error;
        }
    }

    /**
     * Check health of individual MCP server
     * GET /v1/admin/mcp-servers/{server_id}/health
     */
    async checkServerHealth(serverId: string, timeout: number = 10.0): Promise<HealthStatusResponse> {
        try {
            const params = new URLSearchParams({
                timeout: timeout.toString()
            });

            return await this.request(`/v1/admin/mcp-servers/${encodeURIComponent(serverId)}/health?${params}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'check_server_health',
                resource: 'mcp_server',
                serverId: serverId
            });
            throw error;
        }
    }

    /**
     * Check health of multiple MCP servers (bulk check)
     * POST /v1/admin/mcp-servers/health/bulk
     */
    async checkAllMCPServerHealth(
        enabledOnly: boolean = true,
        timeout: number = 10.0,
        serverIds?: string[]
    ): Promise<BulkHealthCheckResponse> {
        try {
            const params = new URLSearchParams({
                enabled_only: enabledOnly.toString(),
                timeout: timeout.toString()
            });

            const body = serverIds ? { server_ids: serverIds } : undefined;

            return await this.request(
                `/v1/admin/mcp-servers/health/bulk?${params}`,
                {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: body ? JSON.stringify(body) : undefined
                }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'check_all_mcp_server_health',
                resource: 'mcp_servers',
                serverIds: serverIds
            });
            throw error;
        }
    }

    // =============================================================================
    // Health Check Configuration Management
    // =============================================================================

    /**
     * Get health check configuration for current tenant
     *
     * Returns configuration for both MCP Servers and AI Models.
     * Creates default config if none exists.
     */
    async fetchHealthCheckConfig(): Promise<HealthCheckConfig> {
        try {
            return await this.request('/v1/admin/gateway-config/health-config');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_health_check_config',
                resource: 'health_config'
            });
            throw error;
        }
    }

    /**
     * Update health check configuration
     *
     * @param data - Partial config update (only provided fields are updated)
     */
    async updateHealthCheckConfig<Sent>(data: ApiBodyExactly<'/v1/admin/gateway-config/health-config', 'patch', Sent>): Promise<HealthCheckConfig> {
        try {
            return await this.request('/v1/admin/gateway-config/health-config', {
                method: 'PATCH',
                body: JSON.stringify(data)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_health_check_config',
                resource: 'health_config'
            });
            throw error;
        }
    }

    /**
     * Reset health check configuration to defaults
     */
    async resetHealthCheckConfig(): Promise<HealthCheckConfig> {
        try {
            return await this.request('/v1/admin/gateway-config/health-config/reset', {
                method: 'POST'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'reset_health_check_config',
                resource: 'health_config'
            });
            throw error;
        }
    }

    /**
     * Get default health check configuration values
     *
     * Returns default values without creating/modifying any records.
     * Useful for displaying defaults in UI.
     */
    async fetchDefaultHealthCheckConfig(): Promise<HealthCheckConfig> {
        try {
            return await this.request('/v1/admin/gateway-config/health-config/defaults');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_default_health_check_config',
                resource: 'health_config'
            });
            throw error;
        }
    }
}

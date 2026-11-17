/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Health Check Type Definitions
 */

/**
 * Information about who performed the health check
 */
export interface CheckedByInfo {
    user_id: string;
    username: string;
    admin_type: 'system' | 'tenant';
}

/**
 * Health status response for individual server
 */
export interface HealthStatusResponse {
    server_id: string;
    server_name: string;
    server_url: string;
    status: 'healthy' | 'unhealthy' | 'error';
    connected: boolean;
    timestamp: string;
    health_status: string;
    check_duration_ms: number;
    error?: string;
    checked_by?: CheckedByInfo;
}

/**
 * Bulk health check response
 */
export interface BulkHealthCheckResponse {
    total: number;
    checked: number;
    healthy: number;
    unhealthy: number;
    failed: number;
    results: HealthStatusResponse[];
}

/*
 * The health-check settings as the Control Plane returns them: the ones a
 * component reads. The console's polling reads `enabled`,
 * `interval_seconds` and `pause_when_inactive`; the background model sweep
 * reads the AI `failure_threshold`. The rest of the document (timeouts,
 * retries, recovery interval, the MCP failure threshold, the AI test prompt
 * and token count, the dashboard's show flags) is not available in this
 * release: the Control Plane refuses a value and returns null, and the
 * console neither reads nor sends one.
 */

/** Dashboard health check settings. */
export interface DashboardHealthCheckSettings {
    enabled: boolean;
    interval_seconds: number;
}

/** MCP server health check settings. */
export interface MCPHealthCheckSettings {
    enabled: boolean;
    interval_seconds: number;
    pause_when_inactive: boolean;
}

/** AI model health check settings. */
export interface AIHealthCheckSettings {
    enabled: boolean;
    interval_seconds: number;
    failure_threshold: number;
    pause_when_inactive: boolean;
}

/**
 * Complete health check configuration
 */
export interface HealthCheckConfig {
    id: string;
    tenant_id: string;
    dashboard_settings: DashboardHealthCheckSettings;
    mcp_settings: MCPHealthCheckSettings;
    ai_settings: AIHealthCheckSettings;
    created_at: string | null;
    updated_at: string | null;
    created_by: string | null;
    updated_by: string | null;
}

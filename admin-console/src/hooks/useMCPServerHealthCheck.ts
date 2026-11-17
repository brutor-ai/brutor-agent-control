/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useCallback } from 'react';
import { useGateway } from './useGateway';
import { useHealthCheckBase } from './useHealthCheckBase';

/**
 * MCP Server Health Check Hook
 *
 * Manages periodic health checks for the MCP Server Tab.
 *
 * - Fetches mcp_settings from dedicated health check config API
 * - Checks MCP Servers ONLY
 * - Independent from other tabs
 * - Only runs when MCP Server Tab is active (mount/unmount)
 *
 * @returns {Object} MCP Server health check state and controls
 */
export const useMCPServerHealthCheck = () => {
    const { triggerAllHealthChecks } = useGateway();

    // Define health check function using useCallback to maintain stable reference
    const healthCheckFn = useCallback(async () => {
        await triggerAllHealthChecks();
    }, [triggerAllHealthChecks]);

    // Use base hook with MCP-specific configuration
    return useHealthCheckBase({
        tabName: 'MCP Servers',
        configKey: 'mcp_settings',
        healthCheckFn,
        defaultInterval: 300  // 5 minutes
    });
};

export default useMCPServerHealthCheck;
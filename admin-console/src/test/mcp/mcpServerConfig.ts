/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A stored MCP server, as the Control Plane answers it: every field the type
 * requires, with the values of an enabled server that takes no credential,
 * has not been probed and has not been discovered. A test names the fields
 * it is about.
 */

import type { MCPServerConfig } from '../../lib/types';

export function buildMCPServerConfig(overrides: Partial<MCPServerConfig> = {}): MCPServerConfig {
    return {
        id: 'srv-1',
        name: 'server',
        description: '',
        base_url: 'https://mcp.unit.example',
        mcp_endpoint_path: '/mcp',
        auth_type: 'none',
        timeout: 30,
        enabled: true,
        ...overrides,
    };
}

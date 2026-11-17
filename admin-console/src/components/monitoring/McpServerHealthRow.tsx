/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * One MCP server on Mission Control's infrastructure panel: its health in
 * words, the error of its last check when it has one, and when it was last
 * checked.
 */

import React from 'react';

import { serverHealthReading, type ServerHealthStatus } from '../../lib/serverHealth';
import { SERVER_HEALTH_LOOK } from '../mcp/ServerHealthBadge';

interface McpServerHealthRowProps {
    name: string;
    health: ServerHealthStatus | null | undefined;
}

const McpServerHealthRow: React.FC<McpServerHealthRowProps> = ({ name, health }) => {
    const reading = serverHealthReading(health);
    const look = SERVER_HEALTH_LOOK[reading.state];
    const Icon = look.icon;
    return (
        <div
            className="flex items-center gap-3 p-2.5 rounded-lg border hover:bg-surface-alt transition-colors"
            data-testid="mcp-server-health"
            data-health={reading.state}
        >
            <div className={`w-2 h-2 rounded-full flex-shrink-0 ${look.dot}`} />
            <Icon className={`w-4 h-4 ${look.iconColor}`} />
            <div className="flex-1 min-w-0 min-h-0">
                <span className="text-sm font-medium text-default truncate block">{name}</span>
                <p className="text-xs text-muted truncate">
                    {reading.label}
                    {health?.error_message ? <span className="text-red-500"> · {health.error_message}</span> : null}
                </p>
            </div>
            <span className="text-xs text-muted flex-shrink-0">
                {health?.last_check ? new Date(health.last_check).toLocaleTimeString() : '—'}
            </span>
        </div>
    );
};

export default McpServerHealthRow;

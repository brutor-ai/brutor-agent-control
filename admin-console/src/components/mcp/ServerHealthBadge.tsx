/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The health of an MCP server as a badge, and how each reading looks. The
 * screens that show a server's health take both from here, so a status reads
 * the same on each of them (`lib/serverHealth.ts` has the readings).
 */

import React from 'react';
import { AlertTriangle, CheckCircle, Clock, HelpCircle, XCircle } from 'lucide-react';

import { serverHealthReading, type ServerHealthReading, type ServerHealthStatus } from '../../lib/serverHealth';

interface HealthLook {
    icon: React.ComponentType<{ className?: string }>;
    /** The colour of the icon. */
    iconColor: string;
    /** The colour of the label on a neutral ground. */
    text: string;
    /** The ground of a round badge. */
    pill: string;
    /** The ground and the text of a square badge. */
    tag: string;
    /** The dot before a row. */
    dot: string;
}

/** One look per reading; `unrecognised` is a status no server is stored with. */
export const SERVER_HEALTH_LOOK: Record<ServerHealthReading['state'], HealthLook> = {
    healthy: { icon: CheckCircle, iconColor: 'text-green-600', text: 'text-green-700', pill: 'bg-surface-alt', tag: 'bg-green-100 text-green-700', dot: 'bg-green-500' },
    degraded: { icon: AlertTriangle, iconColor: 'text-yellow-600', text: 'text-yellow-700', pill: 'bg-surface-alt', tag: 'bg-yellow-100 text-yellow-700', dot: 'bg-yellow-500' },
    unhealthy: { icon: XCircle, iconColor: 'text-red-600', text: 'text-red-700', pill: 'bg-surface-alt', tag: 'bg-red-100 text-red-700', dot: 'bg-red-500' },
    unknown: { icon: Clock, iconColor: 'text-muted', text: 'text-secondary', pill: 'bg-surface-raised', tag: 'bg-surface-raised text-muted', dot: 'bg-gray-400' },
    unrecognised: { icon: HelpCircle, iconColor: 'text-muted', text: 'text-secondary', pill: 'bg-surface-raised', tag: 'bg-surface-raised text-muted', dot: 'bg-gray-400' },
};

interface ServerHealthBadgeProps {
    health: ServerHealthStatus | null | undefined;
    /** `pill`: round, on a table of a Resource Group. `tag`: square, beside a server's name. */
    shape: 'pill' | 'tag';
}

/** The badge. Its title is the error of the last check, when there is one. */
const ServerHealthBadge: React.FC<ServerHealthBadgeProps> = ({ health, shape }) => {
    const reading = serverHealthReading(health);
    const look = SERVER_HEALTH_LOOK[reading.state];
    const Icon = look.icon;
    if (shape === 'tag') {
        return (
            <span
                className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium flex-shrink-0 ${look.tag}`}
                title={health?.error_message || `Health: ${reading.label}`}
                data-health={reading.state}
            >
                <Icon className="w-3 h-3 mr-1" />
                {reading.label}
            </span>
        );
    }
    return (
        <div className={`inline-flex items-center space-x-1 px-2 py-1 rounded-full ${look.pill}`} data-health={reading.state}>
            <Icon className={`w-3 h-3 ${look.iconColor}`} />
            <span className={`text-xs font-medium ${look.text}`}>{reading.label}</span>
        </div>
    );
};

export default ServerHealthBadge;

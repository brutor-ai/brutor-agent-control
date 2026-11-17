/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The health of an MCP server, as the Control Plane states it on a server
 * (`health_status`), and what each status reads as.
 *
 * The contract types the status as a string. Four values are written to it:
 *
 *   healthy    the last check, or the last call through the gateway, worked
 *   degraded   the gateway saw one to four failures in a row
 *   unhealthy  a check failed, five failures in a row, or the circuit is open
 *   unknown    never checked
 *
 * Any other value is read as itself: "Unknown status: <value>".
 */

import type { components } from './generated/control-plane-api';

export type ServerHealthStatus = components['schemas']['ServerHealthStatus'];

export const SERVER_HEALTH_STATES = ['healthy', 'degraded', 'unhealthy', 'unknown'] as const;
export type ServerHealthState = (typeof SERVER_HEALTH_STATES)[number];

const LABELS: Record<ServerHealthState, string> = {
    healthy: 'Healthy',
    degraded: 'Degraded',
    unhealthy: 'Unhealthy',
    unknown: 'Unknown',
};

/** What a status reads as: one of the four states, or `unrecognised` with the value in its label. */
export interface ServerHealthReading {
    state: ServerHealthState | 'unrecognised';
    label: string;
}

const isState = (status: string): status is ServerHealthState => (SERVER_HEALTH_STATES as readonly string[]).includes(status);

/** The reading of a server's health; a server with no health or no status has never been checked. */
export function serverHealthReading(health: { status?: string | null } | null | undefined): ServerHealthReading {
    const status = health?.status;
    if (!status) return { state: 'unknown', label: LABELS.unknown };
    if (isState(status)) return { state: status, label: LABELS[status] };
    return { state: 'unrecognised', label: `Unknown status: ${status}` };
}

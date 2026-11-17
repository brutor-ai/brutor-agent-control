/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What an MCP server's health status reads as. The status is a string in the
 * Control Plane's contract; four values are written to it. Each has its own
 * reading, and a value that is none of them is shown as what it is.
 */

import { describe, expect, it } from 'vitest';

import { SERVER_HEALTH_STATES, serverHealthReading } from '../serverHealth';

describe('serverHealthReading', () => {
    it.each([
        ['healthy', 'Healthy'],
        ['degraded', 'Degraded'],
        ['unhealthy', 'Unhealthy'],
        ['unknown', 'Unknown'],
    ])('reads %s as "%s"', (status, label) => {
        expect(serverHealthReading({ status })).toEqual({ state: status, label });
    });

    it('names exactly the four values that are written', () => {
        expect([...SERVER_HEALTH_STATES]).toEqual(['healthy', 'degraded', 'unhealthy', 'unknown']);
    });

    it.each([undefined, null, {}, { status: null }, { status: '' }])('reads a server never checked (%j) as Unknown', (health) => {
        expect(serverHealthReading(health)).toEqual({ state: 'unknown', label: 'Unknown' });
    });

    it.each(['warning', 'critical', 'HEALTHY', 'requires_tier'])('reads %s, which no server is stored with, as an unknown status that names it', (status) => {
        expect(serverHealthReading({ status })).toEqual({ state: 'unrecognised', label: `Unknown status: ${status}` });
    });
});

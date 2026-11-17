/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
  * Proxy-log cell formatting. The Capability column carries the MCP
  * capability or the handshake method — never the HTTP verb, which has its
  * own column; conflating the two reads as `POST (tools/list)` and hides
  * what was actually called.
  */

import { describe, it, expect } from 'vitest';
import { formatBytes, formatDuration, getCapabilityDisplay } from '../types';
import type { UnifiedProxyLogEntry } from '../types';

const log = (over: Partial<UnifiedProxyLogEntry> = {}): UnifiedProxyLogEntry =>
    ({ id: 'l1', ...over }) as UnifiedProxyLogEntry;

describe('getCapabilityDisplay', () => {
    it('prefers the capability name over the JSON-RPC method', () => {
        expect(
            getCapabilityDisplay(
                log({ mcp: { capability_name: 'search_docs', method: 'tools/call' } })
            )
        ).toBe('search_docs');
    });

    it('falls back to the method for handshake and discovery calls', () => {
        expect(getCapabilityDisplay(log({ mcp: { method: 'tools/list' } }))).toBe(
            'tools/list'
        );
    });

    it('is null for a non-MCP row — the column stays empty, not "undefined"', () => {
        expect(getCapabilityDisplay(log())).toBeNull();
        expect(getCapabilityDisplay(log({ mcp: {} }))).toBeNull();
    });

    it('does not treat an empty capability name as a name', () => {
        expect(
            getCapabilityDisplay(
                log({ mcp: { capability_name: '', method: 'tools/call' } })
            )
        ).toBe('tools/call');
    });
});

describe('formatDuration', () => {
    it('escalates from sub-millisecond to milliseconds to seconds', () => {
        expect(formatDuration(0.4)).toBe('<1ms');
        expect(formatDuration(1)).toBe('1ms');
        expect(formatDuration(999)).toBe('999ms');
        expect(formatDuration(1000)).toBe('1.00s');
    });

    it('drops to one decimal above ten seconds', () => {
        expect(formatDuration(9999)).toBe('10.00s');
        expect(formatDuration(10000)).toBe('10.0s');
        expect(formatDuration(65432)).toBe('65.4s');
    });

    it('rounds sub-millisecond fractions rather than printing them raw', () => {
        expect(formatDuration(12.6)).toBe('13ms');
    });

    it('says N/A when there is no measurement, but keeps a real zero visible', () => {
        expect(formatDuration(undefined)).toBe('N/A');
        expect(formatDuration(null as unknown as number)).toBe('N/A');
        expect(formatDuration(0)).toBe('<1ms');
    });
});

describe('formatBytes', () => {
    it('renders whole bytes without a decimal', () => {
        expect(formatBytes(0)).toBe('0 B');
        expect(formatBytes(512)).toBe('512 B');
        expect(formatBytes(1023)).toBe('1023 B');
    });

    it('scales through KB, MB and GB with one decimal', () => {
        expect(formatBytes(1024)).toBe('1.0 KB');
        expect(formatBytes(1536)).toBe('1.5 KB');
        expect(formatBytes(1024 ** 2)).toBe('1.0 MB');
        expect(formatBytes(1024 ** 3 * 2.5)).toBe('2.5 GB');
    });

    it('says N/A when the size is unknown', () => {
        expect(formatBytes(undefined)).toBe('N/A');
        expect(formatBytes(null as unknown as number)).toBe('N/A');
    });

    it('clamps to the largest unit instead of running off the table', () => {
        // An implausible size must still render a readable cell, not
        // "1.0 undefined".
        expect(formatBytes(1024 ** 4)).toBe('1.0 TB');
        expect(formatBytes(1024 ** 6)).toBe('1048576.0 TB');
        expect(formatBytes(1024 ** 8)).not.toContain('undefined');
    });

    it('does not go below the first unit on a fractional byte count', () => {
        expect(formatBytes(0.5)).toBe('0.5 B');
    });
});

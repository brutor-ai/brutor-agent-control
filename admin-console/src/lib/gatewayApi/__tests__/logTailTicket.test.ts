/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The log tail opens with a ticket, never with the session token.
 *
 * `openLogsWebSocket` asks `POST /v1/admin/system/logs/tail/ticket` for a
 * single-use ticket and opens `/v1/admin/system/logs/tail?ticket=…`. The
 * session token is sent in the Authorization header of the ticket request
 * and appears nowhere in the address of the socket.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GatewayApiClient } from '../../gatewayApiClient';

const SESSION = `${btoa('{"alg":"HS256"}')}.${btoa(JSON.stringify({ exp: 4102444800 }))}.sig`;

const sent: Array<{ method: string; url: string; authorization: string | undefined }> = [];
const opened: string[] = [];

class FakeWebSocket {
    constructor(url: string) {
        opened.push(url);
    }
}

let client: GatewayApiClient;

beforeEach(() => {
    sent.length = 0;
    opened.length = 0;
    vi.stubGlobal(
        'fetch',
        vi.fn((url: unknown, init?: RequestInit) => {
            const headers = (init?.headers ?? {}) as Record<string, string>;
            sent.push({ method: init?.method ?? 'GET', url: String(url), authorization: headers.Authorization });
            return Promise.resolve(
                new Response(JSON.stringify({ ticket: 'tk-1/+=', expires_in: 30 }), {
                    status: 201,
                    headers: { 'Content-Type': 'application/json' },
                }),
            );
        }),
    );
    vi.stubGlobal('WebSocket', FakeWebSocket);
    client = new GatewayApiClient('https://cp.test', () => {});
    client.setAuthToken(SESSION);
});

afterEach(() => {
    client.clearAuthToken();
    vi.unstubAllGlobals();
});

describe('opening the log tail', () => {
    it('asks for a ticket, then opens the socket with it and without the session token', async () => {
        const socket = await client.openLogsWebSocket('warning', 'guardrails');

        expect(socket).toBeInstanceOf(FakeWebSocket);
        expect(sent).toEqual([
            { method: 'POST', url: 'https://cp.test/v1/admin/system/logs/tail/ticket', authorization: `Bearer ${SESSION}` },
        ]);
        expect(opened).toEqual(['wss://cp.test/v1/admin/system/logs/tail?ticket=tk-1%2F%2B%3D&level=warning&module=guardrails']);
        expect(opened[0]).not.toContain(SESSION);
        expect(opened[0]).not.toContain('token=');
    });

    it('opens nothing without a session', async () => {
        client.clearAuthToken();
        expect(await client.openLogsWebSocket()).toBeNull();
        expect(sent).toEqual([]);
        expect(opened).toEqual([]);
    });
});

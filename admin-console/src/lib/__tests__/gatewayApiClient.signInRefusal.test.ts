/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A refused sign-in says what the control plane said.
 *
 * The control plane answers a wrong user name and a wrong password alike with
 * 401 `{"detail": "Invalid credentials"}` (`admin_rbac.py`):
 * the text tells nothing about which of the two was wrong. The
 * client's own word for every 401, "Authentication failed", told the
 * administrator less than that, so the sign-in shows the server's.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GatewayAPIError } from '../gatewayApi/base';
import { GatewayApiClient } from '../gatewayApiClient';

const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('GatewayApiClient: a refused sign-in', () => {
    let client: GatewayApiClient;

    beforeEach(() => {
        localStorage.clear();
        client = new GatewayApiClient('http://control.test', () => undefined);
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('reports the control plane\'s "Invalid credentials", as a 401', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ detail: 'Invalid credentials' }, 401));
        const refused: unknown = await client.login({ username: 'admin', password: 'wrong' }).catch((e: unknown) => e);
        expect(refused).toBeInstanceOf(GatewayAPIError);
        expect((refused as GatewayAPIError).message).toBe('Invalid credentials');
        expect((refused as GatewayAPIError).status).toBe(401);
    });

    it('keeps the client\'s own word for a 401 that says nothing', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({}, 401));
        await expect(client.login({ username: 'admin', password: 'wrong' })).rejects.toThrow('Authentication failed');
    });
});

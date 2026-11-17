/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The client's call that lifts the lock on a tenant administrator's account:
 * `POST /v1/admin/users/tenant/{admin_id}/unlock`, no body, in the caller's
 * session. The control plane answers the administrator as it now is, or
 * refuses (403 without `admin:user:update` or for another tenant's
 * administrator, 404 for none such); a refusal reaches the caller.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { GatewayApiClient } from '../gatewayApiClient';
import { jwt } from '../../test/controlPlane';
import { GatewayAPIError } from '../gatewayApi/base';

/** A session that has not expired: the client sends no request with one that has. */
const SESSION = jwt({ sub: 'adm-1' });

const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('GatewayApiClient — unlocking a tenant administrator', () => {
    let client: GatewayApiClient;

    beforeEach(() => {
        localStorage.clear();
        client = new GatewayApiClient('http://control.test', () => undefined);
        client.setAuthToken(SESSION);
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('posts to the unlock route of that administrator, with the session and no body', async () => {
        const unlocked = { id: 'u-2', username: 'bob', is_locked: false };
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(unlocked));

        await expect(client.unlockTenantAdminUser('u-2')).resolves.toEqual(unlocked);

        expect(fetch).toHaveBeenCalledTimes(1);
        const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit];
        expect(url).toBe('http://control.test/v1/admin/users/tenant/u-2/unlock');
        expect(init.method).toBe('POST');
        expect(init.body).toBeUndefined();
        expect((init.headers as Record<string, string>)['Authorization']).toBe(`Bearer ${SESSION}`);
    });

    it('writes the id as one path segment', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({}));

        await client.unlockTenantAdminUser('a/b?c');

        expect(vi.mocked(fetch).mock.calls[0][0]).toBe('http://control.test/v1/admin/users/tenant/a%2Fb%3Fc/unlock');
    });

    it("hands a refusal to the caller with the control plane's words", async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ detail: 'Cannot unlock admin from another tenant' }, 403));

        const refusal: unknown = await client.unlockTenantAdminUser('u-9').catch((error: unknown) => error);

        expect(refusal).toBeInstanceOf(GatewayAPIError);
        expect((refusal as GatewayAPIError).status).toBe(403);
        expect((refusal as GatewayAPIError).message).toBe('Cannot unlock admin from another tenant');
    });
});

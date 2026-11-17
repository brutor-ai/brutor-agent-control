/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The client's part of "password change required": a 403 whose detail is
 * `{code: "password_change_required", message}` is announced
 * (`PASSWORD_CHANGE_REQUIRED_EVENT`), so the console goes to the
 * change-password screen from whatever route was refused, and thrown with
 * that code and the Control Plane's sentence. The session is kept: the
 * account is signed in, it only has to change its password first. A sign-in
 * answer that says so hands the flag on.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GatewayApiClient } from '../../gatewayApiClient';
import { GatewayAPIError, GATEWAY_AUTH_TOKEN_KEY, PASSWORD_CHANGE_REQUIRED_EVENT, isPasswordChangeRequired } from '../base';
import { TENANT_ADMIN, TOKEN } from '../../../test/controlPlane';

const MESSAGE = "This account's password was generated for it and must be changed first: POST /v1/admin/users/me/change-password.";

const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('a route refused until the password is changed', () => {
    let client: GatewayApiClient;
    let announced: number;
    const listen = () => { announced += 1; };

    beforeEach(() => {
        localStorage.clear();
        announced = 0;
        window.addEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, listen);
        client = new GatewayApiClient('http://control.test', () => undefined);
        client.setAuthToken(TOKEN);
    });

    afterEach(() => {
        window.removeEventListener(PASSWORD_CHANGE_REQUIRED_EVENT, listen);
        vi.restoreAllMocks();
    });

    it('is announced, thrown with its code and sentence, and keeps the session', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ detail: { code: 'password_change_required', message: MESSAGE } }, 403));
        const refused = await client.request<unknown>('/v1/admin/tenants/current').catch((e: unknown) => e);
        expect(refused).toBeInstanceOf(GatewayAPIError);
        expect((refused as GatewayAPIError).status).toBe(403);
        expect((refused as GatewayAPIError).code).toBe('password_change_required');
        expect((refused as GatewayAPIError).message).toBe(MESSAGE);
        expect(isPasswordChangeRequired(refused)).toBe(true);
        expect(announced).toBe(1);
        expect(localStorage.getItem(GATEWAY_AUTH_TOKEN_KEY)).toBe(TOKEN);
    });

    it('another 403 is not announced', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ detail: 'Permission denied' }, 403));
        const refused = await client.request<unknown>('/v1/admin/tenants/current').catch((e: unknown) => e);
        expect(isPasswordChangeRequired(refused)).toBe(false);
        expect(announced).toBe(0);
    });

    it.each([
        ['at the top of the answer', { password_change_required: true, admin: TENANT_ADMIN }],
        ['in its admin', { admin: { ...TENANT_ADMIN, password_change_required: true } }],
    ])('a sign-in answer that says so %s hands the flag on', async (_where, answer) => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ access_token: TOKEN, tenant_id: 'acme', ...answer }));
        const signedIn = await client.login({ username: 'admin', password: 'x', tenant_id: 'acme' });
        expect(signedIn.password_change_required).toBe(true);
    });

    it('a sign-in answer that does not say so hands on false', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ access_token: TOKEN, tenant_id: 'acme', admin: TENANT_ADMIN }));
        const signedIn = await client.login({ username: 'admin', password: 'x', tenant_id: 'acme' });
        expect(signedIn.password_change_required).toBe(false);
    });
});

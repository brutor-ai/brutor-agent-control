/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The client's methods for the second factor (at sign-in and of the signed-in
 * administrator) and for creating an end user: the route, the method and the
 * body each sends.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { clientOver } from '../../../test/clientOver';

const request = vi.fn();
const client = clientOver(request);

beforeEach(() => {
    request.mockReset();
    request.mockResolvedValue({});
});

describe('the second factor at sign-in', () => {
    it('sends the temporary token and the code, and answers with the session', async () => {
        request.mockResolvedValue({ token: 'jwt' });
        await expect(client.completeTwoFactorLogin('tmp', '123456')).resolves.toEqual({ token: 'jwt' });
        expect(request).toHaveBeenCalledWith('/v1/admin/users/login/2fa', {
            method: 'POST',
            body: JSON.stringify({ temp_token: 'tmp', code: '123456' }),
        });
    });

    it('a refusal is thrown as the client got it, for the form to read the lock from', async () => {
        const refusal = Object.assign(new Error('Locked.'), { status: 423 });
        request.mockRejectedValue(refusal);
        await expect(client.completeTwoFactorLogin('tmp', '123456')).rejects.toBe(refusal);
    });
});

describe('the second factor of the signed-in administrator', () => {
    it('reads the status', async () => {
        request.mockResolvedValue({ enabled: true });
        await expect(client.getTwoFactorStatus()).resolves.toEqual({ enabled: true });
        expect(request).toHaveBeenCalledWith('/v1/admin/users/me/2fa/status');
    });

    it('starts a setup with no body', async () => {
        await client.startTwoFactorSetup();
        expect(request).toHaveBeenCalledWith('/v1/admin/users/me/2fa/setup', { method: 'POST' });
    });

    it('verifies and disables with the code', async () => {
        await client.verifyTwoFactorSetup('123456');
        expect(request).toHaveBeenLastCalledWith('/v1/admin/users/me/2fa/verify', { method: 'POST', body: JSON.stringify({ code: '123456' }) });
        await client.disableTwoFactor('654321');
        expect(request).toHaveBeenLastCalledWith('/v1/admin/users/me/2fa/disable', { method: 'POST', body: JSON.stringify({ code: '654321' }) });
    });
});

describe('creating an end user', () => {
    it('posts the body it was given', async () => {
        const body = { username: 'carol', email: 'c@acme.test', password: 'Passw0rd!Passw0rd', send_welcome_email: true };
        request.mockResolvedValue({ success: true, user: { id: 9 } });
        await expect(client.createEndUser(body)).resolves.toEqual({ success: true, user: { id: 9 } });
        expect(request).toHaveBeenCalledWith('/v1/admin/end-users', { method: 'POST', body: JSON.stringify(body) });
    });
});

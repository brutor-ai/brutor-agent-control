/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The client's part of enrolment at sign-in: `login` reports
 * `requires_2fa_enrollment` and its temp token without storing anything, and
 * the two enrolment calls go to the control plane's two routes carrying the
 * temp token in the body, never an Authorization header (there is no session
 * yet; a stale one must not ride along).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { GatewayApiClient } from '../gatewayApiClient';

const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

function lastCall(): { url: string; init: RequestInit; headers: Record<string, string> } {
    const calls = vi.mocked(fetch).mock.calls;
    const [url, init] = calls[calls.length - 1];
    return { url: url as string, init: init as RequestInit, headers: (init?.headers ?? {}) as Record<string, string> };
}

describe('GatewayApiClient — enrolment at sign-in', () => {
    let client: GatewayApiClient;

    beforeEach(() => {
        localStorage.clear();
        client = new GatewayApiClient('http://control.test');
        vi.spyOn(console, 'log').mockImplementation(() => {});
        vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('login reports the enrolment challenge and stores no token', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(
            json({
                requires_2fa_enrollment: true,
                temp_token: 'enrol-1',
                admin_type: 'tenant',
                tenant_id: 'acme',
                tenant_display_name: 'Acme Corp',
                admin: { username: 'admin', email: 'admin@acme.test' },
            }),
        );

        const result = await client.login({ username: 'admin', password: 'Admin123!', tenant_id: 'acme' });

        expect(result.requires_2fa_enrollment).toBe(true);
        expect(result.temp_token).toBe('enrol-1');
        expect(result.tenant_id).toBe('acme');
        expect(result.tenant_display_name).toBe('Acme Corp');
        expect(result.requires_2fa).toBeUndefined();
        expect(localStorage.getItem('gateway_auth_token')).toBeNull();
    });

    it('starts setup with the temp token in the body and no Authorization header', async () => {
        const setup = { secret: 'JBSWY3DPEHPK3PXP', provisioning_uri: 'otpauth://totp/x', qr_code: 'data:image/png;base64,QQ==' };
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(setup));

        await expect(client.startTwoFactorEnrollment('enrol-1')).resolves.toEqual(setup);

        const { url, init, headers } = lastCall();
        expect(url).toBe('http://control.test/v1/admin/users/login/2fa/enroll/setup');
        expect(init.method).toBe('POST');
        expect(JSON.parse(init.body as string)).toEqual({ temp_token: 'enrol-1' });
        expect(headers['Authorization']).toBeUndefined();
    });

    it('completes with the temp token and the code, and leaves installing the session to the caller', async () => {
        const session = { access_token: 'session-jwt', admin: { username: 'admin' }, tenant_id: 'acme', backup_codes: ['A1B2C3D4'] };
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(session));

        await expect(client.completeTwoFactorEnrollment('enrol-1', '123456')).resolves.toEqual(session);

        const { url, init, headers } = lastCall();
        expect(url).toBe('http://control.test/v1/admin/users/login/2fa/enroll/verify');
        expect(init.method).toBe('POST');
        expect(JSON.parse(init.body as string)).toEqual({ temp_token: 'enrol-1', code: '123456' });
        expect(headers['Authorization']).toBeUndefined();
        // Shown once by the form, then installed by the provider: not here.
        expect(localStorage.getItem('gateway_auth_token')).toBeNull();
    });

    it('passes the control plane\'s refusal on as the error message', async () => {
        vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ detail: 'Invalid 2FA code' }, 401));
        await expect(client.completeTwoFactorEnrollment('enrol-1', '000000')).rejects.toThrow('Invalid 2FA code');
    });
});

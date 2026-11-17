/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Resetting one conversation's spend budget.
 *
 * Session spend is SUMMED from the usage ledger rather than held in a
 * counter, because a conversation outlives any cache and a restart must not
 * hand a user a fresh allowance. That makes "reset" impossible to express as
 * a counter write — you cannot zero a sum.
 *
 * The server's answer is a watermark row: the ledger rows stay (they are the
 * audit record) and the sum starts from the watermark. This client call is
 * the only way an operator triggers that, so what it sends is a contract:
 *
 *   * the right verb and path — a GET here would be a no-op that looks like
 *     a success in the UI;
 *   * the session id escaped, since it is user-visible data in a path
 *     segment;
 *   * the reason forwarded, because a budget reset moves money and an
 *     unattributed one is exactly what an auditor asks about.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { GatewayApiClient } from '../gatewayApiClient';

describe('GatewayApiClient.resetSessionBudget', () => {
    let client: GatewayApiClient;
    let fetchSpy: ReturnType<typeof vi.fn>;

    const body = {
        session_id: 'sess-abc',
        reset_at: '2026-08-29T09:00:00Z',
        reset_by: 'admin',
        reason: 'goodwill',
        ledger_rows_retained: true,
    };

    beforeEach(() => {
        fetchSpy = vi.fn(async () =>
            new Response(JSON.stringify(body), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
        // @ts-expect-error - jsdom exposes global.fetch as a binding
        global.fetch = fetchSpy;
        client = new GatewayApiClient('http://gateway.test');
    });

    afterEach(() => vi.restoreAllMocks());

    const call = () => fetchSpy.mock.calls[0];
    const url = () => String(call()[0]);
    const init = () => call()[1] as RequestInit;

    it('POSTs to the session budget reset endpoint', async () => {
        await client.resetSessionBudget('sess-abc');
        expect(url()).toContain('/v1/admin/sessions/sess-abc/budget/reset');
        // A GET would be a no-op the UI would render as success.
        expect(init().method).toBe('POST');
    });

    it('forwards the reason so the reset is attributable', async () => {
        await client.resetSessionBudget('sess-abc', 'goodwill credit');
        expect(url()).toContain('reason=goodwill%20credit');
    });

    it('omits the reason parameter entirely when none is given', async () => {
        await client.resetSessionBudget('sess-abc');
        expect(url()).not.toContain('reason=');
    });

    it('escapes the session id in the path', async () => {
        // Derived keys are opaque, but this is user-visible data going into a
        // path segment — it gets encoded rather than trusted.
        await client.resetSessionBudget('sess/abc?x=1');
        expect(url()).toContain('sess%2Fabc%3Fx%3D1');
        expect(url()).not.toContain('sess/abc?x=1');
    });

    it('returns the server confirmation, including that history was kept', async () => {
        const res = await client.resetSessionBudget('sess-abc', 'goodwill');
        expect(res.session_id).toBe('sess-abc');
        expect(res.reset_by).toBe('admin');
        // The field exists so nobody reads "reset" as "deleted".
        expect(res.ledger_rows_retained).toBe(true);
    });

    it('propagates a failure instead of reporting a silent success', async () => {
        fetchSpy.mockResolvedValueOnce(
            new Response(JSON.stringify({ detail: 'forbidden' }), { status: 403 }),
        );
        await expect(client.resetSessionBudget('sess-abc')).rejects.toBeDefined();
    });
});

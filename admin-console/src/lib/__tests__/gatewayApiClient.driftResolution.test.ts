/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * What the client sends when a drift finding is closed and an
 * incident window is declared, edited or withdrawn. These are sealed
 * declarations the detector honours, so the wire is the contract:
 *
 *   * a close carries its kind and note in the body — a close that dropped
 *     them is a 422 at best, and a silent decision at worst;
 *   * false_positive goes to its own route (it feeds detector tuning);
 *   * cause_fixed carries the window bounds the operator confirmed;
 *   * acknowledge never reaches a closing route;
 *   * windows use the right verb and path per scope.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { GatewayApiClient } from '../gatewayApiClient';

describe('GatewayApiClient — drift resolution', () => {
    let client: GatewayApiClient;
    let fetchSpy: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        fetchSpy = vi.fn(() =>
            Promise.resolve(
                new Response(JSON.stringify({ id: 'x', seal: { sealed: true } }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }),
            ),
        );
        // @ts-expect-error - jsdom exposes global.fetch as a binding
        global.fetch = fetchSpy;
        client = new GatewayApiClient('http://gateway.test');
    });

    afterEach(() => vi.restoreAllMocks());

    const url = (i = 0) => String((fetchSpy.mock.calls[i] as unknown[])[0]);
    const init = (i = 0) => (fetchSpy.mock.calls[i] as unknown[])[1] as RequestInit;
    const body = (i = 0): unknown => JSON.parse(init(i).body as string);

    it('accepted_change and cause_fixed close on /resolve with the kind, note and bounds', async () => {
        await client.closeDriftEvent('drift-1', { kind: 'accepted_change', note: 'retune' });
        expect(url()).toMatch(/\/v1\/admin\/drift\/drift-1\/resolve$/);
        expect(init().method).toBe('POST');
        expect(body()).toEqual({ kind: 'accepted_change', note: 'retune' });

        await client.closeDriftEvent('drift-2', {
            kind: 'cause_fixed',
            note: 'outage',
            window_starts_at: '2026-09-27T21:00:00.000Z',
            window_ends_at: '2026-09-28T06:40:00.000Z',
            cause_category: 'provider_outage',
            tenant_wide: true,
        });
        expect(url(1)).toMatch(/\/drift\/drift-2\/resolve$/);
        expect(body(1)).toMatchObject({
            kind: 'cause_fixed',
            window_starts_at: '2026-09-27T21:00:00.000Z',
            window_ends_at: '2026-09-28T06:40:00.000Z',
            tenant_wide: true,
        });
    });

    it('false_positive closes on its own route, still with the kind', async () => {
        await client.closeDriftEvent('drift-3', { kind: 'false_positive', note: 'noise' });
        expect(url()).toMatch(/\/drift\/drift-3\/false-positive$/);
        expect(body()).toEqual({ kind: 'false_positive', note: 'noise' });
    });

    it('acknowledge posts to /ack and never to a closing route', async () => {
        await client.acknowledgeDriftEvent('drift-4', 'looking');
        expect(url()).toMatch(/\/drift\/drift-4\/ack$/);
        expect(body()).toEqual({ note: 'looking' });
    });

    it('declares on the system route when scoped, else on the tenant route', async () => {
        const w = {
            starts_at: '2026-09-29T02:00:00.000Z',
            ends_at: '2026-09-29T04:00:00.000Z',
            cause_category: 'maintenance' as const,
            reason: 'planned',
        };
        await client.createIncidentWindow(w, 'sys-1');
        expect(url()).toMatch(/\/v1\/admin\/ai-systems\/sys-1\/incident-windows$/);
        await client.createIncidentWindow({ ...w, ai_system_id: null });
        expect(url(1)).toMatch(/\/v1\/admin\/incident-windows$/);
        expect(body(1)).toMatchObject({ ai_system_id: null, reason: 'planned' });
        expect(init(1).method).toBe('POST');
    });

    it('edits with PATCH and withdraws with DELETE by window id', async () => {
        await client.updateIncidentWindow('iw-1', { reason: 'longer' });
        expect(url()).toMatch(/\/v1\/admin\/incident-windows\/iw-1$/);
        expect(init().method).toBe('PATCH');
        await client.deleteIncidentWindow('iw-1');
        expect(url(1)).toMatch(/\/v1\/admin\/incident-windows\/iw-1$/);
        expect(init(1).method).toBe('DELETE');
    });

    it('lists windows and epochs on their read routes', async () => {
        await client.fetchAISystemIncidentWindows('sys-1', { since: '2026-09-01T00:00:00Z' });
        expect(url()).toContain('/v1/admin/ai-systems/sys-1/incident-windows?since=2026-09-01T00%3A00%3A00Z');
        await client.fetchIncidentWindows();
        expect(url(1)).toMatch(/\/v1\/admin\/incident-windows$/);
        await client.fetchAISystemBaselineEpochs('sys-1');
        expect(url(2)).toMatch(/\/v1\/admin\/ai-systems\/sys-1\/baseline-epochs$/);
    });
});

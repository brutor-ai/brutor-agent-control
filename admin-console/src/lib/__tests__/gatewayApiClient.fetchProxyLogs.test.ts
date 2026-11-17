/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * fetchProxyLogs must forward every Datadog-style
 * multi-value filter key to /v1/admin/proxy-logs/query.
 *
 * Translating `params` field-by-field and forwarding only the
 * singular fields would silently drop every plural key
 * (`server_ids`, `gateway_user_ids`, `target_urls`,
 * `mcp_capability_names`, `llm_providers`, `llm_models_used`,
 * `http_status_codes`, `q`, `compliance_tags_any`, ...). The
 * sidebar's facet clicks would look broken: the values come back
 * from /facets but never reach the search query. This test fails
 * loudly if any of those keys goes missing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { GatewayApiClient } from '../gatewayApiClient';

describe('GatewayApiClient.fetchProxyLogs — multi-value filter forwarding', () => {
    let client: GatewayApiClient;
    let fetchSpy: ReturnType<typeof vi.fn>;

    beforeEach(() => {
        // Stub fetch with a single resolved response. We're testing
        // what the CLIENT sends, not what the server returns.
        fetchSpy = vi.fn(() =>
            Promise.resolve(
                new Response(JSON.stringify({ logs: [], pagination: {} }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }),
            ),
        );
        // @ts-expect-error - jsdom exposes global.fetch as a binding
        global.fetch = fetchSpy;
        client = new GatewayApiClient('http://gateway.test');
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    function lastRequestBody(): Record<string, unknown> {
        // RequestInit body is a string here because we serialise with
        // JSON.stringify in the client. The last fetch call carries
        // the payload we care about.
        const call = fetchSpy.mock.lastCall as [unknown, RequestInit | undefined] | undefined;
        if (!call) throw new Error('fetch was not called');
        const init = call[1];
        if (typeof init?.body !== 'string') throw new Error('body was not a JSON string');
        return JSON.parse(init.body) as Record<string, unknown>;
    }

    it('forwards plural multi-value filter keys verbatim', async () => {
        await client.fetchProxyLogs({
            server_ids: ['srv-a', 'srv-b'],
            gateway_user_ids: ['u-1'],
            target_urls: ['https://example.com/a', 'https://example.com/b'],
            mcp_capability_names: ['tools/list', 'tools/call'],
            llm_providers: ['anthropic'],
            llm_models_used: ['claude-opus-4-7'],
            http_status_codes: [200, 502],
            region_routed_to: ['us-east-1'],
            residency_class: ['eu'],
            compliance_tags_any: ['eu_ai_act.high'],
            q: 'denied',
        });

        const body = lastRequestBody();
        expect(body.server_ids).toEqual(['srv-a', 'srv-b']);
        expect(body.gateway_user_ids).toEqual(['u-1']);
        expect(body.target_urls).toEqual(['https://example.com/a', 'https://example.com/b']);
        expect(body.mcp_capability_names).toEqual(['tools/list', 'tools/call']);
        expect(body.llm_providers).toEqual(['anthropic']);
        expect(body.llm_models_used).toEqual(['claude-opus-4-7']);
        expect(body.http_status_codes).toEqual([200, 502]);
        expect(body.region_routed_to).toEqual(['us-east-1']);
        expect(body.residency_class).toEqual(['eu']);
        expect(body.compliance_tags_any).toEqual(['eu_ai_act.high']);
        expect(body.q).toBe('denied');
    });

    it('a filter that takes several values has one name, the list: the single-value twin is not sent', async () => {
        // The single-value names are parameters of the export route, not
        // fields of the search this client sends.
        await client.fetchProxyLogs({
            server_id: 'srv-a',
            gateway_user_id: 'u-1',
            mcp_capability_name: 'tools/list',
            llm_provider: 'anthropic',
            compliance_tag: 'gdpr_art_30',
            idp_server_ids: ['idp-1'],
            correlation_id: 'corr-xyz',
        } as never);

        expect(lastRequestBody()).toEqual({
            limit: 100,
            offset: 0,
            sort_by: 'timestamp',
            sort_order: 'desc',
            correlation_id: 'corr-xyz',
        });
    });

    it('has one method for the search', () => {
        expect('queryProxyLogs' in client).toBe(false);
        expect(typeof client.fetchProxyLogs).toBe('function');
    });

    it('drops undefined and empty-array params (no empty IN() risk)', async () => {
        // Empty arrays in the body would, on the backend, collapse the
        // result to 0 rows (`IN ()` is always false in SQL). The
        // client filters them out — same contract as the audit-logs
        // path.
        await client.fetchProxyLogs({
            server_ids: [],
            gateway_user_ids: undefined,
            target_urls: [],
            mcp_capability_names: ['tools/list'], // present + non-empty
        });

        const body = lastRequestBody();
        expect(body.server_ids).toBeUndefined();
        expect(body.gateway_user_ids).toBeUndefined();
        expect(body.target_urls).toBeUndefined();
        expect(body.mcp_capability_names).toEqual(['tools/list']);
    });

    it('still passes pagination + sort defaults when params are sparse', async () => {
        await client.fetchProxyLogs({});
        const body = lastRequestBody();
        expect(body.limit).toBe(100);
        expect(body.offset).toBe(0);
        expect(body.sort_by).toBe('timestamp');
        expect(body.sort_order).toBe('desc');
    });

    it('forwards time-range fields (start_time/end_time/hours_ago)', async () => {
        await client.fetchProxyLogs({
            start_time: '2026-06-01T00:00:00Z',
            end_time: '2026-06-02T00:00:00Z',
            hours_ago: 24,
        });
        const body = lastRequestBody();
        expect(body.start_time).toBe('2026-06-01T00:00:00Z');
        expect(body.end_time).toBe('2026-06-02T00:00:00Z');
        expect(body.hours_ago).toBe(24);
    });
});

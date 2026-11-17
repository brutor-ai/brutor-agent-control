/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The requests of the methods that take no tenant override.
 *
 * No method of the Community API modules takes a tenant id to put in the
 * query string (`?tenant_id=`), the way a system administrator addresses
 * another tenant. This is the record of the exact request each of these
 * methods sends when it is called the way the pages call it (the arguments
 * are the shapes at the call sites; a method no page calls is recorded in
 * its plain form). The record must not change.
 *
 * The guard that no Community method sends a tenant override at all is
 * `communityTenantOverride.test.ts`.
 */

import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GatewayApiClient } from '../../gatewayApiClient';
import { useAlertsGateway } from '../../../hooks/gateway/alerts';
import { useProxyLogsGateway } from '../../../hooks/gateway/proxyLogs';

const sent: string[] = [];

function describeBody(body: unknown): string {
    if (body === undefined || body === null) return '';
    return typeof body === 'string' ? ` ${body}` : ' <not a string body>';
}

function headerNames(init: RequestInit | undefined): string {
    const headers = init?.headers;
    if (!headers || headers instanceof Headers || Array.isArray(headers)) return '<not a plain headers object>';
    return Object.keys(headers).sort().join(',');
}

let client: GatewayApiClient;

beforeEach(() => {
    sent.length = 0;
    vi.stubGlobal(
        'fetch',
        vi.fn((url: unknown, init?: RequestInit) => {
            sent.push(`${init?.method ?? 'GET'} ${String(url)}${describeBody(init?.body)} [${headerNames(init)}]`);
            return Promise.resolve(
                new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
            );
        }),
    );
    client = new GatewayApiClient('http://cp.test', () => {});
});

afterEach(() => {
    vi.unstubAllGlobals();
});

const addError = () => 'error-id';

describe('the recorded requests', () => {
    it('agent cards', async () => {
        await client.listA2AAgentCards({ enabled_only: true });
        await client.listA2AAgentCards({ enabled_only: undefined });
        await client.listA2AAgentCards();
        await client.getA2AAgentCard('card-1');
        await client.createA2AAgentCard({
            name: 'Planner',
            version: '1.0.0',
            description: null,
            source_url: null,
            card_json: { name: 'Planner' },
        });
        await client.updateA2AAgentCard('card-1', {
            name: 'Planner',
            version: '1.0.1',
            description: 'plans',
            card_json: { name: 'Planner' },
            enabled: true,
        });
        await client.deleteA2AAgentCard('card-1');
        await client.signA2AAgentCard('card-1', {});
        await client.signA2AAgentCard('card-1');
        await client.revokeA2AAgentCard('card-1', { reason: 'compromised' });
        await client.importA2AAgentCard({
            source_url: 'https://agents.test/planner',
            name_override: 'Planner',
            description_override: null,
        });
        await client.discoverA2AAgentCard({ source_url: 'https://agents.test/planner' });
        await client.bulkImportA2AAgentCards({ imports: [{ source_url: 'https://agents.test/planner' }] });
        await client.healthCheckA2AAgentCard('card-1');
        await client.bulkHealthCheckA2AAgentCards();
        await client.fetchA2ARegistryCatalog({ force_refresh: true });
        await client.fetchA2ARegistryCatalog({ force_refresh: undefined });
        await client.getA2ATenantPublicKey();
        await client.setA2AAgentCardGroups('card-1', ['group-1', 'group-2']);
        expect(sent).toMatchInlineSnapshot(`
          [
            "GET http://cp.test/v1/admin/agent-cards?enabled_only=true [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/agent-cards [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/agent-cards [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/agent-cards/card-1 [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards {"name":"Planner","version":"1.0.0","description":null,"source_url":null,"card_json":{"name":"Planner"}} [Accept,Content-Type]",
            "PATCH http://cp.test/v1/admin/agent-cards/card-1 {"name":"Planner","version":"1.0.1","description":"plans","card_json":{"name":"Planner"},"enabled":true} [Accept,Content-Type]",
            "DELETE http://cp.test/v1/admin/agent-cards/card-1 [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/card-1/sign {} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/card-1/sign {} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/card-1/revoke {"reason":"compromised"} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/import {"source_url":"https://agents.test/planner","name_override":"Planner","description_override":null} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/discover {"source_url":"https://agents.test/planner"} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/import-bulk {"imports":[{"source_url":"https://agents.test/planner"}]} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/card-1/health-check [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/agent-cards/health-check/bulk [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/agent-cards/catalog?force_refresh=true [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/agent-cards/catalog [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/agent-cards/tenant-key [Accept,Content-Type]",
            "PUT http://cp.test/v1/admin/agent-cards/card-1/resource-groups {"group_ids":["group-1","group-2"]} [Accept,Content-Type]",
          ]
        `);
    });

    it('delegation graphs and tasks', async () => {
        await client.listA2ADelegationGraphs({ limit: 25, offset: 50, since: '2026-11-01T00:00:00Z', success: undefined });
        await client.listA2ADelegationGraphs({ limit: 25, offset: 0, since: '2026-11-01T00:00:00Z', success: false });
        await client.getA2ADelegationGraph('root/1');
        await client.getA2ADelegationStats({ since: '2026-11-01T00:00:00Z' });
        await client.listA2ATasks({ state: 'working', context_id: undefined, limit: 50, offset: 0 });
        await client.listA2ATasks({ state: undefined, context_id: 'ctx 1', limit: 50, offset: 100 });
        await client.getA2ATask('task/1');
        await client.listA2ATaskPushConfigs('task/1');
        expect(sent).toMatchInlineSnapshot(`
          [
            "GET http://cp.test/v1/admin/a2a/delegation-graphs?limit=25&offset=50&since=2026-11-01T00%3A00%3A00Z [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/a2a/delegation-graphs?limit=25&offset=0&since=2026-11-01T00%3A00%3A00Z&success=false [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/a2a/delegation-graphs/root%2F1 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/a2a/delegation-graphs/stats?since=2026-11-01T00%3A00%3A00Z [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/a2a-tasks?state=working&limit=50&offset=0 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/a2a-tasks?context_id=ctx+1&limit=50&offset=100 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/a2a-tasks/task%2F1 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/a2a-tasks/task%2F1/push-configs [Accept,Content-Type]",
          ]
        `);
    });

    it('proxy logs: query, facets, statistics, thread, activity, clean-up', async () => {
        await client.fetchProxyLogs({
            limit: 50,
            offset: 100,
            sort_by: 'duration_ms',
            sort_order: 'asc',
            hours_ago: 24,
            proxy_types: ['mcp'],
            server_ids: ['srv-a'],
        });
        await client.fetchProxyLogFacets({ hours_ago: 24, proxy_types: ['llm'] });
        await client.suggestProxyLogValues({ field: 'server_id', prefix: 'sr' });
        await client.fetchProxyStats({ hours_ago: 6, proxy_types: ['mcp', 'llm'] });
        await client.fetchProxyStats();
        await client.fetchRequestThread('corr-1');
        await client.fetchUserActivity('user-1', { hours_ago: 12, proxy_types: ['oauth'] });
        await client.fetchUserActivity('user-1');
        await client.cleanupProxyLogs(30);
        expect(sent).toMatchInlineSnapshot(`
          [
            "POST http://cp.test/v1/admin/proxy-logs/query {"limit":50,"offset":100,"sort_by":"duration_ms","sort_order":"asc","hours_ago":24,"proxy_types":["mcp"],"server_ids":["srv-a"]} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/proxy-logs/facets {"hours_ago":24,"proxy_types":["llm"]} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/proxy-logs/suggest {"field":"server_id","prefix":"sr"} [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/proxy-logs/statistics?hours_ago=6&proxy_types=mcp%2Cllm [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/proxy-logs/statistics?hours_ago=24 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/proxy-logs/thread/corr-1 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/proxy-logs/user/user-1/activity?hours_ago=12&proxy_types=oauth [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/proxy-logs/user/user-1/activity?hours_ago=24 [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/proxy-logs/cleanup {"days_to_keep":30} [Accept,Content-Type]",
          ]
        `);
    });

    it('saved log views', async () => {
        await client.listSavedLogViews('proxy_logs');
        await client.listSavedLogViews('admin_audit_logs');
        await client.createSavedLogView({ scope: 'proxy_logs', name: 'Errors', filters: { success: false }, is_shared: true });
        await client.updateSavedLogView('view/1', { name: 'Failures' });
        await client.updateSavedLogView('view/1', { is_shared: false });
        await client.updateSavedLogView('view/1', { filters: { success: false } });
        await client.deleteSavedLogView('view/1');
        expect(sent).toMatchInlineSnapshot(`
          [
            "GET http://cp.test/v1/admin/saved-log-views?scope=proxy_logs [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/saved-log-views?scope=admin_audit_logs [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/saved-log-views {"scope":"proxy_logs","name":"Errors","filters":{"success":false},"is_shared":true} [Accept,Content-Type]",
            "PATCH http://cp.test/v1/admin/saved-log-views/view%2F1 {"name":"Failures"} [Accept,Content-Type]",
            "PATCH http://cp.test/v1/admin/saved-log-views/view%2F1 {"is_shared":false} [Accept,Content-Type]",
            "PATCH http://cp.test/v1/admin/saved-log-views/view%2F1 {"filters":{"success":false}} [Accept,Content-Type]",
            "DELETE http://cp.test/v1/admin/saved-log-views/view%2F1 [Accept,Content-Type]",
          ]
        `);
    });

    it('exports, on the client and through the hook the viewers call', async () => {
        await client.exportProxyLogs({ hours_ago: 24, proxy_types: ['mcp', 'llm'], success: false }, 'jsonl');
        await client.exportProxyLogs({ hours_ago: 1 });
        await client.exportAuditLogs({ admin_ids: ['bob', 'eve'], request_methods: ['POST'], success: true, hours_ago: 6, q: 'tenant' }, 'jsonl');
        await client.exportAuditLogs();

        const { result } = renderHook(() => useProxyLogsGateway({ addError, gatewayClient: client }));
        await result.current.exportProxyLogs({ hours_ago: 24, server_ids: ['srv-a', 'srv-b'], q: 'timeout' }, 'csv');
        await result.current.exportAuditLogs({ actions: ['login'], resource_types: ['tenant'], ip_addresses: ['10.0.0.1'], start_time: '2026-11-01T00:00:00Z', end_time: '2026-11-02T00:00:00Z' }, 'csv');
        await result.current.fetchProxyStats({ server_id: 'srv-a' });
        await result.current.fetchProxyLogs({ limit: 20, offset: 0 });
        expect(sent).toMatchInlineSnapshot(`
          [
            "POST http://cp.test/v1/admin/proxy-logs/export?format=jsonl {"sort_by":"timestamp","sort_order":"desc","hours_ago":24,"proxy_types":["mcp","llm"],"success":false} [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/proxy-logs/export?format=csv {"sort_by":"timestamp","sort_order":"desc","hours_ago":1} [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/users/audit-logs/export?format=jsonl&admin_ids=bob&admin_ids=eve&request_methods=POST&success=true&hours_ago=6&q=tenant [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/users/audit-logs/export?format=csv [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/proxy-logs/export?format=csv {"sort_by":"timestamp","sort_order":"desc","hours_ago":24,"server_ids":["srv-a","srv-b"],"q":"timeout"} [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/users/audit-logs/export?format=csv&actions=login&resource_types=tenant&ip_addresses=10.0.0.1&start_time=2026-11-01T00%3A00%3A00Z&end_time=2026-11-02T00%3A00%3A00Z [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/proxy-logs/statistics?hours_ago=24 [Accept,Content-Type]",
            "POST http://cp.test/v1/admin/proxy-logs/query {"limit":20,"offset":0,"sort_by":"timestamp","sort_order":"desc"} [Accept,Content-Type]",
          ]
        `);
    });

    it('system alerts, on the client and through the hook Mission Control calls', async () => {
        await client.fetchSystemAlerts({ limit: 100 });
        await client.fetchSystemAlerts({ status: 'active', severity: 'critical', category: 'internal_error' });
        await client.fetchSystemAlerts();

        const { result } = renderHook(() => useAlertsGateway({ addError, gatewayClient: client }));
        await result.current.fetchSystemAlerts({ limit: 100 });
        await result.current.fetchSystemAlerts();
        expect(sent).toMatchInlineSnapshot(`
          [
            "GET http://cp.test/v1/admin/system-alerts?limit=100 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/system-alerts?status=active&severity=critical&category=internal_error [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/system-alerts [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/system-alerts?limit=100 [Accept,Content-Type]",
            "GET http://cp.test/v1/admin/system-alerts [Accept,Content-Type]",
          ]
        `);
    });

    it('evidence tree heads, addressed with the tenant id the log list answered with', async () => {
        await client.listEvidenceLogTreeHeads('log-a', { tenantId: 'acme', limit: 20 });
        expect(sent).toMatchInlineSnapshot(`
          [
            "GET http://cp.test/v1/admin/evidence/logs/log-a/tree-heads?tenant_id=acme&limit=20 [Accept,Content-Type]",
          ]
        `);
    });

    it('the administrator\'s own sessions and password, and the words the lifecycle gate knows', async () => {
        await client.listTenantAdminSessions();
        await client.listTenantAdminSessions({ include_inactive: true });
        await client.revokeTenantAdminSession('sess/1');
        await client.changeOwnPassword({ current_password: 'old', new_password: 'new' });
        await client.getGatePolicyVocabulary();
        expect(sent).toEqual([
            'GET http://cp.test/v1/admin/users/tenant/sessions [Accept,Content-Type]',
            'GET http://cp.test/v1/admin/users/tenant/sessions?include_inactive=true [Accept,Content-Type]',
            'POST http://cp.test/v1/admin/users/tenant/sessions/sess%2F1/revoke [Accept,Content-Type]',
            'POST http://cp.test/v1/admin/users/me/change-password {"current_password":"old","new_password":"new"} [Accept,Content-Type]',
            'GET http://cp.test/v1/admin/ai-systems/gate-policy/vocabulary [Accept,Content-Type]',
        ]);
    });

    it('the ISO 42001 coverage methods are gone: no page, hook or test called them', () => {
        // Recorded before the removal, as sent with no argument:
        //   GET /v1/admin/iso42001/coverage
        //   PUT /v1/admin/iso42001/coverage/A.5.2 {"status":"implemented","owner":"ciso"}
        //   GET /v1/admin/iso42001/evidence-pack
        // ISO 42001 is shown by the Obligations board (`fetchObligationsBoard`).
        for (const name of ['getISO42001Coverage', 'updateISO42001Control', 'getISO42001EvidencePack']) {
            expect(Reflect.get(client, name)).toBeUndefined();
        }
    });
});

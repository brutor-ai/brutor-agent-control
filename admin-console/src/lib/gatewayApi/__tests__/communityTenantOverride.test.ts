/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A Community client addresses no other tenant.
 *
 * The session token says which tenant a request is for. Naming a tenant on
 * top of it (`?tenant_id=`, `?tenant_scope=`, an `X-Tenant-ID` header) is how
 * a system administrator reaches across tenants: an Enterprise capability,
 * which a client composed of the Community modules alone must not have.
 *
 * Every method of every Community module is called here on such a client,
 * with arguments made to light up an override if the method has one: in each
 * of the first positions either a marker string (a positional `tenantId`) or
 * an object answering `tenant_id`, `tenantId` and `tenant_scope` with the
 * marker (a `params` object), in every combination. Whatever the method then
 * hands to `fetch` or `WebSocket` is inspected, and so is a URL or a query
 * string it returns for a page to use. The marker in a path segment
 * or a body is not an override and is not looked for: a body field is data
 * the server validates against the session.
 *
 * Some methods copy every key of a filter object into the query string. The
 * probe makes those send `tenant_id` too, although their declared filter
 * types have no such key and a typed caller cannot pass one (`npm run
 * type-check`). They are listed under `FORWARDS_ITS_FILTER`, and the declared
 * types are read from the source to hold that no tenant key is declared.
 *
 * A method that sends nothing whatever it is given proves nothing, so those
 * are listed by name too; a new method that cannot be exercised fails until
 * it is looked at.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('reactflow/dist/style.css', () => ({}));

import { CommunityApiClient } from '../../../test/communityComposition';
import { GatewayApiBase } from '../base';
import { BUILTIN_API_MODULES, installApiModules } from '../modules';

const MARK = 'tenant-probe';
const POSITIONS = 4;
const ARGS = 6;

type Override = 'query tenant_id' | 'query tenant_scope' | 'header X-Tenant-ID';

/** An object argument: every way a `params` object could name a tenant. */
function probeObject(): Record<string, unknown> {
    return { tenant_id: MARK, tenantId: MARK, tenant_scope: 'all' };
}

/** Every mix of marker strings and probe objects over the first positions. */
function argumentLists(): unknown[][] {
    const lists: unknown[][] = [];
    for (let mask = 0; mask < 2 ** POSITIONS; mask++) {
        lists.push(
            Array.from({ length: ARGS }, (_, i) => (i < POSITIONS && mask & (1 << i) ? probeObject() : MARK)),
        );
    }
    return lists;
}

function headerNames(headers: HeadersInit | undefined): string[] {
    if (!headers) return [];
    if (headers instanceof Headers) return [...headers.keys()];
    if (Array.isArray(headers)) return headers.map(([name]) => name);
    return Object.keys(headers);
}

/** The overrides one outgoing request carries. */
function overridesOf(url: string, headers?: HeadersInit): Override[] {
    const found: Override[] = [];
    const query = url.includes('?') ? url.slice(url.indexOf('?') + 1) : '';
    const keys = [...new URLSearchParams(query).keys()].map((key) => key.toLowerCase());
    if (keys.includes('tenant_id') || keys.includes('tenantid')) found.push('query tenant_id');
    if (keys.includes('tenant_scope')) found.push('query tenant_scope');
    if (headerNames(headers).some((name) => name.toLowerCase() === 'x-tenant-id')) found.push('header X-Tenant-ID');
    return found;
}

interface Wire {
    requests: number;
    overrides: Set<Override>;
}

let wire: Wire;

beforeEach(() => {
    wire = { requests: 0, overrides: new Set() };
    const seen = (url: unknown, headers?: HeadersInit) => {
        wire.requests += 1;
        for (const override of overridesOf(String(url), headers)) wire.overrides.add(override);
    };
    vi.stubGlobal(
        'fetch',
        vi.fn((url: unknown, init?: RequestInit) => {
            seen(url, init?.headers);
            return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }));
        }),
    );
    vi.stubGlobal(
        'WebSocket',
        class {
            constructor(url: string) {
                seen(url);
            }
            close() {}
        },
    );
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

function methodNames(mixin: { prototype: object }): string[] {
    return Object.getOwnPropertyNames(mixin.prototype).filter((name) => name !== 'constructor');
}

/** A method that outlives this is waiting on something (a poll); what it sent by then is what counts. */
const SETTLE_MS = 250;

interface Finding {
    overrides: Override[];
    sent: boolean;
}

/** A session token that has not expired: some methods send nothing without one. */
const TOKEN = `h.${btoa(JSON.stringify({ exp: 4_000_000_000 }))}.s`;

/** Call `client[name]` with every argument list; report what went out. */
async function exercise(client: GatewayApiBase, name: string): Promise<Finding> {
    const overrides = new Set<Override>();
    let sent = false;
    for (const list of argumentLists()) {
        wire.requests = 0;
        wire.overrides = new Set();
        // A method under test may have signed the client out.
        client.setAuthToken(TOKEN);
        try {
            const method: unknown = Reflect.get(client, name);
            if (typeof method !== 'function') throw new Error(`${name} is not a method`);
            const outcome: unknown = Reflect.apply(method, client, list);
            // A URL or a query string built for the page to use is a request too.
            if (typeof outcome === 'string' || outcome instanceof URLSearchParams) {
                wire.requests += 1;
                const built = typeof outcome === 'string' ? outcome : `?${outcome.toString()}`;
                for (const override of overridesOf(built)) wire.overrides.add(override);
            }
            await Promise.race([
                Promise.resolve(outcome).catch(() => undefined),
                new Promise((resolve) => setTimeout(resolve, SETTLE_MS)),
            ]);
        } catch {
            // A method may refuse the probe arguments; what it sent before is still inspected.
        }
        if (wire.requests > 0) sent = true;
        for (const override of wire.overrides) overrides.add(override);
    }
    return { overrides: [...overrides].sort(), sent };
}

const COMMUNITY_MODULES = BUILTIN_API_MODULES.filter((mod) => mod.feature === null);

/**
 * Community methods that do name a tenant, and why each may. Remove a row
 * when the override is gone; do not add one without a reason.
 */
const EXCEPTIONS: Record<string, { overrides: Override[]; reason: string }> = {
    'evidence.listEvidenceLogTreeHeads': {
        overrides: ['query tenant_id'],
        reason:
            'System → Audit Chains passes back the tenant_id the server put on the log row ' +
            '(listEvidenceLogs); the page never chooses a tenant. A tenant administrator sends its own.',
    },
};

const LIB = path.resolve(__dirname, '../..');

/** The text of `file` from `from` up to the next `to`: a signature or an interface. */
function declared(file: string, from: string, to: string): string {
    const text = readFileSync(path.join(LIB, file), 'utf8');
    const start = text.indexOf(from);
    const end = text.indexOf(to, start + from.length);
    if (start < 0 || end < 0) throw new Error(`${file}: no "${from}" … "${to}"`);
    return text.slice(start, end);
}

const signature = (module: string, method: string) => declared(`gatewayApi/${module}.ts`, `async ${method}(`, '): Promise');
const filterType = (name: string) => declared('complianceEvidenceTypes.ts', `export interface ${name} {`, '\n}');

const namesTenant = (declaration: string) => /tenant/i.test(declaration);

/**
 * Community methods that copy every key of their filter object into the query
 * string, with where the keys they may be given are declared. None declares a
 * tenant key, so a typed caller cannot send one; an untyped object would get
 * through.
 */
const FORWARDS_ITS_FILTER: Record<string, () => string> = {
    // The private helper the seven compliance methods below build their query with.
    'compliance.complianceQuery': () => declared('gatewayApi/compliance.ts', 'private complianceQuery(', '): string'),
    'compliance.getComplianceOverview': () => signature('compliance', 'getComplianceOverview'),
    'compliance.getComplianceObligations': () => signature('compliance', 'getComplianceObligations'),
    'compliance.getComplianceObligationDetail': () => signature('compliance', 'getComplianceObligationDetail'),
    'compliance.getComplianceOversight': () => signature('compliance', 'getComplianceOversight'),
    'compliance.getComplianceTransparency': () => signature('compliance', 'getComplianceTransparency'),
    'compliance.listComplianceReports': () => signature('compliance', 'listComplianceReports'),
    'compliance.listComplianceIncidents': () => signature('compliance', 'listComplianceIncidents'),
    'evidence.listEvidenceRecords': () => signature('evidence', 'listEvidenceRecords') + filterType('EvidenceRecordFilters'),
    'evidence.getEvidenceSummary': () => signature('evidence', 'getEvidenceSummary') + filterType('EvidenceSummaryFilters'),
    'llmModels.quickSearchLLMs': () => signature('llmModels', 'quickSearchLLMs'),
    'resourceGroups.searchActions': () => signature('resourceGroups', 'searchActions'),
};

/**
 * Community methods that send and build nothing whatever they are given, so
 * the probe says nothing about them.
 */
const SENDS_NOTHING: string[] = [];

describe('the probe', () => {
    class LeakyApi extends GatewayApiBase {
        async positional(id: string, tenantId?: string) {
            return this.request<unknown>(`/v1/x/${id}${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''}`);
        }
        async third(id: string, body: object, tenantId?: string) {
            const qs = new URLSearchParams();
            if (tenantId) qs.append('tenant_id', tenantId);
            return this.request<unknown>(`/v1/x/${id}?${qs.toString()}`, { method: 'POST', body: JSON.stringify(body) });
        }
        async inParams(params?: { tenant_id?: string; limit?: number }) {
            const qs = new URLSearchParams();
            if (params?.tenant_id) qs.set('tenant_id', params.tenant_id);
            return this.request<unknown>(`/v1/y?${qs.toString()}`);
        }
        async scope(params?: { tenant_scope?: string }) {
            return this.request<unknown>(`/v1/z${params?.tenant_scope ? `?tenant_scope=${params.tenant_scope}` : ''}`);
        }
        async header(id: string) {
            return this.request<unknown>(`/v1/h/${id}`, { headers: { 'x-tenant-id': id } });
        }
        async clean(id: string, body: { tenant_id?: string }) {
            return this.request<unknown>(`/v1/tenants/${id}/thing?limit=5`, { method: 'POST', body: JSON.stringify(body) });
        }
        link(id: string, tenantId?: string) {
            return `/v1/l/${id}?v=1${tenantId ? `&tenant_id=${tenantId}` : ''}`;
        }
        query(params: { tenant_id?: string }) {
            return new URLSearchParams(params.tenant_id ? { tenant_id: params.tenant_id } : {});
        }
        count(id: string) {
            return id.length;
        }
    }
    class LeakyClient extends GatewayApiBase {}
    installApiModules(LeakyClient, [{ name: 'leaky', feature: null, mixin: LeakyApi }]);

    it.each([
        ['positional', ['query tenant_id'], true],
        ['third', ['query tenant_id'], true],
        ['inParams', ['query tenant_id'], true],
        ['scope', ['query tenant_scope'], true],
        ['header', ['header X-Tenant-ID'], true],
        ['clean', [], true],
        ['link', ['query tenant_id'], true],
        ['query', ['query tenant_id'], true],
        ['count', [], false],
    ])('sees what %s sends: %j, sent=%s', async (name, overrides, sent) => {
        const client = new LeakyClient('http://cp.test', () => {});
        expect(await exercise(client, name)).toEqual({ overrides, sent });
    });

    it('reads a query key and a header name, not a path or a value', () => {
        expect(overridesOf('http://cp.test/v1/a?limit=1&tenant_id=x')).toEqual(['query tenant_id']);
        expect(overridesOf('http://cp.test/v1/a?TENANT_ID=x')).toEqual(['query tenant_id']);
        expect(overridesOf('ws://cp.test/v1/a?tenant_scope=all')).toEqual(['query tenant_scope']);
        expect(overridesOf('http://cp.test/v1/a', { 'X-Tenant-ID': 'x' })).toEqual(['header X-Tenant-ID']);
        expect(overridesOf('http://cp.test/v1/a', new Headers({ 'x-tenant-id': 'x' }))).toEqual(['header X-Tenant-ID']);
        expect(overridesOf('http://cp.test/v1/a', [['X-Tenant-Id', 'x']])).toEqual(['header X-Tenant-ID']);
        expect(overridesOf('http://cp.test/v1/tenants/tenant_id/x?q=tenant_id', { Accept: 'x-tenant-id' })).toEqual([]);
    });
});

describe('a client of the Community modules', () => {
    it('has every Community module and no Enterprise one', () => {
        expect(COMMUNITY_MODULES.length).toBeGreaterThanOrEqual(29);
        const client: object = new CommunityApiClient('http://cp.test', () => {});
        expect(Reflect.get(client, 'fetchSystemAlerts')).toBeTypeOf('function');
        // multiTenant is Enterprise.
        expect(Reflect.get(client, 'fetchTenants')).toBeUndefined();
        expect(COMMUNITY_MODULES.flatMap((mod) => methodNames(mod.mixin)).length).toBeGreaterThan(400);
    });

    it(
        'names a tenant in no query string and no header, but for the listed exceptions',
        async () => {
            const client = new CommunityApiClient('http://cp.test', () => {});
            const naming: Record<string, Override[]> = {};
            const silent: string[] = [];
            for (const mod of COMMUNITY_MODULES) {
                for (const name of methodNames(mod.mixin)) {
                    const { overrides, sent } = await exercise(client, name);
                    if (overrides.length) naming[`${mod.name}.${name}`] = overrides;
                    if (!sent) silent.push(`${mod.name}.${name}`);
                }
            }
            const allowed = Object.fromEntries(
                Object.entries(EXCEPTIONS).map(([name, { overrides }]) => [name, overrides]),
            );
            const forwarding = Object.fromEntries(
                Object.keys(FORWARDS_ITS_FILTER).map((name): [string, Override[]] => [
                    name,
                    ['query tenant_id', 'query tenant_scope'],
                ]),
            );
            expect(naming).toEqual({ ...allowed, ...forwarding });
            expect(silent.sort()).toEqual([...SENDS_NOTHING].sort());
        },
        120_000,
    );

    it.each(Object.keys(FORWARDS_ITS_FILTER))('%s declares no tenant key in its filter', (name) => {
        const declaration = FORWARDS_ITS_FILTER[name]();
        expect(declaration.length).toBeGreaterThan(40);
        expect(namesTenant(declaration)).toBe(false);
    });

    it('a declared tenant key is seen, in a signature and in a filter type', () => {
        expect(namesTenant('async list(params: { limit?: number; tenant_id?: string } = {}')).toBe(true);
        expect(namesTenant('export interface Filters {\n    tenantId?: string;')).toBe(true);
        expect(namesTenant('async list(params: { limit?: number } = {}')).toBe(false);
        // The filter types are found where the evidence methods name them.
        expect(signature('evidence', 'listEvidenceRecords')).toContain('EvidenceRecordFilters');
        expect(signature('evidence', 'getEvidenceSummary')).toContain('EvidenceSummaryFilters');
        expect(filterType('EvidenceRecordFilters')).toContain('ai_system_id');
    });

    it('no method is listed twice', () => {
        expect(Object.keys(FORWARDS_ITS_FILTER).filter((name) => name in EXCEPTIONS)).toEqual([]);
    });

    it('every exception says why', () => {
        for (const { reason } of Object.values(EXCEPTIONS)) expect(reason.length).toBeGreaterThan(40);
    });
});

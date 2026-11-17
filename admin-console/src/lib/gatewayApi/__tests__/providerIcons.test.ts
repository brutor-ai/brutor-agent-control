/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The provider-icon methods and their module.
 *
 * `/v1/admin/providers` is Community, all of it, including the provider-icon
 * endpoints, and `resolveAssetUrl` is what the Asset Register
 * draws every icon with. The five methods here belong to Community modules,
 * not to the Enterprise `trafficImport` module: a client composed without
 * Enterprise modules must have them.
 *
 * The first block holds, for each method, the exact request it sends and
 * what it gives back, on the console's client and on a client composed from
 * the Community modules alone. The last block is where the methods live.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import { GatewayApiClient } from '../../gatewayApiClient';
import { GatewayApiBase } from '../base';
import { BUILTIN_API_MODULES, installApiModules } from '../modules';

// The client here is the Community console's: the built-in modules. That an
// Enterprise module has none of these methods is held beside that module
// (`enterprise/src/host/lib/gatewayApi/__tests__/providerIcons.test.ts`).
/** The methods, and the Community module each belongs to. */
const PROVIDER_METHODS: Record<string, string> = {
    uploadProviderIcon: 'llmProviders',
    deleteProviderIcon: 'llmProviders',
    getProviderIconUrl: 'llmProviders',
    renameProvider: 'llmProviders',
    resolveAssetUrl: 'aiAssets',
};

type ProviderMethods = Pick<
    GatewayApiClient,
    'uploadProviderIcon' | 'deleteProviderIcon' | 'getProviderIconUrl' | 'renameProvider' | 'resolveAssetUrl'
>;

class CommunityOnlyClient extends GatewayApiBase {}
installApiModules(
    CommunityOnlyClient,
    BUILTIN_API_MODULES.filter((m) => m.feature === null),
);

function jwt(): string {
    const part = (value: object) =>
        btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ sub: 'admin-1', exp: Math.floor(Date.now() / 1000) + 3600 })}.sig`;
}

const TOKEN = jwt();

const CLIENTS: Array<[string, () => GatewayApiBase & Partial<ProviderMethods>]> = [
    ['the client with every module', () => new GatewayApiClient('http://cp.test/')],
    // Typed as not having them, so that the block says so when it does not.
    ['a client of the Community modules alone', () => new CommunityOnlyClient('http://cp.test/')],
];

interface Sent {
    url: string;
    method: string | undefined;
    headers: unknown;
    body: unknown;
}

function answerWith(response: Response): { sent: Sent[] } {
    const sent: Sent[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        sent.push({ url, method: init?.method, headers: init?.headers, body: init?.body });
        return Promise.resolve(response);
    });
    return { sent };
}

/** The method, or a failure that names it: the client does not have it. */
function method<K extends keyof ProviderMethods>(client: Partial<ProviderMethods>, name: K): ProviderMethods[K] {
    const fn = client[name];
    if (typeof fn !== 'function') throw new Error(`the client has no ${name}`);
    return fn.bind(client) as ProviderMethods[K];
}

describe.each(CLIENTS)('the provider methods on %s', (_name, build) => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    const signedIn = () => {
        const client = build();
        client.setAuthToken(TOKEN);
        return client;
    };

    it('uploadProviderIcon POSTs the file as multipart to the provider’s icon and returns the provider', async () => {
        const { sent } = answerWith(
            new Response(JSON.stringify({ id: 'prov 1/x', key: 'acme', has_icon: true }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );
        const file = new File(['<svg/>'], 'logo.svg', { type: 'image/svg+xml' });

        const answer = await method(signedIn(), 'uploadProviderIcon')('prov 1/x', file);

        expect(answer).toEqual({ id: 'prov 1/x', key: 'acme', has_icon: true });
        expect(sent).toHaveLength(1);
        expect(sent[0].url).toBe('http://cp.test/v1/admin/providers/prov%201%2Fx/icon');
        expect(sent[0].method).toBe('POST');
        // No Content-Type: the browser sets it, with the multipart boundary.
        expect(sent[0].headers).toEqual({ Accept: 'application/json', Authorization: `Bearer ${TOKEN}` });
        const body = sent[0].body;
        expect(body).toBeInstanceOf(FormData);
        const entries = body instanceof FormData ? [...body.entries()] : [];
        expect(entries.map(([key]) => key)).toEqual(['file']);
        const uploaded = entries[0]?.[1];
        expect(uploaded).toBeInstanceOf(File);
        expect(uploaded instanceof File ? [uploaded.name, uploaded.type, await uploaded.text()] : null).toEqual([
            'logo.svg',
            'image/svg+xml',
            '<svg/>',
        ]);
    });

    it('deleteProviderIcon DELETEs the provider’s icon and returns nothing', async () => {
        const { sent } = answerWith(new Response(null, { status: 204 }));

        const answer = await method(signedIn(), 'deleteProviderIcon')('prov 1/x');

        expect(answer).toBeUndefined();
        expect(sent).toEqual([
            {
                url: 'http://cp.test/v1/admin/providers/prov%201%2Fx/icon',
                method: 'DELETE',
                headers: {
                    Accept: 'application/json',
                    Authorization: `Bearer ${TOKEN}`,
                    'Content-Type': 'application/json',
                },
                body: undefined,
            },
        ]);
    });

    it('renameProvider POSTs the new key as a query parameter and returns the provider', async () => {
        const { sent } = answerWith(
            new Response(JSON.stringify({ id: 'prov 1/x', key: 'new key&1' }), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
            }),
        );

        const answer = await method(signedIn(), 'renameProvider')('prov 1/x', 'new key&1');

        expect(answer).toEqual({ id: 'prov 1/x', key: 'new key&1' });
        expect(sent).toEqual([
            {
                url: 'http://cp.test/v1/admin/providers/prov%201%2Fx/rename?new_key=new+key%261',
                method: 'POST',
                headers: {
                    Accept: 'application/json',
                    Authorization: `Bearer ${TOKEN}`,
                    'Content-Type': 'application/json',
                },
                body: undefined,
            },
        ]);
    });

    it('getProviderIconUrl is the icon endpoint on the Control Plane, with the upload time as cache buster; no request', () => {
        const { sent } = answerWith(new Response(null, { status: 204 }));
        const url = method(build(), 'getProviderIconUrl');

        expect(url('prov 1/x')).toBe('http://cp.test/v1/admin/providers/prov%201%2Fx/icon');
        expect(url('p1', null)).toBe('http://cp.test/v1/admin/providers/p1/icon');
        expect(url('p1', '')).toBe('http://cp.test/v1/admin/providers/p1/icon');
        expect(url('p1', '2026-11-09T10:00:00+00:00')).toBe(
            'http://cp.test/v1/admin/providers/p1/icon?v=2026-11-09T10%3A00%3A00%2B00%3A00',
        );
        expect(sent).toEqual([]);
    });

    it('resolveAssetUrl passes absolute and data URLs, puts the Control Plane before a path, leaves the rest; no request', () => {
        const { sent } = answerWith(new Response(null, { status: 204 }));
        const resolveUrl = method(build(), 'resolveAssetUrl');

        expect(
            [
                'https://cdn.test/a.png',
                'http://cdn.test/a.png',
                'HTTPS://CDN.test/a.png',
                'data:image/png;base64,AAAA',
                '/v1/admin/mcp-servers/s1/icon',
                '//cdn.test/a.png',
                'lucide:box',
                'icons/a.png',
                '',
            ].map((ref) => resolveUrl(ref)),
        ).toEqual([
            'https://cdn.test/a.png',
            'http://cdn.test/a.png',
            'HTTPS://CDN.test/a.png',
            'data:image/png;base64,AAAA',
            'http://cp.test/v1/admin/mcp-servers/s1/icon',
            'http://cp.test//cdn.test/a.png',
            'lucide:box',
            'icons/a.png',
            '',
        ]);
        expect(sent).toEqual([]);
    });
});

describe('where the provider methods live', () => {
    const declaredBy = (name: string): string[] =>
        BUILTIN_API_MODULES.filter((m) => Object.getOwnPropertyNames(m.mixin.prototype).includes(name)).map((m) => m.name);

    it.each(Object.entries(PROVIDER_METHODS))('%s is declared by the Community module %s, and by no other', (name, module) => {
        expect(declaredBy(name)).toEqual([module]);
        expect(BUILTIN_API_MODULES.find((m) => m.name === module)?.feature).toBeNull();
    });

    it('a client of the Community modules alone answers all of them', () => {
        const missing = Object.keys(PROVIDER_METHODS).filter((name) => !(name in CommunityOnlyClient.prototype));
        expect(missing).toEqual([]);
    });
});

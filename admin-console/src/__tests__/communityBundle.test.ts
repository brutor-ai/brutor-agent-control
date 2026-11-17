/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The scan of a Community build for pieces of Enterprise features
 * (`scripts/scan-community-bundle.mjs`).
 *
 * The first block holds the scan itself to what it says it finds. The last
 * case runs it over the build in `dist/` when there is one: `npm run build`
 * writes the Community build there (the Enterprise build goes to
 * `enterprise/dist`). With no build the case is skipped; the image build
 * and `npm run scan:bundle` run the same scan and do not skip.
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { looksObfuscated, readDenylist, scanBuild, scanText } from '../../scripts/scan-community-bundle.mjs';
import { REPO_ROOT } from '../test/sourceRoots';

interface Denylist {
    routePrefixes: string[];
    /** Enterprise routes under a prefix both editions have routes under. */
    sharedPrefixRoutes?: string[];
    navIds: string[];
    sharedNavIds: Record<string, { label: string; why: string }>;
}

const DENYLIST = readDenylist() as Denylist;
const found = (text: string, denylist: Denylist = DENYLIST): string[] =>
    (scanText(text, denylist) as { kind: string; what: string }[]).map((finding) => `${finding.kind} ${finding.what}`);

const SAMPLE: Denylist = {
    routePrefixes: ['/v1/admin/deploy', '/v1/admin/kb'],
    navIds: ['kb-collections', 'policies'],
    sharedNavIds: { policies: { label: 'Policy as Code', why: 'a sub-tab of Community forms' } },
};

describe('the Community bundle scan', () => {
    it('finds a route under an Enterprise prefix however it is written, and not a longer path segment', () => {
        expect(found('fetch("/v1/admin/deploy")', SAMPLE)).toEqual(['route /v1/admin/deploy']);
        expect(found('this.request(`/v1/admin/deploy/${id}/start`)', SAMPLE)).toEqual(['route /v1/admin/deploy']);
        expect(found("a='/v1/admin/kb?x=1',b='/v1/admin/deploy'", SAMPLE)).toEqual(['route /v1/admin/kb', 'route /v1/admin/deploy']);
        // Another route: the segment goes on.
        expect(found('"/v1/admin/deployments" "/v1/admin/deploy-keys" "/v1/admin/kb_x" "/v1/admin/kbs"', SAMPLE)).toEqual([]);
        expect(found('"/v2/admin/deploy" "/v1/admin/depl"', SAMPLE)).toEqual([]);
    });

    it('finds an Enterprise route under a prefix the Community console also calls, and not the Community routes beside it', () => {
        const shared: Denylist = { ...SAMPLE, sharedPrefixRoutes: ['/v1/admin/users/system'] };
        expect(found('this.request(`/v1/admin/users/system/${encodeURIComponent(id)}/unlock`,{method:"POST"})', shared)).toEqual([
            'route /v1/admin/users/system',
        ]);
        expect(found('"/v1/admin/users/system/login"', shared)).toEqual(['route /v1/admin/users/system']);
        // The Community console's own routes under the prefix.
        expect(
            found('`/v1/admin/users/tenant/${id}/unlock` "/v1/admin/users/tenant/login" "/v1/admin/users/me" "/v1/admin/users/systems"', shared),
        ).toEqual([]);
        // A list without the key is a list with none.
        expect(found('"/v1/admin/users/system/login"', SAMPLE)).toEqual([]);
    });

    it('lists the system administrator routes, and no route the Community control plane serves', () => {
        const listed = DENYLIST.sharedPrefixRoutes ?? [];
        expect(listed).toContain('/v1/admin/users/system');
        expect(listed.filter((route) => !/^\/[A-Za-z0-9/_-]+[A-Za-z0-9]$/.test(route))).toEqual([]);
        // Not a whole Enterprise prefix (those are `routePrefixes`), and distinct.
        expect(listed.filter((route) => DENYLIST.routePrefixes.includes(route))).toEqual([]);
        expect(new Set(listed).size).toBe(listed.length);
        const community = JSON.parse(
            readFileSync(path.join(REPO_ROOT, 'src/lib/__tests__/fixtures/control-plane-routes.json'), 'utf8'),
        ) as string[];
        const served = community
            .map((operation) => operation.split(' ')[1])
            .filter((route) => listed.some((entry) => route === entry || route.startsWith(`${entry}/`)));
        expect(served).toEqual([]);
        // The tenant administrator's unlock is the Community console's.
        expect(community).toContain('POST /v1/admin/users/tenant/{admin_id}/unlock');
        expect(found('request(`/v1/admin/users/tenant/${id}/unlock`)')).toEqual([]);
        expect(found('request(`/v1/admin/users/system/${id}/unlock`)')).toEqual(['route /v1/admin/users/system']);
    });

    it('lists the connector catalog icons as Enterprise, and no prefix the Community control plane serves a route under', () => {
        // The icons a knowledge-base connector catalog entry names as its
        // `icon_url`: the Enterprise control plane serves them, the
        // Community one does not, and only Enterprise screens draw them.
        expect(DENYLIST.routePrefixes).toContain('/v1/assets/connector-icons');
        expect(found('<img src="/v1/assets/connector-icons/notion.svg">')).toEqual(['route /v1/assets/connector-icons']);
        // The other assets are the Community console's.
        expect(found('"/v1/assets/logo.svg" "/v1/assets/connector-icons-old/x.svg"')).toEqual([]);
        const community = JSON.parse(
            readFileSync(path.join(REPO_ROOT, 'src/lib/__tests__/fixtures/control-plane-routes.json'), 'utf8'),
        ) as string[];
        const served = community
            .map((operation) => operation.split(' ')[1])
            .filter((route) => DENYLIST.routePrefixes.some((prefix) => route === prefix || route.startsWith(`${prefix}/`)));
        expect(served).toEqual([]);
    });

    it('finds a navigation id as a whole string literal, in any quotes', () => {
        expect(found('{id:"kb-collections",label:"x"}', SAMPLE)).toEqual(['nav kb-collections']);
        expect(found("open('kb-collections');open(`kb-collections`)", SAMPLE)).toEqual(['nav kb-collections', 'nav kb-collections']);
        // Part of another string, or not a string.
        expect(found('"/effective-kb-collections" "kb-collections-2" kb-collections', SAMPLE)).toEqual([]);
    });

    it('finds an id that is an ordinary word only as the Enterprise entry or a tab id a screen claims', () => {
        // What the Community console has of its own.
        expect(found('{id:"policies",label:"Policies"} x==="policies" case"policies": r.policies', SAMPLE)).toEqual([]);
        // The Enterprise navigation entry, minified or as written.
        expect(found('{id:"policies",label:"Policy as Code",icon:Q}', SAMPLE)).toEqual(['nav policies']);
        expect(found("{ id: 'policies', label: 'Policy as Code', icon: FileText }", SAMPLE)).toEqual(['nav policies']);
        // The Enterprise screen.
        expect(found('{name:"PoliciesTab",ids:["policies"],render:x}', SAMPLE)).toEqual(['nav policies']);
        expect(found('{ids:["a","policies","b"]}', SAMPLE)).toEqual(['nav policies']);
    });

    it('reads a list that is whole: prefixes are paths, ids are distinct, a shared id says why and names its label', () => {
        // 19 since the control plane's renames: /v1/admin/deploy and /v1/admin/deployment are one prefix
        // (/v1/admin/deployments), /v1/admin/mcp-registry and /v1/admin/registries are one (/v1/admin/mcp-registries),
        // and the Portal's send and sent have a prefix of their own (/v1/admin/user-notifications).
        expect(DENYLIST.routePrefixes.length).toBeGreaterThanOrEqual(19);
        expect(DENYLIST.navIds.length).toBeGreaterThanOrEqual(16);
        expect(DENYLIST.routePrefixes.filter((prefix) => !/^\/[A-Za-z0-9/_-]+[A-Za-z0-9]$/.test(prefix))).toEqual([]);
        expect(new Set(DENYLIST.routePrefixes).size).toBe(DENYLIST.routePrefixes.length);
        expect(new Set(DENYLIST.navIds).size).toBe(DENYLIST.navIds.length);
        const shared = Object.entries(DENYLIST.sharedNavIds);
        expect(shared.filter(([id]) => !DENYLIST.navIds.includes(id)).map(([id]) => id)).toEqual([]);
        expect(shared.filter(([, row]) => !row.label || row.why.length < 40).map(([id]) => id)).toEqual([]);
        // Only ordinary words are shared: an id with a hyphen is nobody else's.
        expect(shared.filter(([id]) => id.includes('-')).map(([id]) => id)).toEqual([]);
    });

    it('finds everything on its list in a text that has it all', () => {
        const everything = [
            ...DENYLIST.routePrefixes.map((prefix) => `request('${prefix}/x')`),
            ...(DENYLIST.sharedPrefixRoutes ?? []).map((route) => `request('${route}/x')`),
            ...DENYLIST.navIds.map((id) =>
                id in DENYLIST.sharedNavIds ? `{id:"${id}",label:"${DENYLIST.sharedNavIds[id].label}"}` : `{id:"${id}"}`,
            ),
        ].join(';');
        expect([...new Set(found(everything))].sort()).toEqual(
            [
                ...DENYLIST.routePrefixes.map((prefix) => `route ${prefix}`),
                ...(DENYLIST.sharedPrefixRoutes ?? []).map((route) => `route ${route}`),
                ...DENYLIST.navIds.map((id) => `nav ${id}`),
            ].sort(),
        );
    });
});

describe('the Community build', () => {
    const dist = path.join(REPO_ROOT, 'dist');
    const built = existsSync(path.join(dist, 'index.html'));

    it.skipIf(!built)('when there is one in dist/, is readable and has no Enterprise route prefix or navigation id in it', () => {
        // An obfuscated build cannot be scanned, and is not a Community build.
        expect(looksObfuscated(dist)).toBe(false);
        const { scanned, findings } = scanBuild(dist, DENYLIST) as {
            scanned: number;
            findings: { kind: string; what: string; file: string; around: string }[];
        };
        expect(scanned).toBeGreaterThan(1);
        expect(findings.map((finding) => `${finding.kind} ${finding.what} in ${finding.file}: ${finding.around}`)).toEqual([]);
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Route contract: every endpoint the admin UI calls must exist on the
 * Control Plane.
 *
 * Why this exists
 * ---------------
 * `GatewayApiClient` has ~338 request methods. This file is the safety net
 * for any refactor of the client — including replacing its `Promise<any>`
 * return types with generated ones. Typing a response is an assertion about
 * what the server sends; this checks that the client and the Control Plane
 * agree on the URL at all.
 *
 * Without it, a renamed or removed Control Plane route fails at runtime as a
 * 404 in whichever admin screen happens to call it. This turns that into a
 * failing test.
 *
 * How it works
 * ------------
 * The route list is generated from the Control Plane's OpenAPI snapshot by
 * `scripts/sync-control-plane-routes.mjs`. It is vendored because this repo's
 * CI checks out only this repo. When the Control Plane IS present as a
 * sibling (the normal platform layout) the last test re-derives the list and
 * fails if the vendored copy has gone stale.
 *
 * One contract per edition
 * -----------------------------------------------
 * The Control Plane has two OpenAPI documents: the Community application's
 * and the Enterprise one's (the Community application plus the Enterprise
 * routers). There is a vendored list of each, and a package is held to the
 * list of its own edition (`src/test/apiContract.ts`):
 *
 *   - the Community package's run reads the Community modules of the client
 *     and `fixtures/control-plane-routes.json`, the COMMUNITY list. A
 *     Community module that calls a route only the Enterprise Control Plane
 *     serves fails here: a Community console would get a 404 from its server.
 *   - the Enterprise package runs this file again: the whole client (the
 *     Community modules and the Enterprise ones) against the ENTERPRISE list,
 *     which is under `enterprise/`. Its own `apiContract.test.ts` holds the
 *     two lists and the two halves of the client to each other.
 */

import { readFileSync } from 'node:fs';
import { resolve as resolvePath } from 'node:path';
import { describe, expect, it } from 'vitest';

import { apiContractUnderTest, enterpriseRoutePrefixes, isUnder, vendoredPaths, vendoredRoutes as readVendoredRoutes } from '../../test/apiContract';
import { apiClientFiles, enterpriseUnderTest, sourceRoots } from '../../test/sourceRoots';
import { documentForTest } from '../../../scripts/control-plane-documents.mjs';
import { clientCalls, describeCall, directCalls, directCallsIn, servedBy, sourcesOutside } from './helpers/routeContract';

// The client is the facade plus every module in src/lib/gatewayApi; the request calls live in the modules and the base. The module of an
// Enterprise feature is in that feature's directory under enterprise/,
// and is read from there in the Enterprise package's run
// (`apiClientFiles`). The list the calls are held to is the one of the same
// edition.
const CLIENT_SOURCES = apiClientFiles();
const CONTRACT = apiContractUnderTest();
const vendoredRoutes = readVendoredRoutes(CONTRACT);

const { calls: routes, unresolved } = clientCalls(CLIENT_SOURCES);

const isServed = servedBy(vendoredRoutes);

/**
 * Calls that do NOT resolve to a Control Plane route today.
 *
 * The set is EMPTY, and that is the contract: every request the client can
 * issue is served. An entry is either a wrong path on a live button or a
 * client method nothing calls. Dead surface is not free:
 * it reads as capability, and the next caller to reach for it gets a 404.
 *
 * Adding an entry here needs a very good reason. Fix the path, or delete the
 * method, in preference to pinning it.
 */
const KNOWN_MISSING = new Set<string>([]);

describe('GatewayApiClient route contract', () => {
  it('extracts a meaningful number of call sites', () => {
    // Guards the extractor itself. If a refactor changes how requests are
    // issued, this fails loudly rather than letting the contract test below
    // pass vacuously against an empty set.
    expect(routes.length).toBeGreaterThan(400);
  });

  it('reads every module of the client, wherever its file is', () => {
    // The facade, the base, the registry, and one file per module: 30
    // Community modules, 41 with the Enterprise ones.
    const names = CLIENT_SOURCES.map((file) => file.split('/').pop());
    expect(new Set(names).size).toBe(names.length);
    expect(names.length - 3).toBe(enterpriseUnderTest() ? 41 : 30);
  });

  it('calls only endpoints the Control Plane exposes', () => {
    const missing = routes
      .filter((r) => !isServed(r.method, r.path))
      .filter((r) => !KNOWN_MISSING.has(`${r.method} ${r.path}`))
      .map(describeCall);

    // Sorted + de-duplicated so the failure output is a readable list rather
    // than one line per call site.
    expect([...new Set(missing)].sort()).toEqual([]);
  });

  it('has no stale entries in KNOWN_MISSING', () => {
    // Keeps the allowlist honest: once a route is fixed on either side, its
    // entry has to go, so the list can only shrink.
    const stillBroken = new Set(
      routes
        .filter((r) => !isServed(r.method, r.path))
        .map((r) => `${r.method} ${r.path}`)
    );
    const fixed = [...KNOWN_MISSING].filter((entry) => !stillBroken.has(entry));

    expect(fixed).toEqual([]);
  });

  it('reports how much of the client it can statically resolve', () => {
    // Not every call passes a literal — some build the URL first. Those can't
    // be checked here, so pin the count: if it grows, coverage silently
    // shrank and someone should either inline the path or extend the
    // extractor.
    expect(unresolved).toBeLessThanOrEqual(90);
  });
});

describe('requests issued outside the client\'s modules', () => {
  // A component that is handed the client can issue a request itself:
  // `gatewayClient.request('/v1/admin/tenants/current')`. Such a call is in
  // no module, so the contract above never saw it; two components asked for
  // the deployment capabilities that way. They are held to the
  // same list here, in every source file of the edition under test.
  const OUTSIDE = sourcesOutside(sourceRoots().map((root) => (root.endsWith('enterprise') ? `${root}/src` : root)), CLIENT_SOURCES);
  const direct = directCalls(OUTSIDE);

  it('sees the ways such a call is written', () => {
    const seen = directCallsIn(
      [
        "await gatewayClient.request('/v1/admin/tenants/current');",
        "const r = await gatewayClient?.request<any>('/v1/admin/users/login/2fa', { method: 'POST', body });",
        'gateway.gatewayClient\n    .request(`/v1/admin/tenants/${id}/kb-stats`)',
        "toModelOptions(await client.request('/v1/admin/llms?mode=embedding&limit=100'))",
        "return this.request('/v0/servers');",
        'await gatewayClient.request(url);',
      ].join('\n'),
    );
    expect(seen.routes.map((r) => `${r.method} ${r.path}`)).toEqual([
      'GET /v1/admin/tenants/current',
      'POST /v1/admin/users/login/2fa',
      'GET /v1/admin/tenants/{param}/kb-stats',
      'GET /v1/admin/llms',
    ]);
    expect(seen.unresolved).toBe(1);
  });

  it('looks in the files that are not the client, and finds none: every request is a method of a client module', () => {
    expect(OUTSIDE.length).toBeGreaterThan(400);
    expect(OUTSIDE.filter((file) => CLIENT_SOURCES.includes(file))).toEqual([]);
    expect(direct.calls.map(describeCall)).toEqual([]);
  });

  it('finds one where one is planted: a file of a component that asks for itself', () => {
    // With none left in the tree, this is what shows the search still
    // finds one: the same reading of a file, given a file that has two.
    const planted = directCallsIn(
      [
        "import { useGateway } from '../../hooks/useGateway';",
        'export function Planted() {',
        '  const { gatewayClient } = useGateway();',
        "  const load = () => gatewayClient.request('/v1/admin/tenants/current');",
        "  const revoke = (id: string) => gatewayClient.request(`/v1/admin/users/tenant/sessions/${id}/revoke`, { method: 'POST' });",
        '  return null;',
        '}',
      ].join('\n'),
    );
    expect(planted.routes.map((r) => `${r.method} ${r.path}`)).toEqual([
      'GET /v1/admin/tenants/current',
      'POST /v1/admin/users/tenant/sessions/{param}/revoke',
    ]);
    expect(planted.unresolved).toBe(0);
  });

  it('a call to a route the Control Plane does not serve is reported', () => {
    const planted = directCallsIn("gatewayClient.request('/v1/admin/deployments/capabilitys')").routes;
    expect(planted.filter((r) => !isServed(r.method, r.path)).map((r) => r.path)).toEqual(['/v1/admin/deployments/capabilitys']);
  });

  it('call only endpoints the Control Plane exposes', () => {
    const missing = direct.calls.filter((r) => !isServed(r.method, r.path)).map(describeCall);
    expect([...new Set(missing)].sort()).toEqual([]);
  });

  it('every one of them names its path where it is called', () => {
    // A path built somewhere else cannot be checked: write it in the call,
    // or make the call a method of a client module.
    expect(direct.unresolved).toBe(0);
  });

  it('none asks for the deployment capabilities: that is a method of the client now', () => {
    expect(direct.calls.filter((r) => r.path === '/v1/admin/deployments/capabilities').map(describeCall)).toEqual([]);
  });
});

describe('the route list of the edition under test', () => {
  const prefixes = enterpriseRoutePrefixes();
  const paths = vendoredPaths(CONTRACT);

  it('is the list of this package: the Community one under src/, the Enterprise one under enterprise/', () => {
    expect(CONTRACT.edition).toBe(enterpriseUnderTest() ? 'enterprise' : 'community');
    expect(CONTRACT.routes).toMatch(
      enterpriseUnderTest()
        ? /\/enterprise\/src\/host\/lib\/__tests__\/fixtures\/control-plane-routes\.json$/
        : /\/src\/lib\/__tests__\/fixtures\/control-plane-routes\.json$/
    );
    expect(CONTRACT.routes.includes('/enterprise/')).toBe(enterpriseUnderTest());
  });

  it('is a sorted list of distinct "METHOD /path" rows', () => {
    expect(vendoredRoutes.filter((row) => !/^(GET|PUT|POST|DELETE|PATCH|HEAD|OPTIONS) \/\S*$/.test(row))).toEqual([]);
    expect(new Set(vendoredRoutes).size).toBe(vendoredRoutes.length);
    expect([...vendoredRoutes].sort()).toEqual(vendoredRoutes);
    // The Community Control Plane serves some 650 operations, the Enterprise one some 870.
    expect(vendoredRoutes.length).toBeGreaterThan(enterpriseUnderTest() ? 800 : 600);
  });

  it('has the routes of the Enterprise features in the Enterprise list and in no other', () => {
    // Which document a list was read from, told without the document: the
    // Community application mounts nothing under an Enterprise route prefix,
    // and the Enterprise one mounts something under every one of them.
    expect(prefixes.length).toBeGreaterThan(15);
    const used = prefixes.filter((prefix) => paths.some((path) => isUnder(path, prefix)));
    expect(used).toEqual(enterpriseUnderTest() ? prefixes : []);
  });

  // Skipped where the Control Plane is not beside this repository (as
  // ../brutor-gateway-control or ../control-plane) and no directory of
  // documents is named (this repository's own CI, and a copy of the Community
  // tree alone): the document is not there to read.
  // `node scripts/sync-control-plane-routes.mjs` refuses in the same place, by name.
  const inSync = 'is in sync with the Control Plane OpenAPI snapshot of this edition';
  const available = documentForTest(CONTRACT.edition, `the route list of the edition under test ${inSync}`) !== null;

  it.skipIf(!available)(`${inSync} (skipped without the sibling Control Plane)`, async () => {
    const { SYNC_TARGETS, routesFromSpec } = await import('../../../scripts/sync-control-plane-routes.mjs');
    const { movedDocumentMessage } = await import('../../../scripts/control-plane-documents.mjs');
    const target = SYNC_TARGETS[CONTRACT.edition];
    // The script writes the file this test reads, from the document of this
    // edition: the Community one is `tests/fixtures/openapi_snapshot_community.json`
    // and the Enterprise one `enterprise/tests/fixtures/openapi_snapshot.json`.
    expect(target.out).toBe(CONTRACT.routes);
    const { toolEnv } = await import('../../../scripts/tool-env.mjs');
    const fileName = enterpriseUnderTest() ? 'openapi_snapshot.json' : 'openapi_snapshot_community.json';
    const fixtures = enterpriseUnderTest() ? 'enterprise/tests/fixtures' : 'tests/fixtures';
    // Read from the sibling Control Plane's fixtures, or, where a directory
    // of documents is named instead (the documents of one commit), from that
    // directory under the same file name.
    const documentsDir = toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR');
    const { siblingRepository } = await import('../../../scripts/sibling-repositories.mjs');
    expect(target.snapshot).toBe(
      documentsDir
        ? resolvePath(documentsDir, fileName)
        : resolvePath(siblingRepository('control-plane').root ?? '', fixtures, fileName)
    );
    const spec: unknown = JSON.parse(readFileSync(target.snapshot, 'utf8'));

    // The message says which document was read (its SHA-256) and which the
    // list was written from: when the two differ, the Control Plane document moved.
    const derived = routesFromSpec(spec);
    if (JSON.stringify(derived) !== JSON.stringify(vendoredRoutes)) {
      const added = derived.filter((row: string) => !vendoredRoutes.includes(row));
      const removed = vendoredRoutes.filter((row) => !derived.includes(row));
      throw new Error(
        [
          movedDocumentMessage(CONTRACT.edition, 'routes', target.snapshot, `node scripts/sync-control-plane-routes.mjs ${CONTRACT.edition}`),
          `Routes the document has and the list has not: ${JSON.stringify(added)}`,
          `Routes the list has and the document has not: ${JSON.stringify(removed)}`,
        ].join('\n')
      );
    }
    expect(derived).toEqual(vendoredRoutes);
  });
});

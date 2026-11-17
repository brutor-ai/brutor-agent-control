#!/usr/bin/env node
/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Regenerates the vendored route lists — the "METHOD /path" pairs the Control
// Plane exposes — one per edition:
//
//   community   src/lib/__tests__/fixtures/control-plane-routes.json
//               from the Control Plane's Community OpenAPI document
//   enterprise  enterprise/src/host/lib/__tests__/fixtures/control-plane-routes.json
//               from the Control Plane's Enterprise OpenAPI document
//
// The admin UI's CI checks out only this repo, so the Control Plane's OpenAPI
// snapshots aren't reachable there. We vendor the route lists instead: the
// contract test can then run anywhere, and when the Control Plane repo IS a
// sibling (the normal platform layout) the same test also checks each file is
// still in sync.
//
//   node scripts/sync-control-plane-routes.mjs               # every edition in this tree
//   node scripts/sync-control-plane-routes.mjs community     # one
//   node scripts/sync-control-plane-routes.mjs enterprise
//
// Run it after any Control Plane route change. The Control Plane regenerates
// its own snapshots with:
//   BRUTOR_UPDATE_OPENAPI_SNAPSHOT=1 pytest enterprise/tests/test_openapi_snapshot.py
//   BRUTOR_UPDATE_COMMUNITY_OPENAPI_SNAPSHOT=1 pytest tests/test_community_openapi_snapshot.py

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import { CONTROL_PLANE_DOCUMENTS, documentsLookedFor, editionsFromArgs, recordSource } from './control-plane-documents.mjs';

/** Per edition: the document a route list is read from (`snapshot`) and the vendored file (`out`). */
export const SYNC_TARGETS = Object.freeze(
  Object.fromEntries(
    Object.values(CONTROL_PLANE_DOCUMENTS).map(({ edition, document, routes }) => [
      edition,
      Object.freeze({ edition, snapshot: document, out: routes }),
    ])
  )
);

const HTTP_METHODS = new Set(['get', 'put', 'post', 'delete', 'patch', 'head', 'options']);

/** "METHOD /path" for every operation in an OpenAPI document. */
export function routesFromSpec(spec) {
  const out = [];
  for (const [path, item] of Object.entries(spec.paths ?? {})) {
    for (const method of Object.keys(item)) {
      if (HTTP_METHODS.has(method.toLowerCase())) {
        out.push(`${method.toUpperCase()} ${path}`);
      }
    }
  }
  return out.sort();
}

if (import.meta.url === `file://${process.argv[1]}`) {
  let editions;
  try {
    editions = editionsFromArgs(process.argv.slice(2), existsSync);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(2);
  }
  // Every document is read before any file is written: one missing document
  // leaves both lists as they were.
  const missing = editions.filter((edition) => !existsSync(SYNC_TARGETS[edition].snapshot));
  if (missing.length > 0) {
    for (const edition of missing) {
      console.error(`Control Plane ${edition} snapshot not found at:\n  ${SYNC_TARGETS[edition].snapshot}`);
    }
    console.error(`The documents are looked for ${documentsLookedFor()}.`);
    process.exit(1);
  }
  for (const edition of editions) {
    const { snapshot, out } = SYNC_TARGETS[edition];
    const routes = routesFromSpec(JSON.parse(readFileSync(snapshot, 'utf8')));
    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, JSON.stringify(routes, null, 2) + '\n');
    recordSource(edition, 'routes', snapshot);
    console.log(`Wrote ${routes.length} ${edition} routes to ${out}`);
  }
}

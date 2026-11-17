#!/usr/bin/env node
/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Generates the TypeScript types of the Control Plane's API, one file per
// edition, each from that edition's OpenAPI document:
//
//   community   src/lib/generated/control-plane-api.d.ts
//   enterprise  enterprise/src/lib/generated/control-plane-api.d.ts
//
//   node scripts/gen-control-plane-types.mjs               # every edition in this tree
//   node scripts/gen-control-plane-types.mjs community     # one
//   node scripts/gen-control-plane-types.mjs enterprise
//
// (`npm run gen:api-types:control` is the first with `community`;
// `npm run enterprise:gen:api-types:control` the second with `enterprise`.)

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';

import { CONTROL_PLANE_DOCUMENTS, documentsLookedFor, editionsFromArgs, recordSource } from './control-plane-documents.mjs';

const require = createRequire(import.meta.url);

/** The openapi-typescript command line, as this repository installed it. */
function generatorCli() {
  const manifest = require.resolve('openapi-typescript/package.json');
  const bin = require(manifest).bin;
  return resolve(dirname(manifest), typeof bin === 'string' ? bin : bin['openapi-typescript']);
}

/**
 * Writes the types of `document` to `out`, exactly as
 * `npx openapi-typescript document --default-non-nullable=false -o out` does.
 *
 * `--default-non-nullable=false`: a field is required only where the
 * document's `required` lists it. A field with a default is optional, in a
 * request (the Control Plane fills it) and in a response alike.
 */
export function generateTypes(document, out) {
  execFileSync(process.execPath, [generatorCli(), document, '--default-non-nullable=false', '-o', out], { stdio: ['ignore', 'pipe', 'pipe'] });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  let editions;
  try {
    editions = editionsFromArgs(process.argv.slice(2), existsSync);
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(2);
  }
  const missing = editions.filter((edition) => !existsSync(CONTROL_PLANE_DOCUMENTS[edition].document));
  if (missing.length > 0) {
    for (const edition of missing) {
      console.error(`Control Plane ${edition} document not found at:\n  ${CONTROL_PLANE_DOCUMENTS[edition].document}`);
    }
    console.error(`The documents are looked for ${documentsLookedFor()}.`);
    process.exit(1);
  }
  for (const edition of editions) {
    const { document, types } = CONTROL_PLANE_DOCUMENTS[edition];
    generateTypes(document, types);
    recordSource(edition, 'types', document);
    console.log(`Wrote the ${edition} types to ${types}`);
  }
}

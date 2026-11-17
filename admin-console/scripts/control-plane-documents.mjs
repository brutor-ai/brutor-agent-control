/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// The Control Plane's OpenAPI documents, one per edition, and what this
// repository derives from each.
//
// The Control Plane composes two applications from one build: the Community
// one, and the Enterprise one (the Community one plus the Enterprise routers).
// Each has its own recorded OpenAPI document. Each package of this repository
// is held to the document of its edition:
//
//   community  — the Community package (src/): the route list its API modules
//                are checked against, and the types they are written with.
//   enterprise — the Enterprise package (enterprise/): the same two, for the
//                whole client (the Community modules and the Enterprise ones).
//
// This is the one place the paths are written. Two scripts read it
// (sync-control-plane-routes.mjs, gen-control-plane-types.mjs) and so do the
// tests that hold the vendored files to the documents.
//
// The Control Plane keeps the Community document in tests/fixtures as
// openapi_snapshot_community.json and the Enterprise one in
// enterprise/tests/fixtures as openapi_snapshot.json. If they move,
// `document` below is what changes.

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { siblingRepository, siblingPlaces, skipped } from './sibling-repositories.mjs';
import { toolEnv } from './tool-env.mjs';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
// The sibling Control Plane (scripts/sibling-repositories.mjs); where it is
// not there, the place it is looked for first, where no document is.
const CONTROL_PLANE = siblingRepository('control-plane');
const CONTROL_PLANE_ROOT = CONTROL_PLANE.root ?? CONTROL_PLANE.places[0];

/**
 * Where the two documents are read from. By default their fixtures in the
 * sibling Control Plane working tree (`../brutor-gateway-control`, or
 * `../control-plane` in the public repository). `BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR`
 * names another directory holding the two files under the same names, for
 * generating from the documents of one commit while that working tree is
 * being changed:
 *
 *   git -C ../brutor-gateway-control show <commit>:enterprise/tests/fixtures/openapi_snapshot.json > <dir>/openapi_snapshot.json
 *   git -C ../brutor-gateway-control show <commit>:tests/fixtures/openapi_snapshot_community.json > <dir>/openapi_snapshot_community.json
 *   BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR=<dir> node scripts/gen-control-plane-types.mjs
 *   BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR=<dir> node scripts/sync-control-plane-routes.mjs
 */
const DOCUMENTS_DIR = toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR');

/** Where a document is: at its path in the sibling Control Plane, or under its own name in the directory named instead. */
function documentAt(pathInControlPlane) {
  return DOCUMENTS_DIR ? resolve(DOCUMENTS_DIR, basename(pathInControlPlane)) : resolve(CONTROL_PLANE_ROOT, pathInControlPlane);
}

/** Where the documents are looked for, for a message that says one is not there. */
export function documentsLookedFor() {
  return DOCUMENTS_DIR
    ? `in BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR (${DOCUMENTS_DIR})`
    : `in the Control Plane's fixtures beside this repository, at ${siblingPlaces('control-plane')}`;
}

/**
 * For a test that reads the document of `edition`: its path, or null after a
 * `SKIPPED:` line on stderr that names the test and where it was looked for.
 * @param {'community' | 'enterprise'} edition
 * @param {string} test What is skipped without it.
 * @returns {string | null}
 */
export function documentForTest(edition, test) {
  const { document } = CONTROL_PLANE_DOCUMENTS[edition];
  if (existsSync(document)) return document;
  skipped(test, `the Control Plane ${edition} document is not there: it was looked for ${documentsLookedFor()}`);
  return null;
}

/**
 * Per edition: `document` (the Control Plane's recorded OpenAPI document),
 * `routes` (the vendored "METHOD /path" list), `types` (the generated
 * TypeScript) and `packageRoot` (the directory whose package owns the two).
 */
export const CONTROL_PLANE_DOCUMENTS = Object.freeze({
  community: Object.freeze({
    edition: 'community',
    packageRoot: REPO_ROOT,
    document: documentAt('tests/fixtures/openapi_snapshot_community.json'),
    routes: resolve(REPO_ROOT, 'src/lib/__tests__/fixtures/control-plane-routes.json'),
    types: resolve(REPO_ROOT, 'src/lib/generated/control-plane-api.d.ts'),
  }),
  enterprise: Object.freeze({
    edition: 'enterprise',
    packageRoot: resolve(REPO_ROOT, 'enterprise'),
    document: documentAt('enterprise/tests/fixtures/openapi_snapshot.json'),
    routes: resolve(REPO_ROOT, 'enterprise/src/host/lib/__tests__/fixtures/control-plane-routes.json'),
    types: resolve(REPO_ROOT, 'enterprise/src/lib/generated/control-plane-api.d.ts'),
  }),
});

export const EDITIONS = Object.freeze(Object.keys(CONTROL_PLANE_DOCUMENTS));

/**
 * The editions a command line names: `community`, `enterprise`, or nothing
 * for every edition whose package is in this tree (a Community tree has no
 * enterprise/ directory, and that is not an error). An unknown name is one.
 */
export function editionsFromArgs(args, packageExists) {
  if (args.length === 0) return EDITIONS.filter((edition) => packageExists(CONTROL_PLANE_DOCUMENTS[edition].packageRoot));
  const unknown = args.filter((arg) => !EDITIONS.includes(arg));
  if (unknown.length > 0) throw new Error(`Unknown edition: ${unknown.join(', ')}. One of: ${EDITIONS.join(', ')}.`);
  return [...new Set(args)];
}

/**
 * Which document each vendored file was last written from: the SHA-256 of
 * the document, per edition and per file (`routes`, `types`). The two scripts
 * record it when they write; the tests that compare a vendored file with the
 * sibling Control Plane's document print it beside that document's hash when
 * they differ, so a failure says at once that the document moved (and from
 * what), not only that something is different.
 */
export const SOURCES_FILE = resolve(REPO_ROOT, 'scripts/control-plane-sources.json');

/** The SHA-256 of a file, in hex. */
export function documentHash(file) {
  return createHash('sha256').update(readFileSync(file)).digest('hex');
}

function readSources() {
  if (!existsSync(SOURCES_FILE)) return {};
  return JSON.parse(readFileSync(SOURCES_FILE, 'utf8'));
}

/** Record that the `kind` file (`routes` or `types`) of `edition` was written from `document`. */
export function recordSource(edition, kind, document) {
  const sources = readSources();
  sources[edition] = { ...sources[edition], [kind]: documentHash(document) };
  const ordered = Object.fromEntries(
    Object.keys(sources)
      .sort()
      .map((name) => [name, Object.fromEntries(Object.entries(sources[name]).sort(([a], [b]) => a.localeCompare(b)))])
  );
  writeFileSync(SOURCES_FILE, JSON.stringify(ordered, null, 2) + '\n');
}

/** The hash recorded for the `kind` file of `edition`, or `undefined` when none was. */
export function recordedSource(edition, kind) {
  const recorded = readSources()[edition]?.[kind];
  return typeof recorded === 'string' ? recorded : undefined;
}

/**
 * What a test says when a vendored file is not what `document` gives: the
 * hash of the document as it is now, the hash the file was written from, and
 * the command that writes it again.
 */
export function movedDocumentMessage(edition, kind, document, command) {
  const now = documentHash(document);
  const then = recordedSource(edition, kind);
  const moved =
    then === undefined
      ? 'No source hash is recorded for the vendored file.'
      : then === now
        ? 'That is the document the vendored file was written from: the file was changed by hand, or the generator changed.'
        : `The vendored file was written from sha256 ${then}: the Control Plane document moved.`;
  return [
    `The ${edition} ${kind} file is not what the Control Plane ${edition} document gives.`,
    `Document: ${document}`,
    `Document sha256 now: ${now}`,
    moved,
    `To write it again from the document as it is now: ${command}`,
  ].join('\n');
}

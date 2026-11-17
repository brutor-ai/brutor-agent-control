/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Where a sibling repository is: the one place a test or a script of this
// repository looks for the Control Plane or the gateway core beside it.
//
// A sibling is in one of two places beside this repository, looked at in
// this order:
//
//   1. the directory named after its own repository (`../brutor-gateway-control`),
//   2. its directory in the public repository, where this one is
//      `admin-console/` (`../control-plane`).
//
// The first that is a directory is the sibling. Where neither is, a test
// that reads the sibling is skipped, and a script that needs it refuses;
// both say where it was looked for (`missingSibling`).
//
// A variable that names another place for what is read (the Control Plane's
// documents: BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR) is used alone where it is
// set; the caller reads it, and looks here only where it is not.

import { statSync } from 'node:fs';
import { resolve } from 'node:path';

import { REPO_ROOT } from './tool-env.mjs';

/**
 * Each sibling: what it is called in a message, and its two directory names,
 * the repository's first.
 * @type {Readonly<Record<'control-plane' | 'core-gateway', Readonly<{ label: string, directories: readonly [string, string] }>>>}
 */
export const SIBLINGS = Object.freeze({
  'control-plane': Object.freeze({ label: 'the Control Plane', directories: Object.freeze(/** @type {const} */ (['brutor-gateway-control', 'control-plane'])) }),
  'core-gateway': Object.freeze({ label: 'the gateway core', directories: Object.freeze(/** @type {const} */ (['brutor-gateway-core', 'core-gateway'])) }),
});

/** @typedef {keyof typeof SIBLINGS} SiblingName */

/**
 * @param {string} name
 * @returns {(typeof SIBLINGS)[SiblingName]}
 */
function sibling(name) {
  if (!Object.hasOwn(SIBLINGS, name)) throw new Error(`No sibling repository is called ${JSON.stringify(name)}: one of ${Object.keys(SIBLINGS).join(', ')}.`);
  return SIBLINGS[/** @type {SiblingName} */ (name)];
}

/** @param {string} path */
const isDirectory = (path) => {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
};

/**
 * Where the sibling `name` is beside `repoRoot` (this repository by default):
 * `root`, the first of `places` that is a directory, or null where neither is.
 * @param {SiblingName} name
 * @param {string} [repoRoot]
 * @returns {{ root: string | null, places: string[] }}
 */
export function siblingRepository(name, repoRoot = REPO_ROOT) {
  const places = sibling(name).directories.map((directory) => resolve(repoRoot, '..', directory));
  return { root: places.find(isDirectory) ?? null, places };
}

/**
 * Where the sibling `name` is looked for, in order: `../brutor-gateway-control and at ../control-plane`.
 * @param {SiblingName} name
 * @returns {string}
 */
export function siblingPlaces(name) {
  return sibling(name)
    .directories.map((directory) => `../${directory}`)
    .join(' and at ');
}

/**
 * What a skipped test or a refusing script says where the sibling `name` is under neither name.
 * @param {SiblingName} name
 * @returns {string}
 */
export function missingSibling(name) {
  return `${sibling(name).label} is not beside this repository: it was looked for at ${siblingPlaces(name)}`;
}

/**
 * For a test that reads the sibling `name`: its root, or null after a
 * `SKIPPED:` line on stderr that names the test and both places.
 * @param {SiblingName} name
 * @param {string} test What is skipped without it.
 * @returns {string | null}
 */
export function siblingForTest(name, test) {
  const { root } = siblingRepository(name);
  if (root === null) skipped(test, missingSibling(name));
  return root;
}

/**
 * Record that `test` is skipped, and why: a line on stderr that starts with `SKIPPED:`.
 * @param {string} test
 * @param {string} why
 */
export function skipped(test, why) {
  console.warn(`SKIPPED: ${test}: ${why}`);
}

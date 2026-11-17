/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// The environment of the build tools and tests, read in one place, and the
// registry of every environment variable of the Admin Console.
//
// The registry is two JSON files, one per package: src/envRegistry.json (the
// Community console) and enterprise/envRegistry.json (what only the
// Enterprise package reads). Each variable is defined once there: its name,
// type, default, description, example and edition.
//
// Who reads what:
//
//   the application   src/env.ts: at build time what Vite puts in the bundle
//                     (the `build_name`s), at run time what
//                     docker-entrypoint.sh writes for the page from the
//                     container's environment (the `name`s).
//   the build tools   this module, and nothing else: `toolEnv` and `toolFlag`
//                     for the variables whose `read_by` is `tool`.
//
// A name the registries do not define is not read anywhere. A build tool that
// asks for one is refused (`toolEnv`), and a switch whose value is no spelling
// of a switch is refused, by name (`toolFlag`).

import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * One variable of a registry.
 * @typedef {object} EnvVariable
 * @property {string} name The name in a container's environment, and a build tool's name.
 * @property {string | null} build_name The `VITE_` name Vite embeds from `.env`; null for none.
 * @property {'application' | 'tool'} read_by
 * @property {'url' | 'boolean' | 'path' | 'string'} type
 * @property {string | null} default
 * @property {'both' | 'community' | 'enterprise'} edition
 * @property {string} description
 * @property {string} accepted
 * @property {string} example
 */

/**
 * @typedef {object} EnvRegistry
 * @property {number} schema_version
 * @property {string} component
 * @property {EnvVariable[]} variables
 */

/** @typedef {'community' | 'enterprise'} Edition */
/** @typedef {Record<string, string | undefined>} Environment */

export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/**
 * The registry file of each package, from the repository root.
 * @type {Readonly<Record<Edition, string>>}
 */
export const REGISTRY_FILES = Object.freeze({
  community: 'src/envRegistry.json',
  enterprise: 'enterprise/envRegistry.json',
});

/**
 * The registry of one package; null when the package is not in this tree (a Community tree has no enterprise/).
 * @param {Edition} edition
 * @returns {EnvRegistry | null}
 */
export function readRegistry(edition) {
  const file = resolve(REPO_ROOT, REGISTRY_FILES[edition]);
  return existsSync(file) ? /** @type {EnvRegistry} */ (JSON.parse(readFileSync(file, 'utf8'))) : null;
}

/**
 * The registries of the packages in this tree, Community first.
 * @returns {EnvRegistry[]}
 */
export function registries() {
  /** @type {EnvRegistry[]} */
  const found = [];
  for (const edition of /** @type {Edition[]} */ (Object.keys(REGISTRY_FILES))) {
    const registry = readRegistry(edition);
    if (registry !== null) found.push(registry);
  }
  return found;
}

const variables = () => registries().flatMap((registry) => registry.variables);

/**
 * The one spelling of a switch every variable accepts.
 * @type {readonly string[]}
 */
export const BOOLEAN_SPELLINGS = Object.freeze(['true', 'false', '1', '0', 'yes', 'no', 'on', 'off']);

/**
 * `true` or `false` for one of the accepted spellings (any case); `undefined` for anything else.
 * @param {unknown} raw
 * @returns {boolean | undefined}
 */
export function parseBoolean(raw) {
  const value = String(raw).trim().toLowerCase();
  if (['true', '1', 'yes', 'on'].includes(value)) return true;
  if (['false', '0', 'no', 'off'].includes(value)) return false;
  return undefined;
}

/**
 * @param {Environment} environment
 * @param {string} name
 * @returns {boolean}
 */
const isSet = (environment, name) => typeof environment[name] === 'string' && environment[name] !== '';

/**
 * @param {string} name
 * @returns {EnvVariable}
 */
function toolVariable(name) {
  const variable = variables().find((candidate) => candidate.name === name && candidate.read_by === 'tool');
  if (!variable) throw new Error(`${name} is not a build-tool variable of the registry (${Object.values(REGISTRY_FILES).join(', ')})`);
  return variable;
}

/**
 * The value of a build-tool variable; `undefined` when it is unset or empty. Only a name the registry defines is read.
 * @param {string} name
 * @param {Environment} [environment]
 * @returns {string | undefined}
 */
export function toolEnv(name, environment = process.env) {
  toolVariable(name);
  return isSet(environment, name) ? environment[name] : undefined;
}

/**
 * A build-tool switch: its value, or its default when unset. A value that is no spelling of a switch is refused, by name.
 * @param {string} name
 * @param {Environment} [environment]
 * @returns {boolean}
 */
export function toolFlag(name, environment = process.env) {
  const variable = toolVariable(name);
  const raw = toolEnv(name, environment) ?? variable.default ?? 'false';
  const value = parseBoolean(raw);
  if (value === undefined) throw new Error(`${name} is not valid: it must be one of ${BOOLEAN_SPELLINGS.join(', ')}`);
  return value;
}

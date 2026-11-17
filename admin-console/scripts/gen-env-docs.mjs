#!/usr/bin/env node
/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Writes what is documented about the environment variables from the
// registry (src/envRegistry.json, enterprise/envRegistry.json; see
// tool-env.mjs), so that a variable cannot be documented under another name
// than the one that is read:
//
//   README.md             the table between the two `env-registry` markers
//   .env.example          the whole file
//   enterprise/README.md  the table between the two `env-registry` markers
//
//   node scripts/gen-env-docs.mjs           # write them
//   node scripts/gen-env-docs.mjs --check   # exit 1 when one is out of date
//
// src/__tests__/envRegistry.test.ts holds the files to this output.

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { BOOLEAN_SPELLINGS, REGISTRY_FILES, REPO_ROOT, readRegistry } from './tool-env.mjs';

export { BOOLEAN_SPELLINGS };

/** @param {Edition} edition */
const BEGIN = (edition) => `<!-- env-registry:begin (generated from ${REGISTRY_FILES[edition]} by scripts/gen-env-docs.mjs) -->`;
const END = '<!-- env-registry:end -->';

/** @typedef {import('./tool-env.mjs').EnvRegistry} EnvRegistry */
/** @typedef {import('./tool-env.mjs').Edition} Edition */

/**
 * A file written from a registry.
 * @typedef {object} GeneratedDocument
 * @property {string} file From the repository root.
 * @property {Edition} edition Whose registry it is written from.
 * @property {'tables' | 'example'} kind The tables between the markers, or the whole example file.
 */

/** @param {string} text */
const code = (text) => `\`${text}\``;
/** @param {string} text */
const cell = (text) => String(text).replace(/\|/g, '\\|');

/**
 * The tables of one registry: the variables the console reads, then those the build tools read.
 * @param {EnvRegistry} registry
 * @returns {string}
 */
export function readmeTables(registry) {
  const application = registry.variables.filter((variable) => variable.read_by === 'application');
  const tools = registry.variables.filter((variable) => variable.read_by === 'tool');
  /** @type {string[]} */
  const lines = [];

  if (application.length > 0) {
    lines.push(
      'Read by the console:',
      '',
      '| In a container (environment) | From source (`.env`, read by Vite) | Type | Default | Edition | Description | Accepted | Example |',
      '|---|---|---|---|---|---|---|---|',
    );
    for (const v of application) {
      lines.push(
        `| ${code(v.name)} | ${v.build_name ? code(v.build_name) : 'none'} | ${v.type} | ${v.default === null ? 'none' : code(v.default)} | ${v.edition} | ${cell(v.description)} | ${cell(v.accepted)} | ${code(v.example)} |`,
      );
    }
    lines.push('');
  }

  if (tools.length > 0) {
    lines.push(
      'Read by the build tools and tests:',
      '',
      '| Variable | Type | Default | Edition | Description | Accepted | Example |',
      '|---|---|---|---|---|---|---|',
    );
    for (const v of tools) {
      lines.push(
        `| ${code(v.name)} | ${v.type} | ${v.default === null ? 'none' : code(v.default)} | ${v.edition} | ${cell(v.description)} | ${cell(v.accepted)} | ${code(v.example)} |`,
      );
    }
    lines.push('');
  }
  return lines.join('\n');
}

/**
 * `.env.example`: the build-time names of the application's variables, at their defaults.
 * @param {EnvRegistry} registry
 * @returns {string}
 */
export function envExample(registry) {
  const lines = [
    '# Brutor Agent Control Admin Console: environment for a run from source.',
    '#',
    `# Generated from ${REGISTRY_FILES.community} by scripts/gen-env-docs.mjs. Do not edit it by hand.`,
    '#',
    '# Copy to .env and adjust:',
    '#   cp .env.example .env',
    '#',
    '# Both packages (Community at the root, Enterprise in enterprise/) read this',
    '# file. A value prefixed VITE_ is embedded in the bundle the browser downloads:',
    '# put no secret here. In a container the same settings are given to the',
    '# container under their BRUTOR_ names (README.md, "Configure").',
    '',
  ];
  for (const v of registry.variables) {
    if (v.read_by !== 'application' || !v.build_name) continue;
    lines.push(...wrap(`${v.description} In a container: ${v.name}.`), `${v.build_name}=${v.default ?? ''}`, '');
  }
  return lines.join('\n');
}

/**
 * `text` as comment lines of at most 78 characters.
 * @param {string} text
 * @returns {string[]}
 */
function wrap(text) {
  /** @type {string[]} */
  const lines = [];
  let line = '#';
  for (const word of text.split(/\s+/)) {
    if (line.length + 1 + word.length > 78 && line !== '#') {
      lines.push(line);
      line = '#';
    }
    line += ` ${word}`;
  }
  lines.push(line);
  return lines;
}

/**
 * `current` with what is between the markers of `edition` replaced by its tables.
 * @param {string} current
 * @param {Edition} edition
 * @param {EnvRegistry} registry
 * @returns {string}
 */
function withTables(current, edition, registry) {
  const begin = current.indexOf(BEGIN(edition));
  const end = current.indexOf(END, begin);
  if (begin < 0 || end < 0) {
    throw new Error(`The markers are missing: ${BEGIN(edition)} ... ${END}`);
  }
  return `${current.slice(0, begin)}${BEGIN(edition)}\n\n${readmeTables(registry)}\n${current.slice(end)}`;
}

/**
 * The files written from the registry: where, from which package's registry, and how.
 * @type {readonly GeneratedDocument[]}
 */
export const GENERATED_DOCUMENTS = Object.freeze([
  { file: 'README.md', edition: 'community', kind: 'tables' },
  { file: '.env.example', edition: 'community', kind: 'example' },
  { file: 'enterprise/README.md', edition: 'enterprise', kind: 'tables' },
]);

/**
 * What `document` must hold, given what it holds now (the text outside the markers is kept).
 * @param {GeneratedDocument} document
 * @param {string} current
 * @returns {string}
 */
export function generatedDocument(document, current) {
  const registry = readRegistry(document.edition);
  if (registry === null) throw new Error(`No registry for ${document.edition}`);
  return document.kind === 'example' ? envExample(registry) : withTables(current, document.edition, registry);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const check = process.argv.includes('--check');
  let stale = 0;
  for (const document of GENERATED_DOCUMENTS) {
    const file = resolve(REPO_ROOT, document.file);
    // A Community tree has no enterprise/.
    if (readRegistry(document.edition) === null) continue;
    const current = existsSync(file) ? readFileSync(file, 'utf8') : '';
    const wanted = generatedDocument(document, current);
    if (current === wanted) continue;
    if (check) {
      console.error(`${document.file} is out of date: node scripts/gen-env-docs.mjs writes it again`);
      stale += 1;
    } else {
      writeFileSync(file, wanted);
      console.log(`Wrote ${document.file}`);
    }
  }
  process.exit(stale > 0 ? 1 : 0);
}

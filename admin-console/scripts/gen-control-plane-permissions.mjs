#!/usr/bin/env node
/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Writes scripts/control-plane-permissions.json: the values of every
// administrator permission the Control Plane registers, per edition.
//
//   node scripts/gen-control-plane-permissions.mjs            # the Control Plane repository beside this one, at its HEAD
//                                                             # (../brutor-gateway-control, or ../control-plane in the public repository)
//   node scripts/gen-control-plane-permissions.mjs <commit>   # at that commit
//   BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR=<dir> node scripts/gen-control-plane-permissions.mjs --documents-dir
//                                                             # the two modules in <dir>, at their Control Plane paths
//
// The console shows an action to an administrator who holds a permission and
// hides it from one who does not. A permission string the Control Plane does
// not register is held by nobody, so what it guards is never shown.
// src/__tests__/permissionStrings.test.ts holds every permission string the
// console checks to this list.
//
// The two files are read as source text, never by running anything of the
// Control Plane:
//
//   app/core/admin_user/permissions.py           class Permission
//   enterprise/brutor_enterprise/permissions.py  class EnterprisePermission
//
// From the repository they are read with `git show`, never from its working
// tree. With `--documents-dir` they are read from the directory of documents
// (the one the contract scripts read the OpenAPI documents from), where each
// sits at the path above: the way to generate from modules that are in no
// commit yet. Nothing is written outside this repository in either case.
//
// The file written records the commit it was read at (null for a directory)
// and the SHA-256 of each module's source. Run this again after a permission
// is added to or renamed in the Control Plane.

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { missingSibling, siblingRepository } from './sibling-repositories.mjs';
import { REPO_ROOT, toolEnv } from './tool-env.mjs';

/** The sibling Control Plane (scripts/sibling-repositories.mjs): its root, or null where it is not beside this repository. */
export const CONTROL_PLANE = siblingRepository('control-plane').root;
export const PERMISSIONS_FILE = 'scripts/control-plane-permissions.json';

/** Where each edition's permissions are defined: the file, and the enum class in it. */
export const SOURCES = Object.freeze({
  community: { file: 'app/core/admin_user/permissions.py', enum: 'Permission' },
  enterprise: { file: 'enterprise/brutor_enterprise/permissions.py', enum: 'EnterprisePermission' },
});

/**
 * The values of the string enum `name` in the Python source `source`, in source order.
 * @param {string} source
 * @param {string} name
 * @returns {string[]}
 */
export function enumValues(source, name) {
  const lines = source.split('\n');
  const start = lines.findIndex((line) => new RegExp(`^class ${name}\\(`).test(line));
  if (start < 0) throw new Error(`no class ${name}`);
  /** @type {string[]} */
  const values = [];
  for (const line of lines.slice(start + 1)) {
    // The class ends at the next line that is not indented.
    if (/^\S/.test(line)) break;
    const member = /^ {4}[A-Z][A-Z0-9_]*\s*=\s*(["'])([^"']+)\1\s*(?:#.*)?$/.exec(line);
    if (member) values.push(member[2]);
  }
  if (values.length === 0) throw new Error(`class ${name} has no members`);
  return values;
}

/**
 * @param {string[]} args
 * @returns {string}
 */
const git = (args) => {
  if (CONTROL_PLANE === null) throw new Error(`${missingSibling('control-plane')}: check it out beside this one, or read the modules from a directory with --documents-dir.`);
  return execFileSync('git', ['-C', CONTROL_PLANE, ...args], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
};

/**
 * `<commit>:./<file>`: a file of the Control Plane at a commit, named from the Control Plane's
 * directory, which is the root of its own repository or a directory of the public one.
 * @param {string} commit
 * @param {string} file
 * @returns {string}
 */
export const atCommit = (commit, file) => `${commit}:./${file}`;

/**
 * The SHA-256 of a module's source text, in hex.
 * @param {string} source
 * @returns {string}
 */
export function sourceHash(source) {
  return createHash('sha256').update(source).digest('hex');
}

/**
 * The document written to scripts/control-plane-permissions.json.
 * @param {{ community: string, enterprise: string }} sources The source text of each edition's permission module.
 * @param {string | null} commit The commit the modules were read at; null when they were read from a directory.
 */
export function permissionsDocument(sources, commit) {
  return {
    $comment:
      'The administrator permissions the Control Plane registers, per edition. Generated by scripts/gen-control-plane-permissions.mjs from the two permission modules of the Control Plane: at the commit named here, or (control_plane_commit null) from a directory of documents. source_sha256 is the SHA-256 of each module read. Do not edit it by hand. src/__tests__/permissionStrings.test.ts holds every permission string the console checks to it.',
    control_plane_commit: commit,
    sources: Object.fromEntries(Object.entries(SOURCES).map(([edition, { file, enum: name }]) => [edition, `${file} (class ${name})`])),
    source_sha256: { community: sourceHash(sources.community), enterprise: sourceHash(sources.enterprise) },
    community: enumValues(sources.community, SOURCES.community.enum),
    enterprise: enumValues(sources.enterprise, SOURCES.enterprise.enum),
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const fromDirectory = process.argv[2] === '--documents-dir';
  const documentsDir = toolEnv('BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR');
  if (fromDirectory && !documentsDir) {
    console.error('--documents-dir reads the two permission modules from BRUTOR_CONTROL_PLANE_DOCUMENTS_DIR, which is not set.');
    process.exit(2);
  }
  const commit = fromDirectory ? null : git(['rev-parse', '--verify', `${process.argv[2] ?? 'HEAD'}^{commit}`]).trim();
  /** @param {string} file */
  const read = (file) => (commit === null ? readFileSync(resolve(documentsDir ?? '', file), 'utf8') : git(['show', atCommit(commit, file)]));
  const document = permissionsDocument({ community: read(SOURCES.community.file), enterprise: read(SOURCES.enterprise.file) }, commit);
  writeFileSync(resolve(REPO_ROOT, PERMISSIONS_FILE), `${JSON.stringify(document, null, 2)}\n`);
  console.log(
    `Wrote ${PERMISSIONS_FILE}: ${document.community.length} Community and ${document.enterprise.length} Enterprise permissions, from ${commit === null ? `the modules in ${documentsDir}` : `the Control Plane at ${commit}`}`,
  );
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The pieces of the route contract both packages use:
 * the calls a set of client files makes, and whether a route list serves a
 * call. `gatewayApiClient.routes.test.ts` holds the client of the edition
 * under test to that edition's list; the Enterprise package's
 * `apiContract.test.ts` holds the two lists, and the two halves of the
 * client, to each other.
 */

import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { NOT_SOURCE_DIRS, sourceName } from '../../../test/sourceRoots';
import { extractClientRoutes } from './extractClientRoutes';

export interface ClientCall {
  method: string;
  /** Path with `${...}` interpolations normalised to `{param}`. */
  path: string;
  line: number;
  /** The file the call is written in, as `sourceName` names it. */
  file: string;
}

export interface ClientCalls {
  calls: ClientCall[];
  /** Call sites whose path is not a literal, and so cannot be checked. */
  unresolved: number;
}

/** Every request the given client files issue with a literal path. */
export function clientCalls(files: readonly string[]): ClientCalls {
  const extracted = files.map((file) => ({ file, ...extractClientRoutes(readFileSync(file, 'utf8')) }));
  return {
    calls: extracted.flatMap(({ file, routes }) => routes.map((r) => ({ ...r, file: sourceName(file) }))),
    unresolved: extracted.reduce((n, e) => n + e.unresolved, 0),
  };
}

const isParam = (segment: string) => /^\{.*\}$/.test(segment);

/**
 * Does a route list serve a call? Returns the test for one list of
 * "METHOD /path" rows.
 *
 * Compared segment by segment rather than as normalised strings, because a
 * `{param}` on either side matches a literal on the other. The client builds
 * `/end-users/export/${format}` while the spec declares the concrete
 * `/end-users/export/csv`; that is the same endpoint, and comparing
 * normalised strings reports it as missing.
 */
export function servedBy(routeList: readonly string[]): (method: string, path: string) => boolean {
  const serverRoutes = routeList.map((entry) => {
    const [method, path] = entry.split(' ');
    return { method, segments: path.split('/') };
  });
  return (method, path) => {
    const call = path.split('/');
    return serverRoutes.some(({ method: m, segments }) => {
      if (m !== method || segments.length !== call.length) return false;
      return segments.every((segment, i) => segment === call[i] || isParam(segment) || isParam(call[i]));
    });
  };
}

/** A call as a failure message names it. */
export const describeCall = (call: ClientCall): string => `${call.method} ${call.path}  (${call.file}:${call.line})`;

/**
 * The same call written outside the client's modules: a component, a context
 * or a registry that issues a request on the client it was handed
 * (`gatewayClient.request('/v1/...')`, `client?.request<T>(...)`,
 * `gateway.gatewayClient\n.request(...)`). Those are not methods of a module,
 * so the scan of the modules does not see them.
 */
const DIRECT_CALL = /\b(?:gatewayClient|client)\s*\??\.\s*request\b/g;

/** Every request `source` issues on a client it was handed, with a literal path. */
export function directCallsIn(source: string): ReturnType<typeof extractClientRoutes> {
  // A `this.request` here is another client's own (the registry's): not one of these.
  return extractClientRoutes(source.replace(/\bthis\.request\b/g, 'this.ownRequest').replace(DIRECT_CALL, 'this.request'));
}

/** The source files under `roots` that are not tests, test helpers or the client's own files. */
export function sourcesOutside(roots: readonly string[], clientFiles: readonly string[]): string[] {
  const client = new Set(clientFiles);
  const walk = (dir: string): string[] =>
    readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        return entry.name === '__tests__' || entry.name === 'test' || entry.name === 'generated' || NOT_SOURCE_DIRS.has(entry.name) ? [] : walk(full);
      }
      return /\.tsx?$/.test(entry.name) && !/\.(test|spec|d)\.tsx?$/.test(entry.name) && !client.has(full) ? [full] : [];
    });
  return roots.flatMap(walk).sort();
}

/** Every request the given files issue on a client they were handed, with a literal path. */
export function directCalls(files: readonly string[]): ClientCalls {
  const extracted = files.map((file) => ({ file, ...directCallsIn(readFileSync(file, 'utf8')) }));
  return {
    calls: extracted.flatMap(({ file, routes }) => routes.map((r) => ({ ...r, file: sourceName(file) }))),
    unresolved: extracted.reduce((n, e) => n + e.unresolved, 0),
  };
}

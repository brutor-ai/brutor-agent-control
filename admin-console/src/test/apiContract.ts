/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The API contract of the edition under test.
 *
 * The Control Plane has one OpenAPI document per edition. Each package is
 * held to the document of its own: the Community package checks its API
 * modules against the Community document and is written with the types
 * generated from it; the Enterprise package checks the whole client against
 * the Enterprise document. Where each file is, is written once, in
 * `scripts/control-plane-documents.mjs`; this module says which row a run
 * reads, by the edition under test (`underTest.ts`), and reads it.
 */

import { readFileSync } from 'node:fs';

import { CONTROL_PLANE_DOCUMENTS } from '../../scripts/control-plane-documents.mjs';
import { readDenylist } from '../../scripts/scan-community-bundle.mjs';
import { getArray, parseJson } from '../lib/narrow';

import { enterpriseUnderTest } from './sourceRoots';

export type ContractEdition = 'community' | 'enterprise';

export interface ApiContract {
    edition: ContractEdition;
    /** The Control Plane's recorded OpenAPI document, in the sibling repository. May be absent. */
    document: string;
    /** The vendored list of "METHOD /path" rows read from that document. */
    routes: string;
    /** The TypeScript generated from that document. */
    types: string;
}

export function apiContract(edition: ContractEdition): ApiContract {
    const { document, routes, types } = CONTROL_PLANE_DOCUMENTS[edition];
    return { edition, document, routes, types };
}

/** The contract this run holds its package to: the Enterprise one in the Enterprise package's run. */
export function apiContractUnderTest(): ApiContract {
    return apiContract(enterpriseUnderTest() ? 'enterprise' : 'community');
}

const isString = (value: unknown): value is string => typeof value === 'string';

/** The vendored route list of a contract, as written. */
export function vendoredRoutes(contract: ApiContract): string[] {
    const rows = parseJson(readFileSync(contract.routes, 'utf8'));
    if (!Array.isArray(rows) || !rows.every(isString)) {
        throw new Error(`${contract.routes} is not a list of "METHOD /path" rows`);
    }
    return rows;
}

/** The paths of a vendored route list, without the methods. */
export function vendoredPaths(contract: ApiContract): string[] {
    return [...new Set(vendoredRoutes(contract).map((row) => row.slice(row.indexOf(' ') + 1)))].sort();
}

/**
 * The route prefixes of the Enterprise features, as this repository keeps
 * them for the scan of the Community build.
 */
export function enterpriseRoutePrefixes(): string[] {
    const prefixes = getArray(readDenylist(), 'routePrefixes');
    if (prefixes === undefined || prefixes.length === 0 || !prefixes.every(isString)) {
        throw new Error('scripts/community-bundle-denylist.json has no routePrefixes list');
    }
    return prefixes;
}

/** True when `path` is `prefix` or under it. */
export function isUnder(path: string, prefix: string): boolean {
    return path === prefix || path.startsWith(prefix + '/');
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Every update the console sends is typed against the Control Plane's
 * request type, exactly.
 *
 * The Control Plane refuses an unknown field in the body of an admin PATCH
 * or PUT (422). A body typed by hand compiles whatever it holds, so a field
 * the Control Plane dropped fails on the screen that sends it and nowhere
 * before. `ApiBodyExactly<path, method, Sent>` (`lib/api/controlPlaneTypes`,
 * one per edition) is the contract's request type for the operation, and a
 * key of the value sent that the contract does not name fails to compile.
 *
 * This file holds every PATCH and PUT that sends a body to that type: the
 * function that issues the request names `ApiBodyExactly` with the path and
 * method of the route it calls, or, where it writes the body in place from
 * its own arguments, `satisfies ApiRequestBody` with the same two (an object
 * written in place is refused an unknown key by the plain type). The route is read from the vendored route
 * list of the edition under test.
 */

import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { apiContractUnderTest, vendoredRoutes } from '../../test/apiContract';
import { NOT_SOURCE_DIRS, REPO_ROOT, scanRoots } from '../../test/sourceRoots';

const isTest = (file: string): boolean => /\.(?:test|spec)\.tsx?$|[\\/]__tests__[\\/]|[\\/]src[\\/]test[\\/]|[\\/]generated[\\/]/.test(file);

function sourceFiles(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) return NOT_SOURCE_DIRS.has(entry.name) ? [] : sourceFiles(full);
        return /\.tsx?$/.test(entry.name) && !entry.name.endsWith('.d.ts') && !isTest(full) ? [full] : [];
    });
}

const ROUTES = vendoredRoutes(apiContractUnderTest());

/** The contract's path for a path as the code writes it (`${…}` for a parameter), or null. */
function contractPath(method: string, written: string): string | null {
    // A query string, literal or appended as a prebuilt `${query}`, is not part of the path.
    const segments = written
        .replace(/(?<!\/)\$\{.*\}$/, '')
        .split('?')[0]
        .replace(/\$\{[^}]*\}/g, '{}')
        .split('/');
    for (const row of ROUTES) {
        const [rowMethod, rowPath] = row.split(' ');
        if (rowMethod !== method) continue;
        const rowSegments = rowPath.split('/');
        if (rowSegments.length !== segments.length) continue;
        if (rowSegments.every((segment, i) => (/^\{.*\}$/.test(segment) ? segments[i] !== '' : segment === segments[i]))) return rowPath;
    }
    return null;
}

/** Where a function starts: a method of a class, a function, or a function held by a constant. */
const FUNCTION_START = /^\s*(?:(?:public|private|protected|static|export)\s+)*(?:async\s+\w+\s*[<(]|(?:async\s+)?function\s+\w+|(?:const|let)\s+\w+\s*=\s*(?:useCallback\()?\s*async\b)/;

interface UpdateSite {
    /** `file:line` of the `method:` option. */
    where: string;
    method: 'PATCH' | 'PUT';
    /** The path as written, or null when the request names none nearby. */
    written: string | null;
    /** The text of the function up to the request. */
    functionText: string;
}

/** Every Control Plane PATCH and PUT that sends a body. */
function updateSites(): UpdateSite[] {
    return scanRoots((root) => sourceFiles(root)).flatMap((file) => {
        const lines = readFileSync(file, 'utf8').split('\n');
        return lines.flatMap((line, at): UpdateSite[] => {
            const option = /method:\s*['"](PATCH|PUT)['"]/.exec(line);
            if (!option) return [];
            // The request's options: to the end of the call.
            const options = lines.slice(Math.max(0, at - 3), at + 8).join('\n');
            if (!/\bbody\b/.test(options)) return [];
            // The text below reaches past the `method:` line to the body.
            const end = at + 1 + lines.slice(at + 1, at + 8).findIndex((text) => /\bbody\b/.test(text));
            let start = at;
            while (start > 0 && !FUNCTION_START.test(lines[start])) start -= 1;
            const functionText = lines.slice(start, Math.max(at, end) + 1).join('\n');
            // A request to another service (an MCP registry instance, whose
            // own API is under /v0/) is not held to this contract.
            const request = lines.slice(Math.max(start, at - 8), at + 1).join('\n');
            if (/\bregistryFetch\(|\bfetch\(|[`'"]\/v0\//.test(request)) return [];
            let written: string | null = null;
            for (let i = at; i >= Math.max(start, at - 10) && written === null; i -= 1) {
                written = /[`'"](\/v1\/[^`'"]*)[`'"]/.exec(lines[i])?.[1] ?? null;
            }
            return [{ where: `${path.relative(REPO_ROOT, file).split(path.sep).join('/')}:${at + 1}`, method: option[1] as 'PATCH' | 'PUT', written, functionText }];
        });
    });
}

describe('the body of every update the console sends', () => {
    const sites = updateSites();

    it('is found: the PATCH and PUT requests of the client', () => {
        expect(sites.length).toBeGreaterThan(60);
    });

    it('goes to a route of the contract, read from the request itself', () => {
        const unread = sites.filter((site) => site.written === null || contractPath(site.method, site.written) === null).map((site) => `${site.where}: ${site.method} ${site.written ?? '(no path beside the request)'}`);
        expect(unread).toEqual([]);
    });

    it('is typed as the contract\'s request type of that route, with no key the contract does not name', () => {
        const untyped = sites.flatMap((site) => {
            const route = site.written === null ? null : contractPath(site.method, site.written);
            if (route === null) return [];
            const operation = `<'${route}', '${site.method.toLowerCase()}'`;
            const text = site.functionText.replace(/\s+/g, ' ');
            // The body the function takes, or the body it writes in place.
            const typed = text.includes(`ApiBodyExactly${operation}`) || text.includes(`satisfies ApiRequestBody${operation}>`);
            return typed ? [] : [`${site.where}: ${site.method} ${route}`];
        });
        expect(
            untyped,
            'Type the body the function takes as ApiBodyExactly<path, method, Sent>, or the body it writes in place with `satisfies ApiRequestBody<path, method>` (lib/api/controlPlaneTypes).',
        ).toEqual([]);
    });
});

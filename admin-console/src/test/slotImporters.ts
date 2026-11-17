/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Who imports a slotted piece: the scan of `app/__tests__/slots.test.tsx` and
 * of its Enterprise half under `enterprise/src/host/` ("a slotted piece
 * reaches a page only through its slot"). Reads the trees as text
 * (`sourceRoots`); loads nothing from them.
 */

import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { NOT_SOURCE_DIRS, importedPaths, sourceName } from './sourceRoots';

/** The source files under `dir` that are not tests. */
export function sources(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) return entry.name === '__tests__' || NOT_SOURCE_DIRS.has(entry.name) ? [] : sources(full);
        return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : [];
    });
}

/** Files that import `target` (a path under src, without extension). */
export function importers(files: string[], target: string): string[] {
    const hits: string[] = [];
    for (const file of files) {
        // `… from './x'`, `import('./x')` and the bare `import './x'`,
        // relative or through an alias of the Community tree.
        for (const imported of importedPaths(file, readFileSync(file, 'utf8'))) {
            if (sourceName(imported) === target) hits.push(sourceName(file));
        }
    }
    return hits;
}

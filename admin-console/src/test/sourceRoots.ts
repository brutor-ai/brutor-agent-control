/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where the console's source is, for the tests that read the tree as text
 * (the source-scan guards).
 *
 * There are two roots: `src/`, the Community
 * Edition, and `enterprise/` beside it, the Enterprise Edition. A house rule
 * holds in both, so a guard scans both: code under `enterprise/` is under
 * every rule that code under `src/` is.
 *
 * Which roots a run scans is the edition under test (`underTest.ts`), not
 * what is on the disk: the Community package scans `src/` whether or not
 * `enterprise/` is beside it, and the Enterprise package runs the same
 * guards again over both.
 *
 * A guard keeps its own idea of which files it reads (extensions, test
 * files, generated code); this module only says where to start, which
 * directories are never source, and how a file is named in a recorded list.
 */

import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';

import { editionUnderTest } from './underTest';

export const REPO_ROOT = path.resolve(__dirname, '../..');
export const SRC_ROOT = path.join(REPO_ROOT, 'src');
export const ENTERPRISE_ROOT = path.join(REPO_ROOT, 'enterprise');

/** Directory names that hold no source under any root: installs and build output. */
export const NOT_SOURCE_DIRS: ReadonlySet<string> = new Set(['node_modules', 'dist', 'coverage']);

/** The roots of the edition under test, Community first. */
export function sourceRoots(): string[] {
    return [SRC_ROOT, ...editionUnderTest().sourceRoots.map((dir) => path.join(REPO_ROOT, dir))];
}

/** True when the edition under test has the Enterprise tree: the Enterprise package's run. */
export function enterpriseUnderTest(): boolean {
    return sourceRoots().includes(ENTERPRISE_ROOT);
}

/** What `scan` finds under every root, Community first. */
export function scanRoots<T>(scan: (root: string) => T[]): T[] {
    return sourceRoots().flatMap((root) => scan(root));
}

/** True for a file or directory under `enterprise/`. */
export function isEnterprisePath(file: string): boolean {
    return file === ENTERPRISE_ROOT || file.startsWith(ENTERPRISE_ROOT + path.sep);
}

/**
 * The name a recorded list gives a file: its path under `src/` for a
 * Community file (`components/users/UserManagementTab.tsx`), and its path from the
 * repository root for anything else (`enterprise/src/extension.tsx`).
 * `src/` has no `enterprise` directory (`roots.test.ts`), so no two files
 * share a name.
 */
export function sourceName(file: string): string {
    const under = file === SRC_ROOT || file.startsWith(SRC_ROOT + path.sep) ? SRC_ROOT : REPO_ROOT;
    return path.relative(under, file).split(path.sep).join('/');
}

/** The file a recorded name stands for: the inverse of `sourceName`. */
export function sourceFile(name: string): string {
    return name === 'enterprise' || name.startsWith('enterprise/') ? path.join(REPO_ROOT, name) : path.join(SRC_ROOT, name);
}

/**
 * The module specifiers a source text imports: `… from 'x'` (imports and
 * re-exports), `import('x')` and the bare `import 'x'`.
 */
export function importSpecifiers(text: string): string[] {
    const found: string[] = [];
    for (const match of text.matchAll(/from\s+['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)|import\s+['"]([^'"]+)['"]/g)) {
        found.push(match[1] ?? match[2] ?? match[3]);
    }
    return found;
}

/**
 * The path a specifier written in `fromFile` points at, without a `.ts` or
 * `.tsx` extension: a relative one against the file, `@community/…` and
 * `@/…` (the aliases of `tsconfig.json`) against `src/`. Null for a package.
 */
export function resolveSpecifier(fromFile: string, spec: string): string | null {
    const strip = (p: string): string => p.replace(/\.tsx?$/, '');
    if (spec.startsWith('./') || spec.startsWith('../')) return strip(path.resolve(path.dirname(fromFile), spec));
    for (const alias of ['@community/', '@/']) {
        if (spec.startsWith(alias)) return strip(path.join(SRC_ROOT, spec.slice(alias.length)));
    }
    return null;
}

/** The files `fromFile`'s text imports from the trees, resolved (no extension). */
export function importedPaths(fromFile: string, text: string): string[] {
    return importSpecifiers(text).flatMap((spec) => {
        const resolved = resolveSpecifier(fromFile, spec);
        return resolved === null ? [] : [resolved];
    });
}

/**
 * The files the API client is written in: the facade, then every file of
 * `src/lib/gatewayApi` (the base, the registry and the Community modules),
 * then the module of each Enterprise feature, which is in that feature's
 * directory (`enterprise/src/features/<feature>/lib/gatewayApi`).
 * For the guards that read the whole client as text: a module
 * under `enterprise/` is read too. A tree without `enterprise/`
 * has the Community files alone.
 */
export function apiClientFiles(): string[] {
    const tsFiles = (dir: string): string[] =>
        existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.ts')).sort().map((f) => path.join(dir, f)) : [];
    const features = path.join(ENTERPRISE_ROOT, 'src', 'features');
    const enterprise = (enterpriseUnderTest() && existsSync(features) ? readdirSync(features).sort() : []).flatMap((feature) =>
        tsFiles(path.join(features, feature, 'lib', 'gatewayApi')),
    );
    return [path.join(SRC_ROOT, 'lib', 'gatewayApiClient.ts'), ...tsFiles(path.join(SRC_ROOT, 'lib', 'gatewayApi')), ...enterprise];
}

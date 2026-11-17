/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Every package `src/` imports at runtime must be a runtime dependency.
 *
 * A package imported under `src/` (`react-hot-toast`, say, which
 * `src/index.tsx` imports) but declared in `devDependencies` fails nothing
 * locally: the Dockerfile runs `npm ci` and CI runs
 * `npm ci --legacy-peer-deps`, and both install dev dependencies. The break
 * only appears in a production install (`npm ci --omit=dev`), which is the
 * one place nobody runs by hand — so the failure would surface first in
 * whatever pipeline hardens its install, long after the change that caused it.
 *
 * The reverse direction is checked too: a package declared as a runtime
 * dependency that nothing imports is dead weight in the image (and, for
 * `react-router-dom`, a misleading signal that the console routes with a
 * router — it does not; it switches on an `activeTab` string).
 */

import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { NOT_SOURCE_DIRS, SRC_ROOT, sourceRoots } from '../../test/sourceRoots'

const ROOT = resolve(__dirname, '../../..')

interface Manifest {
    dependencies?: Record<string, string>
    devDependencies?: Record<string, string>
}

/**
 * The packages of the edition under test: the
 * console's own, whose source is `src/`, and in the Enterprise package's run
 * that one too, whose source is `enterprise/src`. Each is held to its own
 * manifest: what its source imports is a runtime dependency it declares
 * itself, and it declares none it does not import.
 */
const PACKAGES = sourceRoots().map((root) => {
    const dir = root === SRC_ROOT ? ROOT : root
    return {
        name: root === SRC_ROOT ? 'src/' : 'enterprise/src/',
        source: root === SRC_ROOT ? root : join(root, 'src'),
        pkg: JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8')) as Manifest,
    }
})

/** Bare-specifier imports, i.e. packages rather than relative paths. */
const IMPORT_RE = /(?:from\s+|import\s*\(\s*)['"]([^'".][^'"]*)['"]/g

/** `@scope/name` keeps two segments; everything else keeps one. */
function packageOf(spec: string): string {
    const parts = spec.split('/')
    return spec.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0]
}

function sourceFiles(dir: string, out: string[] = []): string[] {
    for (const entry of readdirSync(dir)) {
        const full = join(dir, entry)
        if (statSync(full).isDirectory()) {
            // Test files may legitimately import dev-only packages.
            if (entry === '__tests__' || entry === 'test' || NOT_SOURCE_DIRS.has(entry)) continue
            sourceFiles(full, out)
        } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
            out.push(full)
        }
    }
    return out
}

function importedPackages(source: string): Set<string> {
    const found = new Set<string>()
    for (const file of sourceFiles(source)) {
        const src = readFileSync(file, 'utf8')
        for (const m of src.matchAll(IMPORT_RE)) {
            found.add(packageOf(m[1]))
        }
    }
    return found
}

// Packages resolved by the bundler or the runtime rather than by an import
// this scan can see, or intentionally kept without a direct import.
const ALLOWED_UNIMPORTED = new Set<string>([
    // A peer of `recharts`, which imports it. The image installs with
    // `--legacy-peer-deps`, which skips peers, so it has to be declared here
    // or the Community image cannot build.
    'react-is',
])

describe('dependency classification', () => {
    it('every package imported by src/ is a runtime dependency', () => {
        for (const { name, source, pkg } of PACKAGES) {
            const deps = new Set(Object.keys(pkg.dependencies ?? {}))
            const devDeps = new Set(Object.keys(pkg.devDependencies ?? {}))
            const misplaced = [...importedPackages(source)]
                .filter(p => devDeps.has(p) && !deps.has(p))
                .sort()

            expect(
                misplaced,
                `${misplaced.join(', ')} imported by ${name} but declared only in ` +
                `devDependencies. A production install (npm ci --omit=dev) cannot ` +
                `resolve them; local builds and CI both install dev deps, so this ` +
                `stays invisible until an image build hardens its install.`
            ).toEqual([])
        }
    })

    it('runtime dependencies are actually imported', () => {
        for (const { name, source, pkg } of PACKAGES) {
            const imported = importedPackages(source)
            const unused = Object.keys(pkg.dependencies ?? {})
                .filter(p => !imported.has(p) && !ALLOWED_UNIMPORTED.has(p))
                .sort()

            expect(
                unused,
                `${unused.join(', ')} declared as runtime dependencies but never ` +
                `imported under ${name}. Either drop them, or add them to ` +
                `ALLOWED_UNIMPORTED with the reason they are resolved indirectly.`
            ).toEqual([])
        }
    })
})

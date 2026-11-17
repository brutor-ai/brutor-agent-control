/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The boundary between the two trees: `src/` is the
 * Community Edition and stands without `enterprise/` beside it.
 *
 * Nothing under `src/` loads anything from `enterprise/`: not the
 * application, not a test, not a test helper. There is no list of
 * exceptions. The ESLint rule of the same name
 * (`eslint.config.js`, `community/no-enterprise-imports`) refuses such an
 * import as it is written, and `editionBoundary.lint.test.ts` holds that
 * rule to this line.
 *
 * A "load" is any module path written in a file that leads into
 * `enterprise/`: an import, a re-export, a dynamic import, an
 * `import.meta.glob` pattern, a `vi.mock` target. A guard that reads the
 * Enterprise tree as text in the Enterprise package's run
 * (`test/sourceRoots.ts`) loads nothing from it.
 *
 * The second block holds the Enterprise tree to its layout, which the build
 * and test configuration assume; it has something to read in the Enterprise
 * package's run of this file.
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import { ENTERPRISE_ROOT, NOT_SOURCE_DIRS, REPO_ROOT, SRC_ROOT, enterpriseUnderTest, isEnterprisePath, sourceName } from '../../test/sourceRoots';


function files(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) return NOT_SOURCE_DIRS.has(entry.name) ? [] : files(full);
        return /\.(tsx?|jsx?|mjs|cjs|css)$/.test(entry.name) ? [full] : [];
    });
}

/** The text of every string in `text`, read by the TypeScript parser: a path in a comment is not one. */
function strings(file: string, text: string): string[] {
    const kind = /\.(tsx|jsx)$/.test(file) ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, false, kind);
    const out: string[] = [];
    const visit = (node: ts.Node): void => {
        if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || node.kind === ts.SyntaxKind.TemplateHead) {
            out.push((node as ts.LiteralLikeNode).text);
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
    return out;
}

/**
 * The paths into `enterprise/` that `text`, a file at `file`, names: every
 * string that is a relative path, or a path through an alias of the
 * Enterprise tree, and resolves under `enterprise/`.
 */
export function loadsFromEnterprise(file: string, text: string): string[] {
    // Cheap first: a file that never says the word names no such path.
    if (!text.includes('enterprise')) return [];
    if (file.endsWith('.css')) return [...text.matchAll(/(?:url\(|@import\s+)\s*['"]?([^'")\s]+)/g)].map((m) => m[1]).filter((spec) => leadsIntoEnterprise(file, spec));
    return strings(file, text).filter((spec) => leadsIntoEnterprise(file, spec));
}

function leadsIntoEnterprise(file: string, spec: string): boolean {
    if (spec.startsWith('@enterprise/')) return true;
    if (!spec.startsWith('./') && !spec.startsWith('../')) return false;
    return isEnterprisePath(path.resolve(path.dirname(file), spec));
}

describe('src/ loads nothing from enterprise/', () => {
    // This file is not read: the sample paths of its own first test are such strings.
    const self = path.join(SRC_ROOT, 'app', '__tests__', 'editionBoundary.test.ts');
    const all = files(SRC_ROOT).filter((file) => file !== self);
    const loading: Record<string, string[]> = {};
    for (const file of all) {
        const loads = loadsFromEnterprise(file, readFileSync(file, 'utf8'));
        if (loads.length) loading[sourceName(file)] = loads;
    }

    it('the scan sees every way a file can load a module, and not a path in a comment', () => {
        const file = path.join(SRC_ROOT, 'app', 'x.ts');
        const text = [
            "import a from '../../enterprise/src/a';",
            "export { b } from '../../enterprise/src/b';",
            "const c = import('../../enterprise/src/c');",
            "const d = import.meta.glob('../../enterprise/src/*.tsx', { eager: true });",
            'vi.mock("../../enterprise/src/e", () => ({}));',
            'const f = require(`../../enterprise/src/f`);',
            "import g from '@enterprise/src/g';",
            "// import h from '../../enterprise/src/h';",
            "/* import i from '../../enterprise/src/i'; */",
            "import j from '../enterprise-notes/j';",
            "import k from './enterprise';",
            "const name = 'enterprise/src/slots.tsx';",
        ].join('\n');
        expect(loadsFromEnterprise(file, text)).toEqual([
            '../../enterprise/src/a',
            '../../enterprise/src/b',
            '../../enterprise/src/c',
            '../../enterprise/src/*.tsx',
            '../../enterprise/src/e',
            '../../enterprise/src/f',
            '@enterprise/src/g',
        ]);
        expect(all.length).toBeGreaterThan(900);
    });

    it('no file under src/ loads from enterprise/: no application file, no test, no test helper', () => {
        expect(loading).toEqual({});
    });

    it('the entry point mounts the console with no extension, and imports nothing but the mount and the styles', () => {
        // Nothing else tests `index.tsx`: it renders into the page when it is imported.
        const entry = readFileSync(path.join(SRC_ROOT, 'index.tsx'), 'utf8');
        const code = entry.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
        expect([...code.matchAll(/^import .*$/gm)].map((m) => m[0])).toEqual([
            "import { mountConsole } from './app/mount';",
            "import './styles/globals.css';",
        ]);
        expect(code.match(/mountConsole\([^)]*\)/g)).toEqual(['mountConsole()']);
        expect(code).not.toMatch(/extension/i);
    });

    it('the mount renders the console once, with the extensions it is given and none by default', () => {
        const mount = readFileSync(path.join(SRC_ROOT, 'app', 'mount.tsx'), 'utf8').replace(/\{\/\*[\s\S]*?\*\/\}/g, '');
        expect(mount).toMatch(/export function mountConsole\(extensions: readonly AdminExtension\[\] = \[\]\): void \{/);
        expect(mount.match(/<App\b[^>]*>/g)).toEqual(['<App extensions={extensions} />']);
    });
});

describe('the Enterprise tree is laid out as the configuration assumes', () => {
    // tsconfig, ESLint, vitest, Tailwind and the Docker context name
    // `enterprise/src` for source and `enterprise/src/test` for test helpers;
    // the recorded file lists name an Enterprise file from the repository
    // root (`enterprise/src/…`) and a Community one from `src/`.
    it('src/ has no directory named enterprise, so no recorded name is ambiguous', () => {
        expect(existsSync(path.join(SRC_ROOT, 'enterprise'))).toBe(false);
    });

    it('every source file of enterprise/ is under enterprise/src', () => {
        // Read where the Enterprise tree is under test: the Enterprise package's run.
        if (!enterpriseUnderTest()) return;
        const outside = files(ENTERPRISE_ROOT)
            .map((file) => path.relative(REPO_ROOT, file).split(path.sep).join('/'))
            // Beside `src`, the package root holds configuration files only.
            .filter((name) => !name.startsWith('enterprise/src/') && !/^enterprise\/[^/]+\.config\.(ts|js|mjs|cjs)$/.test(name));
        expect(outside).toEqual([]);
    });
});

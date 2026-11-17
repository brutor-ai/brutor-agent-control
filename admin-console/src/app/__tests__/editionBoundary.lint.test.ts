/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The ESLint rule of the edition boundary (`eslint.config.js`, the block named `community/no-enterprise-imports`):
 * a file under `src/` that loads from `enterprise/` is a lint error where it
 * is written, with no allow-list.
 *
 * The rule is run here as the configuration defines it, on text: every way a
 * file can load a module is refused, a path that only looks alike is not,
 * and the configuration has it switched on, as an error, for every file
 * under `src/`. `editionBoundary.test.ts` holds the tree itself to the line.
 */

import path from 'node:path';

import { ESLint, type Linter } from 'eslint';
import tseslint from 'typescript-eslint';
import { describe, expect, it } from 'vitest';

import config, { COMMUNITY_ONLY_BOUNDARY } from '../../../eslint.config.js';
import { REPO_ROOT } from '../../test/sourceRoots';

const blocks = config as Linter.Config[];
const boundary = blocks.filter((block) => block.name === COMMUNITY_ONLY_BOUNDARY);

/** The boundary block alone, with the TypeScript parser: no type information is needed to read an import. */
const eslint = new ESLint({
    cwd: REPO_ROOT,
    overrideConfigFile: true,
    overrideConfig: [{ files: ['**/*.{ts,tsx}'], languageOptions: { parser: tseslint.parser } }, ...boundary],
});

async function refused(text: string, file = 'src/app/x.ts'): Promise<string[]> {
    const [result] = await eslint.lintText(text, { filePath: path.join(REPO_ROOT, file) });
    return result.messages.map((message) => `${message.line}: ${message.ruleId} (${message.severity === 2 ? 'error' : 'warning'})`);
}

describe('the lint rule of the edition boundary', () => {
    it('is one block of the configuration, for every file under src/', () => {
        expect(boundary).toHaveLength(1);
        expect(boundary[0].files).toEqual(['src/**/*.{ts,tsx,js,jsx,mjs,cjs}', '*.config.{ts,js,mjs,cjs}']);
        expect(boundary[0].ignores).toBeUndefined();
    });

    it('refuses every way a file under src/ can load a module from enterprise/', async () => {
        const text = [
            "import a from '../../enterprise/src/a';",
            "import type { B } from '../../enterprise/src/b';",
            "export { c } from '../../enterprise/src/c';",
            "export * from '../../enterprise/src/d';",
            "const e = import('../../enterprise/src/e');",
            "const f = import.meta.glob('../../enterprise/src/*.tsx', { eager: true });",
            "vi.mock('../../enterprise/src/g', () => ({}));",
            'const h = require(`../../enterprise/src/h`);',
            "import i from '@enterprise/src/i';",
            "import j from '../../enterprise';",
            "vi.doMock('../../enterprise/src/k');",
        ].join('\n');
        expect(await refused(text)).toEqual([
            '1: no-restricted-imports (error)',
            '2: no-restricted-imports (error)',
            '3: no-restricted-imports (error)',
            '4: no-restricted-imports (error)',
            '5: no-restricted-syntax (error)',
            '6: no-restricted-syntax (error)',
            '7: no-restricted-syntax (error)',
            '8: no-restricted-syntax (error)',
            '9: no-restricted-imports (error)',
            '10: no-restricted-imports (error)',
            '11: no-restricted-syntax (error)',
        ]);
    });

    it('refuses it in a test and a test helper as in the application', async () => {
        const text = "import a from '../../enterprise/src/a';";
        expect(await refused(text, 'src/app/x.test.tsx')).toEqual(['1: no-restricted-imports (error)']);
        expect(await refused(text, 'src/app/__tests__/x.test.tsx')).toEqual(['1: no-restricted-imports (error)']);
        expect(await refused("import a from '../../enterprise/src/a';", 'src/test/x.ts')).toEqual(['1: no-restricted-imports (error)']);
    });

    it('refuses a package only the Enterprise package declares, which one install would otherwise resolve here', async () => {
        const text = [
            "import Editor from '@monaco-editor/react';",
            "import * as monaco from 'monaco-editor';",
            "import worker from 'monaco-editor/editor/editor.worker.js?worker';",
            "import Markdown from 'react-markdown';",
            "import gfm from 'remark-gfm';",
            "import JavaScriptObfuscator from 'javascript-obfuscator';",
            "import x from 'react-markdown-preview';",
            "import y from 'react';",
        ].join('\n');
        expect(await refused(text)).toEqual([1, 2, 3, 4, 5, 6].map((line) => `${line}: no-restricted-imports (error)`));
        // In this package's build configuration too: the obfuscator is the Enterprise build's.
        expect(await refused("import JavaScriptObfuscator from 'javascript-obfuscator';", 'vite.config.ts')).toEqual(['1: no-restricted-imports (error)']);
    });

    it('does not refuse a path that only looks alike, or a path in a comment or an ordinary string', async () => {
        const text = [
            "import a from '../enterprise-notes/a';",
            "import b from './enterprise';",
            "import c from '../lib/enterpriseFeatures';",
            "import d from 'enterprise';",
            "// import e from '../../enterprise/src/e';",
            "const name = 'enterprise/src/slots.tsx';",
            "const f = import('../lib/f');",
        ].join('\n');
        expect(await refused(text)).toEqual([]);
    });

    it('is on, as an error, for the files of src/ in the configuration as a whole, and nothing after it switches it off', async () => {
        const whole = new ESLint({ cwd: REPO_ROOT });
        for (const file of ['vite.config.ts', 'src/index.tsx', 'src/app/extensions.ts', 'src/app/__tests__/editionBoundary.test.ts', 'src/test/sourceRoots.ts']) {
            const rules = ((await whole.calculateConfigForFile(path.join(REPO_ROOT, file))) as { rules: Record<string, unknown[]> }).rules;
            expect({ file, imports: rules['no-restricted-imports']?.[0], syntax: rules['no-restricted-syntax']?.[0] }).toEqual({ file, imports: 2, syntax: 2 });
            expect((rules['no-restricted-syntax'] ?? []).length).toBeGreaterThan(5);
        }
    });
});

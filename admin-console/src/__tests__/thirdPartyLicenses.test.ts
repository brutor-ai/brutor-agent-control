/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The third-party licences file and its guard. Third-party packages ship
 * unmodified with their licence texts, in a generated file in each image and
 * in the repository.
 *
 * `scripts/third-party-licenses.mjs` is the generator: a Vite plugin that
 * reads the packages of the production build's modules, checks each one's
 * licence against the one allowed list (`scripts/third-party-licenses.json`)
 * and writes `THIRD_PARTY_LICENSES` into the build. These tests hold the
 * guard (an unknown or a disallowed licence fails), the dompurify election,
 * the format, the committed file against package-lock.json, and the two
 * Dockerfiles to the image paths.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import {
    licenceProblems,
    packageDirOf,
    readPolicy,
    renderLicences,
    resolveLicence,
} from '../../scripts/third-party-licenses.mjs';

const ROOT = path.resolve(__dirname, '../..');
const policy = readPolicy(path.join(ROOT, 'scripts/third-party-licenses.json'));

describe('the allowed list', () => {
    it('is written once, in scripts/third-party-licenses.json', () => {
        expect(policy.allowed).toContain('MIT');
        expect(policy.allowed).toContain('Apache-2.0');
        expect(new Set(policy.allowed).size).toBe(policy.allowed.length);
    });

    it('does not allow MPL-2.0: dompurify is taken under Apache-2.0', () => {
        expect(policy.allowed).not.toContain('MPL-2.0');
    });
});

describe('resolveLicence', () => {
    it('accepts an allowed licence', () => {
        expect(resolveLicence('example', 'MIT', policy)).toEqual({ licence: 'MIT', problems: [] });
    });

    it('refuses a package with no licence, UNLICENSED or a SEE LICENSE IN pointer', () => {
        for (const declared of [undefined, '', 'UNLICENSED', 'SEE LICENSE IN LICENSE.txt']) {
            const { problems } = resolveLicence('example', declared, policy);
            expect(problems).toHaveLength(1);
            expect(problems[0]).toMatch(/^example: (no licence declared|unknown licence)/);
        }
    });

    it('refuses an identifier that is not on the allowed list', () => {
        expect(resolveLicence('example', 'GPL-3.0-only', policy).problems).toEqual([
            'example: GPL-3.0-only is not on the allowed list (scripts/third-party-licenses.json)',
        ]);
        expect(resolveLicence('example', 'MPL-2.0', policy).problems).toHaveLength(1);
    });

    it('refuses a malformed expression', () => {
        expect(resolveLicence('example', '(MIT OR', policy).problems[0]).toMatch(/unknown licence/);
    });

    it('needs every part of an AND', () => {
        expect(resolveLicence('example', '(MIT AND Apache-2.0)', policy).licence).toBe('MIT AND Apache-2.0');
        expect(resolveLicence('example', '(MIT AND GPL-2.0-only)', policy).problems).toHaveLength(1);
    });

    it('takes the first allowed alternative of an OR', () => {
        expect(resolveLicence('example', '(MIT OR Apache-2.0)', policy).licence).toBe('MIT');
        expect(resolveLicence('example', '(GPL-2.0-only OR MIT)', policy).licence).toBe('MIT');
        expect(resolveLicence('example', '(GPL-2.0-only OR LGPL-3.0-only)', policy).problems).toHaveLength(1);
    });

    it('takes dompurify under Apache-2.0, the licence chosen of its two', () => {
        expect(resolveLicence('dompurify', '(MPL-2.0 OR Apache-2.0)', policy)).toEqual({
            licence: 'Apache-2.0',
            problems: [],
        });
        // The election holds even if MPL-2.0 were allowed one day.
        const wider = { ...policy, allowed: ['MPL-2.0', ...policy.allowed] };
        expect(resolveLicence('dompurify', '(MPL-2.0 OR Apache-2.0)', wider).licence).toBe('Apache-2.0');
    });

    it('refuses an election the package does not offer', () => {
        expect(resolveLicence('dompurify', 'MPL-2.0', policy).problems[0]).toMatch(/elected Apache-2.0/);
    });
});

describe('packageDirOf', () => {
    it('finds the innermost package of a module', () => {
        expect(packageDirOf('/r/node_modules/a/node_modules/@s/b/dist/x.js?commonjs-proxy')).toBe('/r/node_modules/a/node_modules/@s/b');
        expect(packageDirOf('\0/r/node_modules/react/index.js?commonjs-es-import')).toBe('/r/node_modules/react');
        expect(packageDirOf('/r/src/App.tsx')).toBeUndefined();
    });
});

describe('renderLicences', () => {
    const packages = [
        { name: 'b', version: '2.0.0', declared: 'MIT', licence: 'MIT', text: 'same text' },
        { name: 'a', version: '1.0.0', declared: '(MIT OR Apache-2.0)', licence: 'MIT', text: 'same text' },
        { name: 'c', version: '3.0.0', declared: 'ISC', licence: 'ISC', text: 'other text' },
    ];
    const text = renderLicences({ title: 'T (E)', command: 'npm run x', packages });

    it('lists each package with its version and licence, sorted, then each text once', () => {
        expect(text).toContain('Third-party licences: T (E)\n');
        expect(text).toContain('Regenerate: npm run x.\n');
        expect(text).toContain('Dependencies (3)\na 1.0.0 MIT [offered as (MIT OR Apache-2.0)]\nb 2.0.0 MIT\nc 3.0.0 ISC\n');
        expect(text.match(/same text/g)).toHaveLength(1);
        expect(text).toContain('MIT\nUsed by: a 1.0.0, b 2.0.0\n');
        expect(text).toContain('ISC\nUsed by: c 3.0.0\n');
    });

    it('is the same for the same packages in any order', () => {
        expect(renderLicences({ title: 'T (E)', command: 'npm run x', packages: [...packages].reverse() })).toBe(text);
    });
});

describe('the full text of a licence', () => {
    const fullTexts = [{ id: 'Apache-2.0', marker: 'TERMS AND CONDITIONS', text: 'Apache License\nTERMS AND CONDITIONS ...' }];

    it('is added once when a package under it carries only its notice', () => {
        const text = renderLicences({
            title: 'T (E)',
            command: 'npm run x',
            fullTexts,
            packages: [
                { name: 'n', version: '1.0.0', declared: 'Apache-2.0', licence: 'Apache-2.0', text: 'Licensed under the Apache License, Version 2.0' },
                { name: 'm', version: '1.0.0', declared: 'MIT', licence: 'MIT', text: 'mit' },
            ],
        });
        expect(text).toContain('Apache-2.0: the full text of the licence (n 1.0.0 carry its notice only)\nUsed by: n 1.0.0\n');
        expect(text.match(/TERMS AND CONDITIONS/g)).toHaveLength(1);
    });

    it('is not added when every package under it carries it', () => {
        const text = renderLicences({
            title: 'T (E)',
            command: 'npm run x',
            fullTexts,
            packages: [{ name: 'n', version: '1.0.0', declared: 'Apache-2.0', licence: 'Apache-2.0', text: 'Apache License\nTERMS AND CONDITIONS' }],
        });
        expect(text).not.toContain('the full text of the licence');
    });

    it('is the policy\'s Apache-2.0 text, kept beside it', () => {
        expect(policy.fullTexts['Apache-2.0'].file).toBe('licence-texts/Apache-2.0.txt');
        expect(readFileSync(path.join(ROOT, 'scripts', policy.fullTexts['Apache-2.0'].file), 'utf8')).toContain(policy.fullTexts['Apache-2.0'].marker);
    });
});

describe('licenceProblems', () => {
    it('collects every problem, so one build names them all', () => {
        expect(
            licenceProblems(
                [
                    { name: 'x', version: '1.0.0', declared: 'GPL-3.0-only', text: 't' },
                    { name: 'y', version: '1.0.0', declared: 'MIT', text: '' },
                    { name: 'z', version: '1.0.0', declared: 'MIT', text: 't' },
                ],
                policy,
            ).problems,
        ).toEqual([
            'x: GPL-3.0-only is not on the allowed list (scripts/third-party-licenses.json)',
            'y 1.0.0: no licence file in the package',
        ]);
    });
});

/** The `name version` pairs of a committed THIRD_PARTY_LICENSES. */
function listed(file: string): [string, string][] {
    const text = readFileSync(file, 'utf8');
    const block = text.split(/\nDependencies \(\d+\)\n/)[1].split('\n\n')[0];
    return block.split('\n').map((line) => line.split(' ').slice(0, 2) as [string, string]);
}

const lock = JSON.parse(readFileSync(path.join(ROOT, 'package-lock.json'), 'utf8')) as {
    packages: Record<string, { version?: string }>;
};
const locked = new Set(
    Object.entries(lock.packages).map(([where, entry]) => `${where.replace(/^.*node_modules\//, '')} ${entry.version}`),
);

describe.each([
    ['Community', 'THIRD_PARTY_LICENSES', 'dist/THIRD_PARTY_LICENSES'],
    ['Enterprise', 'enterprise/THIRD_PARTY_LICENSES', 'enterprise/dist/THIRD_PARTY_LICENSES'],
])('the committed THIRD_PARTY_LICENSES (%s)', (_edition, committed, built) => {
    const file = path.join(ROOT, committed);
    const present = existsSync(file); // a Community tree alone has no enterprise/

    it('lists packages at the version package-lock.json records (a copy inside another package excepted)', () => {
        if (!present) return;
        const pairs = listed(file).filter(([name]) => name !== 'dompurify');
        expect(pairs.length).toBeGreaterThan(10);
        expect(pairs.filter(([name, version]) => !locked.has(`${name} ${version}`))).toEqual([]);
    });

    it('lists React, the bundler helpers (vite) and Tailwind, and nothing of Brutor', () => {
        if (!present) return;
        const names = listed(file).map(([name]) => name);
        expect(names).toEqual(expect.arrayContaining(['react', 'vite', 'tailwindcss']));
        expect(names.some((name) => name.startsWith('brutor-'))).toBe(false);
    });

    it('matches the build, when there is one', () => {
        const output = path.join(ROOT, built);
        if (!present || !existsSync(output)) return;
        expect(readFileSync(output, 'utf8')).toBe(readFileSync(file, 'utf8'));
    });
});

describe('DOMPurify, of which monaco-editor carries a copy', () => {
    it('is not in the Community build, which has no editor', () => {
        expect(readFileSync(path.join(ROOT, 'THIRD_PARTY_LICENSES'), 'utf8')).not.toMatch(/^dompurify /m);
    });

    // A Community tree alone has no enterprise/.
    const file = path.join(ROOT, 'enterprise/THIRD_PARTY_LICENSES');
    it.skipIf(!existsSync(file))('is in the Enterprise build under Apache-2.0, with the Apache-2.0 text and not the MPL-2.0 one', () => {
        const text = readFileSync(file, 'utf8');
        expect(text).toMatch(/^dompurify \S+ Apache-2\.0 \[offered as \(MPL-2\.0 OR Apache-2\.0\)\]$/m);
        expect(text).toMatch(/^Apache-2\.0\nUsed by: dompurify [^\n]*\n-+\n\s*Apache License\n\s*Version 2\.0, January 2004/m);
        expect(text).not.toContain('Mozilla Public License');
    });
});

describe.each([
    ['Community', 'Dockerfile', '/app/dist', 'THIRD_PARTY_LICENSES'],
    ['Enterprise', 'enterprise/Dockerfile', '/app/enterprise/dist', 'enterprise/THIRD_PARTY_LICENSES'],
])('the %s image', (_edition, dockerfile, dist, committed) => {
    const file = path.join(ROOT, dockerfile);
    const text = existsSync(file) ? readFileSync(file, 'utf8') : '';

    it('fails its build when the generated file differs from the committed one', () => {
        if (!text) return; // a Community tree alone has no enterprise/Dockerfile
        expect(text).toContain(`RUN node scripts/third-party-licenses.mjs check ${dist}/THIRD_PARTY_LICENSES ${committed}`);
    });

    it('ships the file at /usr/share/licenses/brutor/ and beside the application, and labels it', () => {
        if (!text) return;
        expect(text).toContain(`COPY --from=builder ${dist}/THIRD_PARTY_LICENSES /usr/share/licenses/brutor/THIRD_PARTY_LICENSES`);
        expect(text).toContain(`COPY --from=builder ${dist} /usr/share/nginx/html`);
        expect(text).toContain('LABEL ai.brutor.third-party-licenses="/usr/share/licenses/brutor/THIRD_PARTY_LICENSES"');
    });
});

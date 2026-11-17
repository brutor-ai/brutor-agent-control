/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The images and the CI install exactly what `package-lock.json` records,
 * with npm's own check of peer dependencies: `npm ci` and no flag that
 * switches the check off. A lock file that does not install that way is
 * repaired, not installed around.
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { findWorkflows } from '../test/workflows';

const ROOT = path.resolve(__dirname, '..', '..');

/** The image of each package in this tree: a Community tree has no enterprise/. */
const IMAGES = ['Dockerfile', 'enterprise/Dockerfile'].filter((file) => existsSync(path.join(ROOT, file)));

/**
 * The files that install the dependencies, by their path from the root: the
 * images and the console's workflows, in this repository or at the root of
 * the public one (`../test/workflows.ts`).
 */
const INSTALLERS = [...IMAGES, ...findWorkflows(ROOT).mine.map((file) => path.relative(ROOT, file).split(path.sep).join('/'))];

/** The pages that tell a person how to install. */
const READMES = ['README.md', 'enterprise/README.md', 'docs/getting-started-guide.md'].filter((file) => existsSync(path.join(ROOT, file)));

/** The commands of a file, without its comments. */
const commands = (file: string) =>
    readFileSync(path.join(ROOT, file), 'utf8')
        .split('\n')
        .filter((line) => !line.trimStart().startsWith('#'))
        .join('\n');

describe('the install of the dependencies', () => {
    it.each(IMAGES)('is `npm ci` in %s', (file) => {
        expect(commands(file)).toMatch(/^RUN npm ci\b/m);
        expect(commands(file)).not.toMatch(/\bnpm (install|i)\b/);
    });

    it.each(INSTALLERS)('switches no peer-dependency check off in %s', (file) => {
        expect(commands(file)).not.toMatch(/--legacy-peer-deps|--force\b|legacy-peer-deps\s*=\s*true/);
    });

    it.each(READMES)('is not told to do so by %s either', (file) => {
        expect(readFileSync(path.join(ROOT, file), 'utf8')).not.toMatch(/--legacy-peer-deps/);
    });

    it('has no .npmrc that switches it off for everyone', () => {
        const npmrc = path.join(ROOT, '.npmrc');
        expect(existsSync(npmrc) ? readFileSync(npmrc, 'utf8') : '').not.toMatch(/legacy-peer-deps\s*=\s*true|force\s*=\s*true/);
    });
});

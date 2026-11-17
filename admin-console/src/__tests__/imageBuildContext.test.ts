/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the image build compiles is in the image's build context.
 *
 * The Dockerfile runs `tsc` over what `tsconfig.json` includes, in a context
 * that `.dockerignore` has thinned. A helper under `__tests__/` that is not
 * itself named `*.test.ts` is compiled; if it imports `src/test/…`, which the
 * context leaves out, the Community image does not build.
 *
 * Rule held here: every source file the build's program contains has all its
 * relative imports inside the build context.
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(__dirname, '..', '..');

/** A glob of the two files' dialect (`**`, `*`, plain directories) as a test on a repo-relative path. */
function matcher(pattern: string): (file: string) => boolean {
    const dir = pattern.replace(/\/\*\*$/, '').replace(/\/$/, '');
    const source = dir
        .split('**/')
        .map((part) => part.split('*').map((s) => s.replace(/[.+^${}()|[\]\\]/g, '\\$&')).join('[^/]*'))
        .join('(?:.*/)?');
    const exact = new RegExp(`^${source}$`);
    const under = new RegExp(`^${source}/`);
    return (file) => exact.test(file) || under.test(file);
}

function patterns(file: string, read: (text: string) => string[]): ((f: string) => boolean)[] {
    return read(readFileSync(path.join(ROOT, file), 'utf8')).map(matcher);
}

const leftOutOfTheProgram = patterns('tsconfig.json', (text) => {
    const config = JSON.parse(text) as { exclude?: string[] };
    return config.exclude ?? [];
});
const leftOutOfTheContext = patterns('.dockerignore', (text) =>
    text
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line !== '' && !line.startsWith('#') && !line.startsWith('!')),
);

function sources(dir: string, out: string[] = []): string[] {
    for (const name of readdirSync(dir)) {
        const full = path.join(dir, name);
        if (statSync(full).isDirectory()) sources(full, out);
        else if (/\.(ts|tsx)$/.test(name) && !name.endsWith('.d.ts')) out.push(path.relative(ROOT, full));
    }
    return out;
}

/** The file a relative import names, if it is one of the repository's. */
function resolved(from: string, specifier: string): string | null {
    const base = path.join(path.dirname(from), specifier);
    for (const tail of ['', '.ts', '.tsx', '/index.ts', '/index.tsx']) {
        if (existsSync(path.join(ROOT, base + tail)) && statSync(path.join(ROOT, base + tail)).isFile()) {
            return path.normalize(base + tail);
        }
    }
    return null;
}

describe('the Community image build', () => {
    const compiled = sources(path.join(ROOT, 'src')).filter((f) => !leftOutOfTheProgram.some((m) => m(f)));

    it('compiles something', () => {
        expect(compiled.length).toBeGreaterThan(300);
    });

    it('compiles no file the build context leaves out', () => {
        expect(compiled.filter((f) => leftOutOfTheContext.some((m) => m(f)))).toEqual([]);
    });

    it('compiles no file that imports one the build context leaves out', () => {
        const broken: string[] = [];
        for (const file of compiled) {
            const text = readFileSync(path.join(ROOT, file), 'utf8');
            for (const match of text.matchAll(/(?:from|import)\s*\(?\s*['"](\.{1,2}\/[^'"]+)['"]/g)) {
                const target = resolved(file, match[1]);
                if (target && leftOutOfTheContext.some((m) => m(target))) broken.push(`${file} -> ${target}`);
            }
        }
        expect(broken).toEqual([]);
    });
});

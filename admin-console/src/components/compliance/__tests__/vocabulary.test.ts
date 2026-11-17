/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Vocabulary discipline for the Compliance console.
 *
 * Display strings under `src/components/compliance/**` may not say
 * verified, certified, compliant, guaranteed, pass, approved or
 * tamper-proof. They say sealed, witnessed, timestamped, recomputed,
 * judged, met (n of m), not met, not evaluable, withheld, declared,
 * standalone, plural, self-witnessed.
 *
 * The check is a pragmatic scan of what a user can read: string literals
 * and JSX text, with comments stripped first. Identifiers are not
 * user-facing, and `\b` does not split `approved_by` or `statement.verified`
 * — but a property read inside a template literal (`${r.verified}`) would
 * match, so `${…}` expressions are dropped from template contents before
 * scanning. Test files are excluded.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const ROOT = resolve(__dirname, '..');

const BANNED = /\b(verified|certified|compliant|guaranteed|pass|approved|tamper-proof)\b/i;

function sourceFiles(dir: string, out: string[] = []): string[] {
    for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
            if (entry === '__tests__') continue;
            sourceFiles(full, out);
        } else if (/\.(ts|tsx)$/.test(entry)) {
            out.push(full);
        }
    }
    return out;
}

function stripComments(src: string): string {
    return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:\\])\/\/[^\n]*/g, '$1');
}

/** Every string a user could read: quoted literals, template chunks, JSX text. */
export function userFacingStrings(src: string): string[] {
    const out: string[] = [];
    const clean = stripComments(src);
    const literal = /'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`/g;
    for (const m of clean.matchAll(literal)) {
        const text = m[1] ?? m[2] ?? (m[3] ?? '').replace(/\$\{[^}]*\}/g, ' ');
        if (text) out.push(text);
    }
    // JSX text: anything between a closing `>` and the next `<` or `{`.
    for (const m of clean.matchAll(/>([^<>{}]+)</g)) {
        const text = m[1].trim();
        if (text) out.push(text);
    }
    return out;
}

describe('compliance vocabulary lint', () => {
    const files = sourceFiles(ROOT);

    it('scans the console', () => {
        expect(files.length).toBeGreaterThan(10);
    });

    it('no display string uses a banned word', () => {
        const offenders: string[] = [];
        for (const file of files) {
            for (const text of userFacingStrings(readFileSync(file, 'utf8'))) {
                const hit = BANNED.exec(text);
                if (hit) offenders.push(`${file.slice(ROOT.length + 1)}: "${text.slice(0, 80)}" (${hit[1]})`);
            }
        }
        const report = offenders.length
            ? `Display strings under components/compliance may not say verified / certified / ` +
              `compliant / guaranteed / pass / approved / tamper-proof. ` +
              `Say sealed, witnessed, timestamped, recomputed, judged, met, not met, ` +
              `not evaluable, withheld, declared instead.\n${offenders.join('\n')}`
            : '';
        expect(report).toBe('');
    });

    it('the scanner sees through identifiers but not through text', () => {
        expect(userFacingStrings("const x = a.verified; const y = 'approved_by';")).toEqual(['approved_by']);
        expect(BANNED.test('approved_by')).toBe(false);
        expect(BANNED.test('unverified against the instrument')).toBe(false);
        expect(BANNED.test('Recomputed: consistent')).toBe(false);
        expect(BANNED.test('the check will pass')).toBe(true);
        expect(userFacingStrings('<p>Tamper-proof ledger</p>')).toEqual(['Tamper-proof ledger']);
        expect(userFacingStrings('const s = `${r.verified} ok`;')).toEqual(['  ok']);
    });
});

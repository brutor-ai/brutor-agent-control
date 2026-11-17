/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The script in `index.html` that sets the theme before first paint.
 *
 * It runs before the bundle, so it cannot import the theme provider: it
 * reads the preference the provider stores, a JSON object
 * (`{"theme":"dark","fontSize":"medium"}`), and sets `data-theme` on
 * `<html>` when it is one of the themes the console offers (`AVAILABLE_THEMES`
 * in `hooks/useTheme.ts`). Anything else in storage, and nothing in storage,
 * is dark, the console's default.
 *
 * The two editions serve the same page under their own names
 * (`enterprisePackage.test.ts` holds them equal), so the script is read from
 * the Community page.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { runInNewContext } from 'node:vm';

import { describe, expect, it } from 'vitest';

import { AVAILABLE_THEMES } from '../hooks/useTheme';
import { REPO_ROOT } from '../test/sourceRoots';

const page = readFileSync(path.join(REPO_ROOT, 'index.html'), 'utf8');
const script = /<script>\s*(\(function\(\) \{\s*try \{\s*var raw = localStorage\.getItem\('admin-theme'\);[\s\S]*?\}\)\(\);)\s*<\/script>/.exec(page)?.[1];

/** Run the script against a storage holding `stored`; the `data-theme` it sets. */
function themeFor(stored: string | null | Error): string | undefined {
    const attributes: Record<string, string> = {};
    const localStorage = {
        getItem: (key: string) => {
            if (stored instanceof Error) throw stored;
            return key === 'admin-theme' ? stored : null;
        },
    };
    const document = { documentElement: { setAttribute: (name: string, value: string) => { attributes[name] = value; } } };
    // The page's own inline script, run with a storage and a document of the test's.
    runInNewContext(script ?? '', { localStorage, document });
    return attributes['data-theme'];
}

describe('the theme script of index.html', () => {
    it('is in the page, and knows the themes the console offers', () => {
        expect(script).toBeDefined();
        const named = [...(script ?? '').matchAll(/theme !== '([a-z-]+)'/g)].map((m) => m[1]);
        expect(named.sort()).toEqual([...AVAILABLE_THEMES].sort());
    });

    it.each([
        ['{"theme":"light","fontSize":"medium"}', 'light'],
        ['{"theme":"dark","fontSize":"small"}', 'dark'],
    ])('applies the theme of the stored preference %s', (stored, theme) => {
        expect(themeFor(stored)).toBe(theme);
    });

    it.each([
        ['nothing stored', null],
        ['a theme name on its own', 'light'],
        ['a quoted theme name', '"light"'],
        ['an object without a theme', '{"fontSize":"small"}'],
        ['a theme the console does not offer', '{"theme":"high-contrast"}'],
        ['a theme that does not exist', '{"theme":"sepia"}'],
        ['something that is not JSON', '{theme'],
        ['a JSON null', 'null'],
    ])('is dark for %s', (_what, stored) => {
        expect(themeFor(stored)).toBe('dark');
    });

    it('is dark when storage cannot be read', () => {
        expect(themeFor(new Error('blocked'))).toBe('dark');
    });
});

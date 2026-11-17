/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The global `<Toaster />` MUST be mounted at the app's root.
 *
 * Why:
 *     `react-hot-toast` writes to a portal that the library's
 *     `<Toaster />` component mounts on render. With no
 *     `<Toaster />` rendered anywhere in the React tree, every
 *     `toast.error(...)` / `toast.success(...)` /
 *     `toast.loading(...)` call across the admin UI writes to a
 *     portal that does not exist — the user sees nothing. A
 *     failed save, delete or import then looks like "I hit Save
 *     and nothing happens": the worst-case UX for a write that
 *     actually failed. One missing line, app-wide impact.
 *
 * What this test does:
 *     Static / syntactic check on `src/app/mount.tsx` to confirm
 *     it imports `Toaster` from `react-hot-toast` and renders
 *     `<Toaster ...>`. We don't render the live tree because
 *     `App.tsx` pulls in dozens of side-effecting modules
 *     (env config, auth, API client, …) that are heavy to set
 *     up here and unrelated to this invariant. The file-level
 *     check is tight: without the Toaster the test fails with a
 *     clear message.
 *
 *     The check intentionally targets the root render rather
 *     than "anywhere in the tree" because that's the only place
 *     a global Toaster belongs — putting it inside an individual
 *     tab unmounts it on tab change, defeating the whole
 *     point.
 */

import { readFileSync } from 'fs';
import { resolve } from 'path';

import { describe, expect, it } from 'vitest';

// The root render is the mount every entry point calls.
const INDEX_PATH = resolve(__dirname, '..', 'app', 'mount.tsx');

describe('global toast container', () => {
    const source = readFileSync(INDEX_PATH, 'utf-8');

    it('imports Toaster from react-hot-toast', () => {
        // Allow either named-import shape:
        //   import { Toaster } from 'react-hot-toast';
        //   import { Toaster, toast } from 'react-hot-toast';
        // Reject default-import-only shapes that don't pull
        // Toaster in.
        const importRegex =
            /import\s*\{[^}]*\bToaster\b[^}]*\}\s*from\s*['"]react-hot-toast['"]/;
        expect(source).toMatch(importRegex);
    });

    it('renders <Toaster /> in the app root', () => {
        // Match any `<Toaster ...>` JSX usage. The exact prop
        // configuration (position, z-index, durations) is
        // tunable — what matters is that a Toaster is present.
        // Without this, every `toast.error(...)` in the admin
        // UI silently no-ops.
        expect(source).toMatch(/<Toaster\b/);
    });

    it('mounts Toaster at app-root level (not nested inside a tab)', () => {
        // Heuristic: the Toaster lives in the same file that
        // owns `createRoot` / `root.render(...)`. Inside a single
        // tab component it would unmount on tab change and
        // toasts would go dark — so we assert co-location with
        // the root render.
        expect(source).toMatch(/createRoot|root\.render/);
        const hasToasterAndRender =
            /<Toaster\b/.test(source) && /root\.render\s*\(/.test(source);
        expect(hasToasterAndRender).toBe(true);
    });
});

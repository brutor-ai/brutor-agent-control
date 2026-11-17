/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
    plugins: [react()],
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
    test: {
        environment: 'happy-dom',
        globals: true,
        setupFiles: ['./src/test/setup.ts'],
        // vitest's 5 s per-test default is below what a loaded CI runner (or
        // a laptop running the trial stack) needs for a test with a few
        // async waits (asyncUtilTimeout is 5 s in setup.ts) or a whole-tree
        // scan like noImplementationReferences. These timed out under load
        // and passed alone.
        testTimeout: 15000,
        // The Community package's tests. The Enterprise package beside it,
        // where there is one, runs its own (enterprise/vitest.config.ts,
        // which extends this configuration).
        include: ['src/**/*.{test,spec}.{ts,tsx}'],
        coverage: {
            provider: 'v8',
            reporter: ['text', 'html', 'lcov'],
            // `src/components/**` is measured too: it is the bulk of this
            // app, and a number over lib and hooks alone would look far
            // healthier than the code is.
            include: ['src/lib/**', 'src/hooks/**', 'src/components/**'],
            exclude: ['src/test/**', 'src/lib/generated/**', '**/*.d.ts', '**/.DS_Store'],
            // Ratchet floor, same discipline as the lint `--max-warnings` cap:
            // pinned just under today's measured coverage so the number can
            // only go UP. Raise these when you add tests; never lower them to
            // make a build pass.
            // The floors are this package's measured values rounded down
            // (79.78 / 70.58 / 76.8 / 82.26).
            thresholds: {
                statements: 79,
                branches: 70,
                functions: 76,
                lines: 82,
            },
        },
    },
})

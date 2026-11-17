/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync } from 'fs'
import path from 'path'
import pkg from './package.json'
import { thirdPartyLicenses } from './scripts/third-party-licenses.mjs'

/**
 * The security headers of every response, read from the file nginx includes
 * (`nginx-security-headers.conf`), so that the development server and
 * `vite preview` send what the image sends. The response header is the only
 * place the console's Content-Security-Policy is said: the pages carry no
 * security `<meta>` tag (src/__tests__/securityHeaders.test.ts).
 */
export const securityHeaders: Record<string, string> = Object.fromEntries(
    [
        ...readFileSync(path.resolve(__dirname, 'nginx-security-headers.conf'), 'utf8').matchAll(
            /^\s*add_header\s+(\S+)\s+"([^"]*)"\s+always\s*;/gm,
        ),
    ].map((m) => [m[1], m[2]]),
)

// The Community build of the Admin Console. It is not
// obfuscated and has no Monaco chunk: the obfuscator and the policy editor
// are the Enterprise build's (enterprise/vite.config.ts, which extends this
// configuration).
export default defineConfig({
    define: {
        __APP_VERSION__: JSON.stringify(pkg.version),
    },
    plugins: [
        react(),
        // THIRD_PARTY_LICENSES in the build: every shipped package with its
        // licence and licence text; an unknown or disallowed licence fails
        // the build (scripts/third-party-licenses.mjs). The Enterprise build
        // extends this configuration and is named by its root.
        thirdPartyLicenses({
            lockDir: __dirname,
            policyFile: path.resolve(__dirname, 'scripts/third-party-licenses.json'),
            builds: {
                [__dirname]: {
                    title: 'Brutor Agent Control Admin Console (Community Edition)',
                    command: 'npm run build && npm run licenses:update',
                },
                [path.resolve(__dirname, 'enterprise')]: {
                    title: 'Brutor Agent Control Admin Console (Enterprise Edition)',
                    command: 'npm run enterprise:build && npm run enterprise:licenses:update',
                },
            },
        }),
    ],
    resolve: {
        alias: {
            '@': path.resolve(__dirname, './src'),
        },
    },
    server: {
        host: '0.0.0.0',
        port: 3002,
        headers: securityHeaders,
    },
    preview: {
        headers: securityHeaders,
    }
})
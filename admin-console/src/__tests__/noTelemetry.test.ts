/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console sends no product analytics or telemetry.
 *
 * A guard on the class, not on one vendor: no analytics SDK among the
 * dependencies, and no analytics host or SDK name in the source, the runtime
 * configuration the container writes, or the Content-Security-Policy —
 * a CSP exception for a tracker is how one comes back unnoticed.
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { ENTERPRISE_ROOT, NOT_SOURCE_DIRS, enterpriseUnderTest, scanRoots } from '../test/sourceRoots';

const ROOT = path.resolve(__dirname, '..', '..');
/** The packages of this tree: the console, and the Enterprise one when it has its own files. */
// The package of every edition under test: the console's own, and the Enterprise one in its run.
const PACKAGE_DIRS = enterpriseUnderTest() ? [ROOT, ENTERPRISE_ROOT] : [ROOT];
const TRACKERS =
    /posthog|mixpanel|amplitude|segment\.(com|io)|google-analytics|googletagmanager|gtag\(|plausible\.io|hotjar|fullstory|heap\.io|sentry\.io|datadoghq/i;

function sourceFiles(dir: string, out: string[] = []): string[] {
    for (const name of readdirSync(dir)) {
        const full = path.join(dir, name);
        if (statSync(full).isDirectory()) {
            if (!NOT_SOURCE_DIRS.has(name) && name !== 'generated') sourceFiles(full, out);
        } else if (/\.(ts|tsx|css|html)$/.test(name)) {
            out.push(full);
        }
    }
    return out;
}

describe('no telemetry', () => {
    it('no analytics SDK is a dependency', () => {
        const manifests = PACKAGE_DIRS.map((dir) => path.join(dir, 'package.json')).filter(
            (file) => file === path.join(ROOT, 'package.json') || existsSync(file),
        );
        const names = manifests.flatMap((file) => {
            const pkg = JSON.parse(readFileSync(file, 'utf8')) as {
                dependencies?: Record<string, string>;
                devDependencies?: Record<string, string>;
            };
            return Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });
        });
        expect(names.filter((n) => TRACKERS.test(n) || /analytics/i.test(n))).toEqual([]);
    });

    it('no analytics host or SDK is named in the source', () => {
        const self = path.join(ROOT, 'src', '__tests__', 'noTelemetry.test.ts');
        const hits = scanRoots((root) => sourceFiles(root))
            .filter((f) => f !== self)
            .filter((f) => TRACKERS.test(readFileSync(f, 'utf8')))
            .map((f) => path.relative(ROOT, f));
        expect(hits).toEqual([]);
    });

    it('the container configuration and the CSP name no analytics host', () => {
        const names = ['index.html', 'nginx.conf', 'docker-entrypoint.sh', 'vite.config.ts'];
        // Every one of the console's own; of the Enterprise package, the ones it has.
        const files = [
            ...names.map((f) => path.join(ROOT, f)),
            ...PACKAGE_DIRS.slice(1).flatMap((dir) => names.map((f) => path.join(dir, f)).filter((f) => existsSync(f))),
        ];
        const hits = files.filter((f) => TRACKERS.test(readFileSync(f, 'utf8'))).map((f) => path.relative(ROOT, f));
        expect(hits).toEqual([]);
    });
});

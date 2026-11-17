/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The container's entrypoint, run for a test: the real
 * `docker-entrypoint.sh` with a stand-in `nginx`, writing into a directory
 * of the test's. Both images run it as it is.
 */

import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect } from 'vitest';

const ROOT = path.resolve(__dirname, '..', '..');
const SCRIPT = path.join(ROOT, 'docker-entrypoint.sh');
const WEB_ROOT = '/usr/share/nginx/html';

const created: string[] = [];

/** Remove what the runs of a test left behind. */
export function removeContainers(): void {
    for (const dir of created.splice(0)) rmSync(dir, { recursive: true, force: true });
}

export interface ContainerRun {
    status: number | null;
    output: string;
    /** The file the script wrote, or null when it wrote none. */
    written: string | null;
    /** Whether the script went on to start nginx. */
    nginxStarted: boolean;
}

/** Run the entrypoint with `env` as the container's environment. */
export function startContainer(env: Record<string, string>): ContainerRun {
    const dir = mkdtempSync(path.join(tmpdir(), 'admin-entrypoint-'));
    created.push(dir);
    const bin = path.join(dir, 'bin');
    const html = path.join(dir, 'html');
    mkdirSync(bin);
    mkdirSync(html);
    // The script ends by starting nginx: a stand-in that says it was started.
    const started = path.join(dir, 'nginx-started');
    writeFileSync(path.join(bin, 'nginx'), `#!/bin/sh\ntouch "${started}"\nexit 0\n`);
    chmodSync(path.join(bin, 'nginx'), 0o755);
    // The same script, writing into a directory of this test's.
    const script = path.join(dir, 'entrypoint.sh');
    const source = readFileSync(SCRIPT, 'utf8');
    // The script writes into the web root.
    expect(source).toContain(WEB_ROOT);
    writeFileSync(script, source.split(WEB_ROOT).join(html));
    const result = spawnSync('/bin/sh', [script], { env: { PATH: `${bin}:/usr/bin:/bin`, ...env }, encoding: 'utf8' });
    const file = path.join(html, 'env-config.js');
    return {
        status: result.status,
        output: `${result.stdout}${result.stderr}`,
        written: existsSync(file) ? readFileSync(file, 'utf8') : null,
        nginxStarted: existsSync(started),
    };
}

/** What the console would see in `window._env_` after the entrypoint ran with `env`. */
export function runtimeConfig(env: Record<string, string>): Record<string, string> {
    const run = startContainer(env);
    expect({ status: run.status, output: run.status === 0 ? '' : run.output }).toEqual({ status: 0, output: '' });
    // The file sets `window._env_`.
    expect(run.written).toMatch(/window\._env_ = \{/);
    // The object is written one `KEY: "value"` per line.
    return Object.fromEntries([...(run.written ?? '').matchAll(/^\s*([A-Z_]+): "(.*)",?$/gm)].map((m) => [m[1], m[2]]));
}

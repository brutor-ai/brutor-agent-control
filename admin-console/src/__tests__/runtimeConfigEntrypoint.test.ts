/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The container's runtime configuration, end to end.
 *
 * In an image the console reads its configuration from `window._env_`,
 * which `docker-entrypoint.sh` writes at start from the container's
 * environment (`src/env.ts`). The script is shell and cannot read the
 * registry (`src/envRegistry.json`), so this holds the two together: the
 * script writes every variable the console reads at run time, under the
 * registry's names and with the registry's defaults, and it reads no
 * other name.
 *
 * This runs the real script with a stand-in `nginx` and reads the file it
 * writes (`src/test/containerEntrypoint.ts`). The Enterprise image runs
 * the same script with nothing beside it.
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import { readRegistry } from '../../scripts/tool-env.mjs';
import { removeContainers, runtimeConfig, startContainer } from '../test/containerEntrypoint';

afterEach(removeContainers);

const start = (env: Record<string, string>) => startContainer(env);

const ROOT = path.resolve(__dirname, '..', '..');

const runtimeVariables = () => (readRegistry('community')?.variables ?? []).filter((variable) => variable.read_by === 'application');

describe('the container entrypoint and the console agree on the runtime configuration', () => {
    it('writes every variable the console reads at run time, under its name, and no other', () => {
        expect(Object.keys(runtimeConfig({})).sort()).toEqual(runtimeVariables().map((variable) => variable.name).sort());
    });

    it('falls back, for each variable, to the default the registry states', () => {
        const config = runtimeConfig({});
        expect(config).toEqual(Object.fromEntries(runtimeVariables().map((variable) => [variable.name, variable.default])));
    });

    it('passes on the Control Plane address it is given', () => {
        const config = runtimeConfig({ BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'https://control.example.test' });
        expect(config.BRUTOR_CONTROL_PLANE_PUBLIC_URL).toBe('https://control.example.test');
        expect(config.BRUTOR_GATEWAY_PUBLIC_URL).toBe('http://localhost:8100');
    });

    it('passes on the Core Gateway address it is given', () => {
        const config = runtimeConfig({ BRUTOR_GATEWAY_PUBLIC_URL: 'https://gateway.example.test' });
        expect(config.BRUTOR_GATEWAY_PUBLIC_URL).toBe('https://gateway.example.test');
        expect(config.BRUTOR_CONTROL_PLANE_PUBLIC_URL).toBe('http://localhost:5050');
    });

    it('passes on the debug switch', () => {
        expect(runtimeConfig({ BRUTOR_DEBUG_ENABLED: 'true' }).BRUTOR_DEBUG_ENABLED).toBe('true');
    });

    it('starts nginx when nothing is wrong', () => {
        const run = start({ BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'https://control.example.test' });
        expect(run.status).toBe(0);
        expect(run.nginxStarted).toBe(true);
    });

    it('builds with the same Control Plane default as it runs with', () => {
        const control = runtimeVariables().find((variable) => variable.name === 'BRUTOR_CONTROL_PLANE_PUBLIC_URL');
        for (const file of ['Dockerfile', path.join('enterprise', 'Dockerfile')]) {
            if (!existsSync(path.join(ROOT, file))) continue;
            const source = readFileSync(path.join(ROOT, file), 'utf8');
            const arg = new RegExp(`^ARG ${control?.build_name}=(\\S+)$`, 'm').exec(source);
            // The file declares the build-time default.
            expect({ file, default: arg?.[1] }).toEqual({ file, default: control?.default });
        }
    });
});

describe('a variable the console does not read', () => {
    // There are no old names to refuse: a name the registry does not define
    // is not read, and the container starts as if it were not set.
    const UNREAD = { GATEWAY_URL: 'https://unread-control.example.test', NODE_ENV: 'production', SOMETHING_ELSE: 'x' };

    it('is ignored: the container writes the registry\'s configuration and starts nginx', () => {
        const run = start(UNREAD);
        expect(run.status).toBe(0);
        expect(run.nginxStarted).toBe(true);
        expect(runtimeConfig(UNREAD)).toEqual(runtimeConfig({}));
    });

    it('is never printed', () => {
        expect(start(UNREAD).output).not.toContain('unread-control.example.test');
    });
});

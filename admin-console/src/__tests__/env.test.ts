/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * How the console reads its configuration (`src/env.ts`).
 *
 * Each setting has one definition in the registry and two places it can
 * come from: at run time `window._env_`, which the container's entrypoint
 * writes under the variable's name, and at build time what Vite embedded
 * under its `VITE_` name. What the container was given wins, so one image
 * serves any installation; a build from source has no `window._env_`.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

type Runtime = Record<string, string> | undefined;
const setRuntime = (value: Runtime) => {
    // happy-dom's window property cannot be deleted: assign.
    (window as unknown as { _env_: Runtime })._env_ = value;
};

async function load(runtime: Runtime, build: Record<string, string> = {}) {
    vi.resetModules();
    setRuntime(runtime);
    for (const [name, value] of Object.entries(build)) vi.stubEnv(name, value);
    return import('../env');
}

afterEach(() => {
    vi.unstubAllEnvs();
    setRuntime(undefined);
    vi.resetModules();
});

describe('the console\'s configuration', () => {
    it('is what the container was given, under the variables\' own names', async () => {
        const { env } = await load({
            BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'https://control.example.test',
            BRUTOR_GATEWAY_PUBLIC_URL: 'https://gateway.example.test',
            BRUTOR_DEBUG_ENABLED: 'true',
        });
        expect(env).toEqual({
            BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'https://control.example.test',
            BRUTOR_GATEWAY_PUBLIC_URL: 'https://gateway.example.test',
            BRUTOR_DEBUG_ENABLED: true,
        });
    });

    it('prefers what the container was given to what was built in', async () => {
        const { env } = await load(
            { BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'https://control.example.test' },
            { VITE_CONTROL_PLANE_PUBLIC_URL: 'http://built-in.example.test', VITE_GATEWAY_PUBLIC_URL: 'http://built-in-gateway.example.test' },
        );
        expect(env.BRUTOR_CONTROL_PLANE_PUBLIC_URL).toBe('https://control.example.test');
        // Not given at run time: the built-in value stands.
        expect(env.BRUTOR_GATEWAY_PUBLIC_URL).toBe('http://built-in-gateway.example.test');
    });

    it('is what was built in when there is no container configuration (a run from source)', async () => {
        const { env } = await load(undefined, {
            VITE_CONTROL_PLANE_PUBLIC_URL: 'http://localhost:5999',
            VITE_GATEWAY_PUBLIC_URL: 'http://localhost:8999',
        });
        expect(env.BRUTOR_CONTROL_PLANE_PUBLIC_URL).toBe('http://localhost:5999');
        expect(env.BRUTOR_GATEWAY_PUBLIC_URL).toBe('http://localhost:8999');
    });

    it('falls back to the defaults of the registry', async () => {
        const { env } = await load({});
        expect(env.BRUTOR_CONTROL_PLANE_PUBLIC_URL).toBe('http://localhost:5050');
        expect(env.BRUTOR_GATEWAY_PUBLIC_URL).toBe('http://localhost:8100');
        expect(env.BRUTOR_DEBUG_ENABLED).toBe(false);
    });

    it('reads no name the registry does not define', async () => {
        const { env } = await load(
            { GATEWAY_URL: 'https://old-control.example.test', PROXY_URL: 'https://old-gateway.example.test', ENABLE_DEBUG: 'true' },
            { VITE_GATEWAY_URL: 'http://old-built-in.example.test', VITE_PROXY_URL: 'http://old-built-in-gateway.example.test' },
        );
        expect(env.BRUTOR_CONTROL_PLANE_PUBLIC_URL).toBe('http://localhost:5050');
        expect(env.BRUTOR_GATEWAY_PUBLIC_URL).toBe('http://localhost:8100');
        expect(env.BRUTOR_DEBUG_ENABLED).toBe(false);
    });

    it.each([
        ['true', true],
        ['1', true],
        ['on', true],
        ['YES', true],
        ['false', false],
        ['0', false],
        ['maybe', false],
    ])('reads the debug switch %j as %s', async (raw, expected) => {
        const { env } = await load({ BRUTOR_DEBUG_ENABLED: raw });
        expect(env.BRUTOR_DEBUG_ENABLED).toBe(expected);
    });

    it('makes an address of the Control Plane from a path', async () => {
        const { apiUrl } = await load({ BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'https://control.example.test/' });
        expect(apiUrl('/v1/admin/tenants/current')).toBe('https://control.example.test/v1/admin/tenants/current');
        expect(apiUrl('v1/x')).toBe('https://control.example.test/v1/x');
    });

    it('says where each value came from, by name and never with the value of anything it was not asked for', async () => {
        const { envSources } = await load({ BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'https://control.example.test' }, { VITE_GATEWAY_PUBLIC_URL: 'http://localhost:8999' });
        expect(envSources()).toEqual({
            BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'container',
            BRUTOR_GATEWAY_PUBLIC_URL: 'build',
            BRUTOR_DEBUG_ENABLED: 'default',
        });
    });
});

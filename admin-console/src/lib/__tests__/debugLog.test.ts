/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console's one writer of diagnostics to the browser's console
 * (`src/lib/debugLog.ts`): silent unless `BRUTOR_DEBUG_ENABLED` is on.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

type Runtime = Record<string, string> | undefined;
const setRuntime = (value: Runtime) => {
    (window as unknown as { _env_: Runtime })._env_ = value;
};

const WRITERS = ['log', 'debug', 'info', 'warn', 'error', 'group', 'groupEnd', 'table', 'trace', 'dir'] as const;

/** Every writer of the browser's console, silenced and watched. */
function watchConsole() {
    return WRITERS.map((name) => vi.spyOn(console, name).mockImplementation(() => undefined));
}

async function load(debug: string | undefined) {
    vi.resetModules();
    setRuntime(debug === undefined ? {} : { BRUTOR_DEBUG_ENABLED: debug });
    const spies = watchConsole();
    const module = await import('../debugLog');
    return { ...module, spies };
}

afterEach(() => {
    vi.restoreAllMocks();
    setRuntime(undefined);
    vi.resetModules();
});

describe('debugLog', () => {
    it.each([['false'], ['off'], ['0'], [''], [undefined]])('writes nothing with the debug switch at %j', async (debug) => {
        const { debugLog, debugConfiguration, spies } = await load(debug);
        debugLog('health checks started', 30, true);
        debugConfiguration();
        for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    });

    it('writes the message and its details with the debug switch on', async () => {
        const { debugLog, spies } = await load('true');
        debugLog('health checks started', 30, true);
        expect(console.log).toHaveBeenCalledTimes(1);
        expect(console.log).toHaveBeenCalledWith('[brutor] health checks started', 30, true);
        for (const spy of spies.slice(1)) expect(spy).not.toHaveBeenCalled();
    });

    it('writes the configuration the console started with, one setting a line, with the debug switch on', async () => {
        const { debugConfiguration } = await load('true');
        debugConfiguration();
        expect(console.log).toHaveBeenCalledWith('[brutor] BRUTOR_DEBUG_ENABLED = true (container)');
        expect(console.log).toHaveBeenCalledWith('[brutor] BRUTOR_CONTROL_PLANE_PUBLIC_URL = http://localhost:5050 (default)');
    });

    it('reading the configuration writes nothing by itself', async () => {
        vi.resetModules();
        setRuntime({ BRUTOR_DEBUG_ENABLED: 'true' });
        const spies = watchConsole();
        await import('../../env');
        for (const spy of spies) expect(spy).not.toHaveBeenCalled();
    });
});

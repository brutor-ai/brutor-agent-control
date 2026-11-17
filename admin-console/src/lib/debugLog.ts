/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console's one writer of diagnostics to the browser's console.
 *
 * It writes only where `BRUTOR_DEBUG_ENABLED` is on; otherwise it does
 * nothing. Production source calls nothing else of `console` for a
 * diagnostic: ESLint's `no-console` (eslint.config.js) refuses `console.log`,
 * `console.debug`, `console.info` and the rest outside this file, and leaves
 * `console.warn` and `console.error` for failures.
 *
 * A diagnostic is a fixed sentence and scalars that say where the console
 * is (a count, a duration, a status, an identifier). The type of `details`
 * holds that: an object does not compile, so a request body, a response, a
 * form's state or a user record cannot be handed over whole. What a scalar
 * holds is the caller's to answer for: never a password, a token, a key or
 * any other credential, with the switch on or off.
 */

import { env, envSources } from '../env';

/** What a diagnostic may carry besides its sentence. */
export type DebugDetail = string | number | boolean | null | undefined;

const PREFIX = '[brutor]';

/** Whether the console writes diagnostics (`BRUTOR_DEBUG_ENABLED`). */
export const debugEnabled = (): boolean => env.BRUTOR_DEBUG_ENABLED;

/** Write `message` and `details` to the browser's console, where debugging is on. */
export function debugLog(message: string, ...details: DebugDetail[]): void {
    if (!debugEnabled()) return;
    console.log(`${PREFIX} ${message}`, ...details);
}

/**
 * Write the configuration the console started with, and where each setting
 * came from, where debugging is on. Addresses and switches only: there is
 * no secret in the console's configuration.
 */
export function debugConfiguration(): void {
    if (!debugEnabled()) return;
    const sources = envSources();
    for (const name of Object.keys(sources) as (keyof typeof sources)[]) {
        debugLog(`${name} = ${String(env[name])} (${sources[name]})`);
    }
}

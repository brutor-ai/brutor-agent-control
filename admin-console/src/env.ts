/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The console's configuration: the one module that reads it.
 *
 * Every setting is defined once, in `envRegistry.json` beside this file
 * (name, type, default, description, example). A setting can come from two
 * places, and this module is the only reader of both:
 *
 *   at run time    `window._env_`, under the variable's own name
 *                  (`BRUTOR_CONTROL_PLANE_PUBLIC_URL`). In a container
 *                  `docker-entrypoint.sh` writes it from the container's
 *                  environment into `env-config.js`, which the page loads
 *                  before the application. So one image serves any
 *                  installation: the addresses are set when the container
 *                  starts, not when the image is built.
 *   at build time  what Vite embedded from `.env`, under the variable's
 *                  `VITE_` name (`VITE_CONTROL_PLANE_PUBLIC_URL`). A run
 *                  from source has no `window._env_`
 *                  (`public/env-config.js` leaves it unset).
 *
 * Per setting, what the container was given wins, then what was built in,
 * then the default of the registry. A name the registry does not define is
 * not read here or anywhere.
 *
 * Nothing else names `import.meta.env` or `window._env_`
 * (`src/__tests__/envRegistry.test.ts`). The build tools' environment is
 * read by `scripts/tool-env.mjs`.
 */

import registry from './envRegistry.json';

/** The console's settings, by the name of their variable. */
export interface EnvConfig {
    /** Base URL of the Control Plane (the admin API), as the browser reaches it: every `/v1/admin/*` call. */
    BRUTOR_CONTROL_PLANE_PUBLIC_URL: string;
    /**
     * Base URL of the Core Gateway, as the browser reaches it: the gateway
     * addresses the console shows (`/v1/proxy/*`, agent cards) and the
     * probes it calls directly.
     */
    BRUTOR_GATEWAY_PUBLIC_URL: string;
    /** Write the console's diagnostics, and the configuration it started with, to the browser's console (`src/lib/debugLog.ts`). */
    BRUTOR_DEBUG_ENABLED: boolean;
}

type SettingName = keyof EnvConfig;

/** Where a setting's value came from. */
export type EnvSource = 'container' | 'build' | 'default';

declare global {
    interface Window {
        /** The container's configuration, by variable name; unset in a run from source. */
        _env_?: Partial<Record<string, string>>;
    }
}

/** A variable of a registry, as far as reading it goes. */
export interface SettingDefinition {
    name: string;
    build_name: string | null;
    read_by: string;
    default: string | null;
}

/** What Vite embedded at build time, by `VITE_` name. */
const built = import.meta.env as unknown as Partial<Record<string, string | boolean>>;

/**
 * The value of the application setting `name` of `variables` as text, and
 * where it came from, read now: what the container was given, then what
 * was built in, then the registry's default. The Enterprise package reads
 * the settings of its own registry through this.
 */
export function readSetting(variables: readonly SettingDefinition[], name: string): { value: string; source: EnvSource } {
    const found = variables.find((variable) => variable.name === name && variable.read_by === 'application');
    if (!found) throw new Error(`${name} is not a variable of the registry`);
    const given = window._env_?.[name];
    if (typeof given === 'string' && given !== '') return { value: given, source: 'container' };
    const embedded = found.build_name === null ? undefined : built[found.build_name];
    if (typeof embedded === 'string' && embedded !== '') return { value: embedded, source: 'build' };
    return { value: found.default ?? '', source: 'default' };
}

const read = (name: SettingName): { value: string; source: EnvSource } => readSetting(registry.variables, name);

/** A switch, in the one spelling every variable accepts; anything else is off. */
const isOn = (value: string): boolean => ['true', '1', 'yes', 'on'].includes(value.trim().toLowerCase());

export const env: EnvConfig = {
    BRUTOR_CONTROL_PLANE_PUBLIC_URL: read('BRUTOR_CONTROL_PLANE_PUBLIC_URL').value,
    BRUTOR_GATEWAY_PUBLIC_URL: read('BRUTOR_GATEWAY_PUBLIC_URL').value,
    BRUTOR_DEBUG_ENABLED: isOn(read('BRUTOR_DEBUG_ENABLED').value),
};

/** Where each setting's value came from: the container, the build, or the registry's default. */
export function envSources(): Record<SettingName, EnvSource> {
    return {
        BRUTOR_CONTROL_PLANE_PUBLIC_URL: read('BRUTOR_CONTROL_PLANE_PUBLIC_URL').source,
        BRUTOR_GATEWAY_PUBLIC_URL: read('BRUTOR_GATEWAY_PUBLIC_URL').source,
        BRUTOR_DEBUG_ENABLED: read('BRUTOR_DEBUG_ENABLED').source,
    };
}

/** Whether this is the development server (`npm run dev`), as Vite says. */
export const isDevelopmentBuild: boolean = built.DEV === true;

/** Whether this is a production build, as Vite says. */
export const isProductionBuild: boolean = built.PROD === true;

/** The address of a Control Plane path. */
export const apiUrl = (path: string): string => {
    const baseUrl = env.BRUTOR_CONTROL_PLANE_PUBLIC_URL.replace(/\/$/, '');
    const cleanPath = path.startsWith('/') ? path : `/${path}`;
    return `${baseUrl}${cleanPath}`;
};

export default env;

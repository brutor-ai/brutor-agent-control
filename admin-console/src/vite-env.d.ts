/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/// <reference types="vite/client" />

declare const __APP_VERSION__: string;

/**
 * What Vite embeds in the bundle at build time. The settings are defined
 * in `envRegistry.json` and read by `env.ts`, which is the only module
 * that reads them.
 */
interface ImportMetaEnv {
    /** The Control Plane, as the browser reaches it. */
    readonly VITE_CONTROL_PLANE_PUBLIC_URL?: string;
    /** The Core Gateway, as the browser reaches it. */
    readonly VITE_GATEWAY_PUBLIC_URL?: string;

    // Vite's own.
    readonly MODE: string;
    readonly BASE_URL: string;
    readonly PROD: boolean;
    readonly DEV: boolean;
    readonly SSR: boolean;
}

interface ImportMeta {
    readonly env: ImportMetaEnv;
}

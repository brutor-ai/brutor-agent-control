/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Vitest global test setup for Admin Console
 */

import '@testing-library/jest-dom'
import { configure } from '@testing-library/react'
import { beforeEach, vi } from 'vitest'

// findBy*/waitFor give up after 1 s by default. On a loaded CI runner,
// under the full parallel suite, async renders land later than that and
// tests flaked red that pass alone in a fraction of a second
// (AuditExportTab). A wider window costs nothing when the
// assertion passes, which is every time it is right.
configure({ asyncUtilTimeout: 5000 })

// Vite injects __APP_VERSION__ at build time. The test env
// doesn't run the Vite define plugin, so any component that
// references the global blows up. Stub it before any module
// reads it.
;(globalThis as any).__APP_VERSION__ = 'test'

// The container's configuration, as docker-entrypoint.sh writes it
// (src/env.ts reads it under the variables' own names).
Object.defineProperty(window, '_env_', {
    writable: true,
    value: {
        BRUTOR_CONTROL_PLANE_PUBLIC_URL: 'http://localhost:5050',
        BRUTOR_GATEWAY_PUBLIC_URL: 'http://localhost:8100',
        BRUTOR_DEBUG_ENABLED: 'false',
    },
})

// Mock localStorage
const localStorageMock = (() => {
    let store: Record<string, string> = {}
    return {
        getItem: (key: string) => store[key] ?? null,
        setItem: (key: string, value: string) => { store[key] = value },
        removeItem: (key: string) => { delete store[key] },
        clear: () => { store = {} },
        get length() { return Object.keys(store).length },
        key: (index: number) => Object.keys(store)[index] ?? null,
    }
})()

Object.defineProperty(window, 'localStorage', { value: localStorageMock })

// Reset storage between tests
beforeEach(() => {
    localStorage.clear()
})

// Suppress console noise in tests
vi.spyOn(console, 'group').mockImplementation(() => {})
vi.spyOn(console, 'groupEnd').mockImplementation(() => {})

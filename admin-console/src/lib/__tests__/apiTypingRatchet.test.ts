/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The `Promise<any>` count only goes down.
 *
 * Most methods of `GatewayApiClient` return a type generated from the
 * Control Plane's OpenAPI document (`ApiResponse<path, method>`); the rest
 * return `Promise<any>`. A method stays on `Promise<any>` where the route's
 * generated type has no fields to offer: the Control Plane declares no
 * `response_model` for it, or one whose payload is a free dictionary.
 *
 * Adding one more `Promise<any>` is the path of least resistance, and
 * nothing else in the build notices: `any` is legal TypeScript, and one more
 * ESLint `no-explicit-any` warning is invisible in the total.
 *
 * This is the floor. It is a ratchet in the same spirit as the lint cap: when
 * the number drops, lower it here — never raise it.
 *
 * Why it matters beyond tidiness: these are the return types of governance
 * writes. A hand-written interface is an *assertion* about what the server
 * sends and nothing verifies it, so a wrong field name or wrong optionality
 * compiles cleanly and fails at runtime — and for the limits, guardrails and
 * policy methods, "silently dropped a field" is a governance outcome, not a
 * display bug.
 */

import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'

import { apiClientFiles, enterpriseUnderTest } from '../../test/sourceRoots'

// The client's methods live in the modules of src/lib/gatewayApi; the counts
// are over the facade and every module. The module of an Enterprise feature
// is in that feature's directory under enterprise/: in the Enterprise
// package's run of this file the counts are over those too
// (`apiClientFiles`).
// The two numbers are per edition under test: the Community package's are
// its own modules' alone, the Enterprise package's are the whole client's.
// The generated types are per edition too: a Community module's
// `ApiResponse` reads the Community document's types, an Enterprise module's
// the Enterprise document's (`enterprise/src/lib/api/controlPlaneTypes.ts`).
// The alias has one name in both, so the counts below are over both.
const CLIENT_FILES = apiClientFiles()
const HAS_ENTERPRISE = enterpriseUnderTest()

/**
 * Current count. LOWER THIS when you convert a method; never raise it.
 *
 * The cap is the measured count exactly, with no slack: one of slack lets a
 * new `Promise<any>` through. 41 in the Community client, 16 more in the
 * Enterprise feature modules.
 */
const MAX_PROMISE_ANY = HAS_ENTERPRISE ? 56 : 41

/**
 * Methods already converted. This one may only go UP. The floor is the
 * measured count: 140 in the Community client, 30 more in the Enterprise
 * feature modules.
 */
const MIN_TYPED_RESPONSES = HAS_ENTERPRISE ? 170 : 140

function clientSource(): string {
    return CLIENT_FILES.map((f) => readFileSync(f, 'utf8')).join('\n')
}

describe('API typing ratchet', () => {
    it('does not add new Promise<any> returns', () => {
        const count = (clientSource().match(/Promise<any>/g) ?? []).length
        expect(
            count,
            `the API client (src/lib/gatewayApi) has ${count} Promise<any> returns, up from the ` +
            `ratchet of ${MAX_PROMISE_ANY}. New client methods should use the ` +
            `generated ApiResponse<path, method> alias ` +
            `(src/lib/api/controlPlaneTypes.ts). If you genuinely converted methods, ` +
            `lower MAX_PROMISE_ANY to match.`
        ).toBeLessThanOrEqual(MAX_PROMISE_ANY)
    })

    it('does not lose already-typed responses', () => {
        const count = (clientSource().match(/ApiResponse</g) ?? []).length
        expect(
            count,
            `only ${count} methods use the generated ApiResponse alias, down ` +
            `from ${MIN_TYPED_RESPONSES}. A conversion was reverted.`
        ).toBeGreaterThanOrEqual(MIN_TYPED_RESPONSES)
    })

    it('counts every module of the client, wherever its file is', () => {
        // One file per module, plus the facade and the two files of the
        // module directory that are not modules (base, modules).
        const names = CLIENT_FILES.map((f) => f.split('/').pop())
        expect(new Set(names).size).toBe(names.length)
        // 29 Community modules; 40 with the Enterprise ones.
        expect(names.length - 3).toBe(HAS_ENTERPRISE ? 40 : 29)
    })

    it('the ratchet is not stale', () => {
        // If the real count has dropped well below the cap, the cap is no
        // longer doing its job — it would allow silent regressions back up to
        // the old number. Keep it within a small margin of reality.
        const count = (clientSource().match(/Promise<any>/g) ?? []).length
        expect(
            MAX_PROMISE_ANY - count,
            `MAX_PROMISE_ANY is ${MAX_PROMISE_ANY} but the real count is ` +
            `${count}. Lower the ratchet — a cap with slack in it permits the ` +
            `regressions it exists to prevent.`
        ).toBeLessThanOrEqual(5)
    })
})

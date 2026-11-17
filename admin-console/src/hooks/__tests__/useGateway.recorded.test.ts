/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The record of the gateway hooks of both editions taken together: every key
 * the Community facade `useGateway` and the Enterprise hook return between
 * them, and which domain hook each key comes from.
 *
 * `recorded/useGateway.keys.json`:
 *   - `all`: the keys of both hooks, sorted;
 *   - `modules`: per domain hook, its Enterprise feature (null: Community)
 *     and the keys it returns;
 *   - `facade`: the keys no domain hook returns: the core's, and the two the
 *     facade derives from the registry hook's state.
 *
 * `recorded/useGateway.surface.json` is the two tables of the hooks' API
 * surface (269 rows).
 *
 * The records are held from both sides: the Community facade returns the
 * recorded keys less the Enterprise ones and is tested with the recorded
 * rows of those methods (below), and the Enterprise hook has the rest, the
 * two together being the records exactly
 * (`enterprise/src/hooks/__tests__/useEnterpriseGateway.recorded.test.ts`).
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => {
    const client = new Proxy({}, { get: () => () => Promise.resolve({}) });
    return { auth: { isAuthenticated: true, isLoading: false, gatewayClient: client } };
});
vi.mock('../useAuth', () => ({ useAuth: () => mocks.auth }));

import { GATEWAY_HOOK_MODULES } from '../gateway/modules';
import { PASS_THROUGH, REPORTING, RESOLVES_EMPTY } from '../../test/gatewaySurface';
import { useGateway } from '../useGateway';

const RECORDED = resolve(__dirname, 'recorded');

interface RecordedKeys {
    all: string[];
    modules: Record<string, { feature: string | null; keys: string[] }>;
    facade: string[];
}
interface RecordedSurface {
    PASS_THROUGH: unknown[][];
    REPORTING: unknown[][];
    RESOLVES_EMPTY: string[];
}

const record = JSON.parse(readFileSync(resolve(RECORDED, 'useGateway.keys.json'), 'utf8')) as RecordedKeys;
const surface = JSON.parse(readFileSync(resolve(RECORDED, 'useGateway.surface.json'), 'utf8')) as RecordedSurface;

/** The two keys derived from the registry hook's state: they belong with that hook. */
const DERIVED_FROM_ENTERPRISE = ['enabledRegistries', 'hasEnabledRegistries'];

/** The recorded keys that came from an Enterprise domain hook, or were derived from one. */
const enterpriseKeys = new Set([
    ...Object.values(record.modules)
        .filter((module) => module.feature !== null)
        .flatMap((module) => module.keys),
    ...DERIVED_FROM_ENTERPRISE,
]);

const fakeCore = () => ({
    gatewayClient: new Proxy({}, { get: () => () => Promise.resolve({}) }),
    addError: vi.fn(),
    enableAuth: true,
    isAuthenticated: true,
});

describe('the record of the one hook', () => {
    it('reads as expected: every key once, 21 domain hooks, four of them Enterprise', () => {
        expect(new Set(record.all).size).toBe(record.all.length);
        expect(record.all).toHaveLength(309);
        expect(Object.keys(record.modules)).toHaveLength(21);
        expect(
            Object.entries(record.modules)
                .filter(([, m]) => m.feature !== null)
                .map(([name, m]) => `${name}: ${m.feature}`),
        ).toEqual(['portal: portal', 'multiTenant: multi_tenant', 'mcpRegistry: mcp_registry', 'knowledgeBase: knowledge_base']);
        expect(record.facade).toEqual([
            'clearErrors', 'dismissError', 'enabledRegistries', 'errors', 'gatewayClient', 'gatewayUrl',
            'getErrorsBySeverity', 'hasCriticalErrors', 'hasEnabledRegistries', 'hasErrors', 'initialLoading',
            'isAuthenticated', 'llmProxyUrl',
        ]);
        expect(enterpriseKeys.size).toBe(48);
    });
});

describe('useGateway, the Community hook', () => {
    const now = Object.keys(renderHook(() => useGateway()).result.current).sort();

    it('returns the recorded keys less the ones of the Enterprise domain hooks, and nothing else', () => {
        expect(now).toEqual(record.all.filter((key) => !enterpriseKeys.has(key)));
        expect(now).toHaveLength(261);
    });

    it('returns no key of an Enterprise domain hook', () => {
        expect(now.filter((key) => enterpriseKeys.has(key))).toEqual([]);
    });

    it('is made of the 17 Community domain hooks, each returning the keys recorded for it', () => {
        const modules = Object.fromEntries(
            GATEWAY_HOOK_MODULES.map((m) => {
                const { result } = renderHook(() => (m.hook as (ctx: unknown) => Record<string, unknown>)(fakeCore()));
                return [m.name, { feature: m.feature, keys: Object.keys(result.current).sort() }];
            }),
        );
        const recorded = Object.fromEntries(Object.entries(record.modules).filter(([, m]) => m.feature === null));
        expect(modules).toEqual(recorded);
        expect(Object.keys(modules)).toHaveLength(17);
    });

    it('its test tables are the recorded rows of the methods it returns, and no other row', () => {
        const mine = (rows: unknown[][]): unknown[][] => rows.filter((row) => !enterpriseKeys.has(String(row[0])));
        expect(PASS_THROUGH).toEqual(mine(surface.PASS_THROUGH));
        expect(REPORTING).toEqual(mine(surface.REPORTING));
        expect([...RESOLVES_EMPTY]).toEqual(surface.RESOLVES_EMPTY.filter((method) => !enterpriseKeys.has(method)));
        expect(PASS_THROUGH.length + REPORTING.length).toBe(239);
    });
});

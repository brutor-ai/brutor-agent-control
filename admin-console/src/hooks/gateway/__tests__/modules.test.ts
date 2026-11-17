/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The domain hooks of `useGateway`.
 *
 * The hook is a core hook plus 21 domain hooks, composed by the facade.
 * These tests hold the composition: every domain hook is called once, in the
 * registry's order, with the core; each returns operations of its own (no
 * name twice); the Enterprise hooks are the recorded ones; and a domain hook
 * reaches the client through the core only, never through another hook or
 * the facade.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { EnterpriseFeature } from '../../../app/editions';
import { GATEWAY_HOOK_MODULES } from '../modules';
import { editionUnderTest } from '../../../test/underTest';
import { NOT_SOURCE_DIRS, importedPaths, scanRoots, sourceName } from '../../../test/sourceRoots';

const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE_DIR = resolve(HERE, '..');
const FACADE = resolve(HERE, '../../useGateway.ts');

/** The Enterprise hooks and their features. */
const RECORDED_ENTERPRISE: Record<string, EnterpriseFeature> = {
    portal: 'portal',
    multiTenant: 'multi_tenant',
    mcpRegistry: 'mcp_registry',
    knowledgeBase: 'knowledge_base',
};

/**
 * The domain hooks of the console as shipped: the ones `useGateway` calls
 * (`GATEWAY_HOOK_MODULES`, Community), then the ones `useEnterpriseGateway`
 * calls. All 21 are held to the same rules here. That the Enterprise facade calls
 * its four is checked beside it
 * (`enterprise/src/hooks/__tests__/useEnterpriseGateway.recorded.test.ts`).
 */
const EXTENSION_HOOK_MODULES = (await editionUnderTest().registries()).hookModules;
const ALL_HOOK_MODULES = [...GATEWAY_HOOK_MODULES, ...EXTENSION_HOOK_MODULES];
/**
 * False in the Community package's run: the hooks are the Community ones
 * alone there, and every rule below holds them the same way. True where the
 * Enterprise package runs this file again with its own (`test/underTest.ts`).
 */
const HAS_ENTERPRISE = EXTENSION_HOOK_MODULES.length > 0;

/** A core every domain hook accepts: a client that answers anything, and an error sink. */
const fakeCore = () => ({
    gatewayClient: new Proxy({}, { get: () => () => Promise.resolve({}) }),
    addError: vi.fn(),
    enableAuth: true,
    isAuthenticated: true,
});

describe('the domain hooks of useGateway', () => {
    it('have unique names and are tagged as recorded', () => {
        const names = ALL_HOOK_MODULES.map((m) => m.name);
        expect(new Set(names).size).toBe(names.length);
        expect(names.length).toBe(HAS_ENTERPRISE ? 21 : 17);
        // The Community facade's are the first 17, none of an Enterprise feature.
        expect(ALL_HOOK_MODULES.slice(0, GATEWAY_HOOK_MODULES.length)).toEqual(GATEWAY_HOOK_MODULES);
        expect(GATEWAY_HOOK_MODULES.length).toBe(17);
        expect(GATEWAY_HOOK_MODULES.filter((m) => m.feature !== null)).toEqual([]);
        expect(names.every((n) => n !== '')).toBe(true);
        const tagged = Object.fromEntries(
            ALL_HOOK_MODULES.filter((m) => m.feature !== null).map((m) => [m.name, m.feature])
        );
        expect(tagged).toEqual(HAS_ENTERPRISE ? RECORDED_ENTERPRISE : {});
    });

    it('each return operations of their own, and no name twice', () => {
        const owner = new Map<string, string>();
        const twice: string[] = [];
        for (const m of ALL_HOOK_MODULES) {
            const { result } = renderHook(() => (m.hook as (ctx: unknown) => Record<string, unknown>)(fakeCore()));
            const keys = Object.keys(result.current);
            if (keys.length === 0) twice.push(`${m.name} returns nothing`);
            for (const k of keys) {
                const other = owner.get(k);
                if (other !== undefined) twice.push(`${k} from ${m.name} and ${other}`);
                owner.set(k, m.name);
            }
        }
        expect(twice).toEqual([]);
        // 251 operations from the Community hooks; 299 with the Enterprise ones.
        expect(owner.size).toBeGreaterThan(HAS_ENTERPRISE ? 280 : 240);
    });

    // A domain hook is an exported `use…Gateway` function. Its file is found
    // wherever it lives: in this directory, or anywhere else under the trees
    // of both editions (the Enterprise hooks are under
    // `enterprise/`, and under these two rules).
    const HOOK_DIR = 'hooks/gateway/';
    const FACADES = ['hooks/useGateway', 'enterprise/src/hooks/useEnterpriseGateway'];

    function sources(dir: string): string[] {
        return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
            const full = join(dir, entry.name);
            if (entry.isDirectory()) {
                return entry.name === '__tests__' || entry.name === 'test' || NOT_SOURCE_DIRS.has(entry.name) ? [] : sources(full);
            }
            return /\.tsx?$/.test(entry.name) && !/\.(test|d)\.tsx?$/.test(entry.name) ? [full] : [];
        });
    }

    /** The hook files: named as `sourceName` names them (no extension), with the domain hooks each declares. */
    const hookFiles: Array<{ name: string; file: string; src: string; hooks: string[] }> = [];
    for (const file of scanRoots((root) => sources(root))) {
        const name = sourceName(file).replace(/\.tsx?$/, '');
        const src = readFileSync(file, 'utf8');
        const hooks = [...src.matchAll(/^export\s+(?:function|const)\s+(use[A-Za-z0-9]+Gateway)\b/gm)].map((x) => x[1]);
        // Every file of this directory is a hook file, whatever it declares.
        const here = name.startsWith(HOOK_DIR) && !['core', 'modules'].includes(name.slice(HOOK_DIR.length));
        if (!FACADES.includes(name) && (hooks.length > 0 || here)) hookFiles.push({ name, file, src, hooks });
    }
    const isHookFile = (name: string): boolean => hookFiles.some((h) => h.name === name);

    it('are the files of this directory, one each', () => {
        const files = readdirSync(MODULE_DIR)
            .filter((f) => f.endsWith('.ts') && !['core.ts', 'modules.ts'].includes(f))
            .map((f) => f.replace(/\.ts$/, ''))
            .sort();
        // This directory holds the hooks `useGateway` calls (Community) and
        // no other: an Enterprise hook's file is in its feature's directory
        // under `enterprise/`.
        expect(files).toEqual(GATEWAY_HOOK_MODULES.map((m) => m.name).sort());
        // Wherever it is, a hook's file is named after its registry entry.
        expect(hookFiles.map((h) => h.name.split('/').pop()).sort()).toEqual(ALL_HOOK_MODULES.map((m) => m.name).sort());
        const misplaced = hookFiles
            .filter((h) => !h.name.startsWith(HOOK_DIR))
            .filter((h) => !/^enterprise\/src\/features\/[a-z_]+\/hooks\/gateway\/[A-Za-z]+$/.test(h.name));
        expect(misplaced.map((h) => h.name)).toEqual([]);
        // …and, over both trees: each file declares one domain hook, and
        // the hooks declared are the registered ones.
        expect(hookFiles.filter((h) => h.hooks.length !== 1).map((h) => h.name)).toEqual([]);
        expect(hookFiles.flatMap((h) => h.hooks).sort()).toEqual(ALL_HOOK_MODULES.map((m) => m.hook.name).sort());
    });

    it('import from the core only, never from another hook or the facade', () => {
        expect(hookFiles.length).toBe(ALL_HOOK_MODULES.length);
        for (const { name, file, src } of hookFiles) {
            const local = importedPaths(file, src)
                .map((imported) => sourceName(imported))
                .filter((imported) => FACADES.includes(imported) || imported.startsWith(HOOK_DIR) || isHookFile(imported));
            expect([...new Set(local)], `${name} imports ${local.join(', ')}`).toEqual([`${HOOK_DIR}core`]);
        }
    });

    it('are each called once by the facade, in the registry’s order, with the core', () => {
        const facade = readFileSync(FACADE, 'utf8');
        const calls = [...facade.matchAll(/= (use[A-Za-z]+Gateway)\((core|\{ \.\.\.core[^)]*\})\);/g)].map((x) => x[1]);
        expect(calls).toEqual(GATEWAY_HOOK_MODULES.map((m) => m.hook.name));
        // each hook is called exactly once, wherever the call sits; the core first, exactly once
        const callCounts = GATEWAY_HOOK_MODULES.map((m) => [m.hook.name, facade.match(new RegExp(`\\b${m.hook.name}\\(`, 'g'))?.length ?? 0]);
        expect(callCounts.filter(([, n]) => n !== 1)).toEqual([]);
        expect(facade.match(/useGatewayCore\(/g)?.length).toBe(1);
        expect(facade.indexOf('useGatewayCore(')).toBeLessThan(facade.indexOf(`${calls[0]}(core`));
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The edition under test (`../underTest.ts`) decides what a run reads, and
 * the disk does not: a Community run in a tree that has `enterprise/` beside
 * `src/` scans `src/` alone and counts the Community modules alone.
 */

import { existsSync } from 'node:fs';

import { afterEach, describe, expect, it } from 'vitest';

import { ENTERPRISE_ROOT, SRC_ROOT, apiClientFiles, enterpriseUnderTest, isEnterprisePath, sourceRoots } from '../sourceRoots';
import { declareEditionUnderTest, editionUnderTest } from '../underTest';

describe('the edition under test', () => {
    const declared = editionUnderTest();
    afterEach(() => declareEditionUnderTest(declared));

    it('with nothing added it is the Community Edition: src/ alone, whatever is on the disk', async () => {
        declareEditionUnderTest({ sourceRoots: [], registries: () => Promise.resolve({ apiModules: [], hookModules: [] }) });
        expect(sourceRoots()).toEqual([SRC_ROOT]);
        expect(enterpriseUnderTest()).toBe(false);
        expect(apiClientFiles().filter(isEnterprisePath)).toEqual([]);
        expect(await editionUnderTest().registries()).toEqual({ apiModules: [], hookModules: [] });
    });

    it('a declared tree is scanned after src/, and the client files of its features are counted', () => {
        declareEditionUnderTest({ sourceRoots: ['enterprise'], registries: declared.registries });
        expect(sourceRoots()).toEqual([SRC_ROOT, ENTERPRISE_ROOT]);
        expect(enterpriseUnderTest()).toBe(true);
        // Counted where the tree is there to count.
        expect(apiClientFiles().filter(isEnterprisePath).length > 0).toBe(existsSync(ENTERPRISE_ROOT));
    });

    it('this run declares what its package is: nothing for the Community package', () => {
        // The Enterprise package's set-up file declares its tree; no other file does.
        expect(declared.sourceRoots.length === 0).toBe(!enterpriseUnderTest());
        expect(sourceRoots().length).toBe(1 + declared.sourceRoots.length);
    });
});

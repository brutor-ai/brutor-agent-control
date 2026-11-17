/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The edition under test.
 *
 * Each package runs its own tests: the Community package the ones under
 * `src/`, and nothing of another tree is read or loaded while it does,
 * whether that tree is on the disk or not. Some Community tests hold a rule
 * that every edition is under: the source-scan guards, the guards on the API
 * client modules and on the gateway hook modules. Those are written to take
 * what the edition under test adds, and a package that extends the console
 * runs them again with its own additions (its vitest configuration lists
 * them; its set-up file declares the additions here).
 *
 * With nothing declared the edition under test is the Community Edition:
 * no tree beside `src/`, no module beside the Community ones.
 */

import type { GatewayHookModuleSpec } from '../hooks/gateway/modules';
import type { ApiModuleSpec } from '../lib/gatewayApi/modules';

/** The registries an extension adds to, as far as the Community guards hold them. */
export interface ExtensionRegistries {
    apiModules: readonly ApiModuleSpec[];
    hookModules: readonly GatewayHookModuleSpec[];
}

export interface EditionUnderTest {
    /** The source trees the edition adds to `src/`, as directories from the repository root. */
    sourceRoots: readonly string[];
    /** Loaded only by the tests that ask: loading an extension's registries loads its modules. */
    registries: () => Promise<ExtensionRegistries>;
}

const COMMUNITY: EditionUnderTest = {
    sourceRoots: [],
    registries: () => Promise.resolve({ apiModules: [], hookModules: [] }),
};

// On `globalThis`: a set-up file and the test file it runs before need not share this module's instance.
const KEY = Symbol.for('brutor.admin-console.edition-under-test');
type Holder = { [KEY]?: EditionUnderTest };

/** Called by the set-up file of a package that extends the console. Not by a test. */
export function declareEditionUnderTest(edition: EditionUnderTest): void {
    (globalThis as Holder)[KEY] = edition;
}

export function editionUnderTest(): EditionUnderTest {
    return (globalThis as Holder)[KEY] ?? COMMUNITY;
}

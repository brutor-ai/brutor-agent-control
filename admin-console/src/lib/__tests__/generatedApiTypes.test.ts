/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The generated Control Plane types are the ones of the edition's own
 * document.
 *
 * The Community package is written with the types generated from the
 * Community OpenAPI document (`src/lib/generated/control-plane-api.d.ts`),
 * the Enterprise package with those of the Enterprise document
 * (`enterprise/src/lib/generated/control-plane-api.d.ts`). A path only the
 * Enterprise Control Plane serves is then not a key of the Community `paths`,
 * and a Community module that types a call with one does not compile.
 *
 * That holds only while each file really is generated from its document.
 * Held here for the edition under test (the Enterprise package runs this
 * file again):
 *
 *   - anywhere: the paths of the generated file are exactly the paths of the
 *     vendored route list of the same edition, which the route contract
 *     (`gatewayApiClient.routes.test.ts`) holds to the document;
 *   - beside the Control Plane repository: generating again from the
 *     document gives the file, byte for byte.
 */

import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { apiContractUnderTest, enterpriseRoutePrefixes, isUnder, vendoredPaths } from '../../test/apiContract';
import { enterpriseUnderTest } from '../../test/sourceRoots';
import { documentForTest } from '../../../scripts/control-plane-documents.mjs';

const CONTRACT = apiContractUnderTest();
const generated = readFileSync(CONTRACT.types, 'utf8');

/** The keys of the generated `paths` interface, in the order written. */
function generatedPaths(source: string): string[] {
    const start = source.indexOf('export interface paths {');
    const end = source.indexOf('\nexport type webhooks');
    if (start < 0 || end < start) throw new Error('not an openapi-typescript file: no `paths` interface');
    return [...source.slice(start, end).matchAll(/^ {4}"([^"]+)": \{$/gm)].map((m) => m[1]);
}

describe('the generated Control Plane types of the edition under test', () => {
    const paths = generatedPaths(generated);

    it('are in the package they are the types of', () => {
        expect(CONTRACT.types.includes('/enterprise/')).toBe(enterpriseUnderTest());
        expect(CONTRACT.types).toMatch(/\/src\/lib\/generated\/control-plane-api\.d\.ts$/);
    });

    it('have exactly the paths of the vendored route list of the same edition', () => {
        expect(paths.length).toBeGreaterThan(enterpriseUnderTest() ? 600 : 450);
        expect(new Set(paths).size).toBe(paths.length);
        expect([...paths].sort()).toEqual(vendoredPaths(CONTRACT));
    });

    it('have the paths of the Enterprise features in the Enterprise file and in no other', () => {
        const prefixes = enterpriseRoutePrefixes();
        const used = prefixes.filter((prefix) => paths.some((p) => isUnder(p, prefix)));
        expect(used).toEqual(enterpriseUnderTest() ? prefixes : []);
    });

    // A field with a default may be left out of a request: the Control Plane
    // fills it. The generator is run with `--default-non-nullable=false`, so a
    // field is required only where the document's `required` says so.
    it('type a field that has a default as optional', () => {
        const lines = generated.split('\n');
        const requiredWithDefault: string[] = [];
        let defaulted = false;
        for (const line of lines) {
            if (/^\s*\* @default /.test(line)) defaulted = true;
            const property = /^\s*("?[\w$-]+"?)(\??):/.exec(line);
            if (property && !line.trimStart().startsWith('*')) {
                if (defaulted && property[2] !== '?') requiredWithDefault.push(line.trim());
                defaulted = false;
            }
        }
        expect(requiredWithDefault.slice(0, 5)).toEqual([]);
    });

    // Skipped where the Control Plane is not beside this repository (as
    // ../brutor-gateway-control or ../control-plane) and no directory of
    // documents is named: the document is not there to generate from.
    const generatesTheSame = 'are what generating from the Control Plane document of this edition gives';
    const document = documentForTest(CONTRACT.edition, `the generated Control Plane types ${generatesTheSame}`);
    it.skipIf(document === null)(
        `${generatesTheSame} (skipped without the sibling Control Plane)`,
        async () => {
            const { generateTypes } = await import('../../../scripts/gen-control-plane-types.mjs');
            const { movedDocumentMessage } = await import('../../../scripts/control-plane-documents.mjs');
            const dir = mkdtempSync(path.join(tmpdir(), 'control-plane-types-'));
            try {
                const out = path.join(dir, 'control-plane-api.d.ts');
                generateTypes(CONTRACT.document, out);
                // Compared as a boolean: a difference in a 70,000-line file is not a readable failure.
                // The message says which document was compared (its SHA-256) and
                // which the file was generated from: when the two differ, the
                // Control Plane document moved and the file is to be generated again.
                const command = enterpriseUnderTest() ? 'npm run enterprise:gen:api-types:control' : 'npm run gen:api-types:control';
                const same = readFileSync(out, 'utf8') === generated;
                if (!same) throw new Error(movedDocumentMessage(CONTRACT.edition, 'types', CONTRACT.document, command));
                expect(same).toBe(true);
            } finally {
                rmSync(dir, { recursive: true, force: true });
            }
        },
        60_000,
    );
});

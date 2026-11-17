/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Nothing in this repository points at a record its reader cannot open.
 *
 * A comment, a test title, a string on screen, a script's output or a
 * recorded fixture says what the code does and why in its own words. It does
 * not cite a numbered design document, finding, decision or plan step, a
 * sprint, a tracker id, the short name of an internal build, or a path into
 * a repository that is not published, and it does not tell the history of a
 * change. Public standards may be cited: `src/test/privateReferences.ts`
 * lists each one with its title, and holds the rules and the examples, which
 * are the same in every repository of the product.
 *
 * The Community package scans every text file outside `enterprise/`. The
 * Enterprise package runs this test again and scans the whole tree.
 *
 * Generated contract files are the one exception. They are generated from
 * the OpenAPI documents of the Control Plane and the gateway: their text is
 * the descriptions those components wrote, and it is not edited here. They
 * are scanned separately and held to a recorded ceiling that only goes down;
 * a file whose document is clean has a ceiling of 0. To print what arrives
 * through them:
 *
 *   PRIVATE_REFERENCES_REPORT=1 npx vitest run src/__tests__/noPrivateReferences.test.ts
 */

import { describe, expect, it } from 'vitest';

import {
    PUBLIC_RFCS,
    SCANNER_EXAMPLES,
    countByKind,
    formatHits,
    packageOf,
    privateReferences,
    scanFiles,
    scanText,
    textFiles,
    type Hit,
} from '../test/privateReferences';
import { enterpriseUnderTest } from '../test/sourceRoots';
import { toolFlag } from '../../scripts/tool-env.mjs';

/**
 * Generated from another component's document, never edited by hand: the
 * file, the document it is generated from, and the most references it may
 * carry. A ceiling is lowered when the count drops and never raised.
 */
const GENERATED_CONTRACT_FILES: Readonly<Record<string, { from: string; ceiling: number }>> = {
    'src/lib/generated/control-plane-api.d.ts': {
        from: 'the Control Plane’s Community OpenAPI document',
        ceiling: 0,
    },
    'src/lib/generated/core-proxy-api.d.ts': {
        from: 'the gateway’s proxy OpenAPI document',
        ceiling: 0,
    },
    'enterprise/src/lib/generated/control-plane-api.d.ts': {
        from: 'the Control Plane’s Enterprise OpenAPI document',
        ceiling: 0,
    },
};

/** Files the scan does not read, each for a reason. */
const NOT_SCANNED: readonly string[] = [
    // The rules and their examples spell out what the scan looks for.
    'src/test/privateReferences.ts',
];

const inScope = (file: string): boolean => enterpriseUnderTest() || packageOf(file) === 'community';

function summary(hits: readonly Hit[]): string {
    const lines: string[] = [];
    for (const pkg of ['community', 'enterprise'] as const) {
        const own = hits.filter((hit) => packageOf(hit.file) === pkg);
        if (own.length === 0) continue;
        lines.push(`${pkg}: ${own.length} (${countByKind(own).map(([kind, n]) => `${kind} ${n}`).join(', ')})`);
    }
    return lines.join('\n');
}

describe('no reference to a private record', () => {
    const files = textFiles().filter(inScope);
    const own = files.filter((file) => !(file in GENERATED_CONTRACT_FILES) && !NOT_SCANNED.includes(file));
    const generated = files.filter((file) => file in GENERATED_CONTRACT_FILES);

    it('reads the tree', () => {
        expect(own.length).toBeGreaterThan(900);
        expect(own).toContain('README.md');
        expect(own).toContain('package.json');
        expect(own).toContain('src/__tests__/noPrivateReferences.test.ts');
    });

    it('no file cites an internal document, finding, plan step, decision, sprint, tracker id, build or private repository, or tells history', () => {
        const hits = scanFiles(own);
        const report = hits.length
            ? 'A reader of this repository cannot open these records. Say what the code does and why, or delete the remark.\n' +
              `${summary(hits)}\n${formatHits(hits).join('\n')}`
            : '';
        expect(report).toBe('');
    }, 180_000);

    it('the generated contract files are the listed ones, and each carries no more than its recorded ceiling', () => {
        // Every file under a `generated` directory is listed: a new one is
        // either clean or added here with its source and its count.
        expect(files.filter((file) => /(^|\/)generated\//.test(file)).sort()).toEqual(
            Object.keys(GENERATED_CONTRACT_FILES).filter(inScope).sort(),
        );
        const over: string[] = [];
        const listing: string[] = [];
        for (const file of generated) {
            const hits = scanFiles([file]);
            const { from, ceiling } = GENERATED_CONTRACT_FILES[file];
            listing.push(`${file} (from ${from}): ${hits.length}, ceiling ${ceiling}`, ...formatHits(hits));
            if (hits.length > ceiling) over.push(`${file}: ${hits.length} references, ceiling ${ceiling}. They come from ${from}: fix them there and regenerate.`);
        }
        if (toolFlag('PRIVATE_REFERENCES_REPORT')) console.info(`References arriving through generated contract files\n${listing.join('\n')}`);
        expect(over.join('\n')).toBe('');
    }, 180_000);
});

describe('the scan for private references', () => {
    it.each(SCANNER_EXAMPLES)('reads %j', (text, expected) => {
        expect(privateReferences(text).map((r) => `${r.line}: ${r.kind}: ${r.cited}`)).toEqual(expected);
    });

    it('reads the name of a file as well as its text', () => {
        // Written in pieces so that this file passes its own rule.
        const name = ['src/lib/composition', 'Pha', 'se3.test.ts'].join('');
        expect(formatHits(scanText(name, 'export {};\n'))).toEqual([`${name}:0: phase of an internal plan: ${['Pha', 'se3'].join('')} (in the file's name)`]);
    });

    it('lists each public standard it allows with a title', () => {
        for (const [number, title] of Object.entries(PUBLIC_RFCS)) {
            expect(Number(number)).toBeGreaterThanOrEqual(1000);
            expect(title.length).toBeGreaterThan(8);
        }
    });
});

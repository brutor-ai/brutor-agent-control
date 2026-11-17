/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * No hand-rolled popover inside a table.
 *
 * A panel positioned `absolute … top-full` (or `bottom-full`) inside a table
 * cell is clipped by the table's scroll wrapper: on a short table the part
 * below the last row is simply cut off (the column help of a short list,
 * a help tip on a rightmost column).
 * `AnchoredPopover` renders into document.body with viewport-aware placement
 * and is the only supported way to anchor a panel to a table element.
 *
 * This parses every component with the TypeScript compiler and fails on a
 * JSX element whose className makes it an edge-anchored absolute panel AND
 * whose JSX ancestors include <table>, <thead>, <tbody>, <tr>, <th> or <td>.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import { ENTERPRISE_ROOT, NOT_SOURCE_DIRS, REPO_ROOT, scanRoots } from '../../../test/sourceRoots';

const ROOT = join(__dirname, '..', '..');
/** The Community components, and everything of the Enterprise tree. */
const underRoot = (root: string): string => (root === ENTERPRISE_ROOT ? root : ROOT);
const TABLE_TAGS = new Set(['table', 'thead', 'tbody', 'tr', 'th', 'td']);

function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
        const p = join(dir, name);
        if (statSync(p).isDirectory()) return name === '__tests__' || NOT_SOURCE_DIRS.has(name) ? [] : files(p);
        return p.endsWith('.tsx') ? [p] : [];
    });
}

function tagName(node: ts.JsxElement | ts.JsxSelfClosingElement): string {
    const t = ts.isJsxElement(node) ? node.openingElement.tagName : node.tagName;
    return t.getText();
}

function classText(node: ts.JsxElement | ts.JsxSelfClosingElement): string {
    const attrs = ts.isJsxElement(node) ? node.openingElement.attributes : node.attributes;
    for (const a of attrs.properties) {
        if (ts.isJsxAttribute(a) && a.name.getText() === 'className' && a.initializer) {
            return a.initializer.getText();
        }
    }
    return '';
}

function isEdgeAnchoredPanel(cls: string): boolean {
    if (!/\babsolute\b/.test(cls)) return false;
    if (!/\b(top-full|bottom-full)\b/.test(cls)) return false;
    // A 4px CSS triangle (tooltip arrow) is decoration, not a panel.
    if (/\bborder-transparent\b/.test(cls)) return false;
    return true;
}

export function findings(source: string, fileName: string): string[] {
    const sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const out: string[] = [];
    const walk = (node: ts.Node, inTable: boolean) => {
        let nowInTable = inTable;
        if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
            if (inTable && isEdgeAnchoredPanel(classText(node))) {
                const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
                out.push(`${fileName}:${line + 1}`);
            }
            if (TABLE_TAGS.has(tagName(node))) nowInTable = true;
        }
        ts.forEachChild(node, (c) => walk(c, nowInTable));
    };
    walk(sf, false);
    return out;
}

describe('popovers inside tables', () => {
    it('detects the pattern it guards against', () => {
        const bad = `const X = () => (<table><tbody><tr><td><div className="absolute left-0 top-full z-30">x</div></td></tr></tbody></table>);`;
        const ok = `const Y = () => (<div><div className="absolute left-0 top-full">menu</div></div>);`;
        expect(findings(bad, 'bad.tsx')).toHaveLength(1);
        expect(findings(ok, 'ok.tsx')).toHaveLength(0);
    });

    it('no component anchors an absolute panel inside a table (use AnchoredPopover)', () => {
        const all = scanRoots((root) => files(underRoot(root))).flatMap((f) =>
            findings(readFileSync(f, 'utf8'), f.slice(REPO_ROOT.length + 1)),
        );
        expect(all).toEqual([]);
        // Parses every component file; allow for a loaded CI runner.
    }, 60_000);
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Liveness is ONE computation and ONE presentation.
 *
 * The control plane computes the verdict (`state` + `reason`); every UI
 * surface renders it through `lib/livenessState.ts`. The bug class this
 * guards: a component re-deriving its own label from `has_ever_run` /
 * `is_silent` (/ `configured`). That derivation cannot express `idle` or
 * `unstarted`, so a service nobody called would read "Alive" on the status
 * strip while the fleet list and the scorer say something else.
 *
 * Two static rules over every non-test source file (TypeScript AST):
 *   1. No code READS `has_ever_run` or `is_silent`. Declaring them on a
 *      wire type (a property signature) is allowed — they are still sent.
 *   2. No second state→presentation map: an object literal keyed by three
 *      or more of the five liveness states may only live in
 *      `lib/livenessState.ts`.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import { NOT_SOURCE_DIRS, scanRoots, sourceName } from '../../test/sourceRoots';

import {
    LIVENESS_STATE_PRESENTATION,
    LIVENESS_STATES,
    livenessPresentation,
} from '../livenessState';

const DERIVATION_FLAGS = new Set(['has_ever_run', 'is_silent']);
const STATE_KEYS = new Set<string>(['alive', 'silent', 'idle', 'unstarted', 'not_configured']);
// Named as `sourceName` names it: the one file, in the Community tree.
const MAPPING_HOME = 'lib/livenessState.ts';

function files(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
        const p = join(dir, name);
        if (statSync(p).isDirectory()) {
            return name === '__tests__' || name === 'generated' || NOT_SOURCE_DIRS.has(name) ? [] : files(p);
        }
        if (/\.test\.tsx?$/.test(name) || name.endsWith('.d.ts')) return [];
        return /\.tsx?$/.test(name) ? [p] : [];
    });
}

function propName(name: ts.PropertyName | undefined): string | null {
    if (!name) return null;
    if (ts.isIdentifier(name) || ts.isStringLiteral(name)) return name.text;
    return null;
}

export function findings(source: string, fileName: string): string[] {
    const sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const out: string[] = [];
    const at = (n: ts.Node, what: string) => {
        const { line } = sf.getLineAndCharacterOfPosition(n.getStart());
        out.push(`${fileName}:${line + 1} ${what}`);
    };
    const walk = (node: ts.Node) => {
        // Rule 1 — reading one of the two flags: x.has_ever_run, x['is_silent'],
        // or destructuring `{ is_silent }`.
        if (ts.isPropertyAccessExpression(node) && DERIVATION_FLAGS.has(node.name.text)) {
            at(node, `reads ${node.name.text}`);
        } else if (
            ts.isElementAccessExpression(node) &&
            ts.isStringLiteral(node.argumentExpression) &&
            DERIVATION_FLAGS.has(node.argumentExpression.text)
        ) {
            at(node, `reads ${node.argumentExpression.text}`);
        } else if (ts.isBindingElement(node)) {
            const key = node.propertyName ? propName(node.propertyName) : node.name.getText();
            if (key && DERIVATION_FLAGS.has(key)) at(node, `destructures ${key}`);
        }
        // Rule 2 — a second liveness presentation map.
        if (ts.isObjectLiteralExpression(node) && fileName !== MAPPING_HOME) {
            const keys = node.properties
                .map((p) => (ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p) ? propName(p.name) : null))
                .filter((k): k is string => k !== null && STATE_KEYS.has(k));
            if (new Set(keys).size >= 3) at(node, `maps liveness states (${keys.join(', ')})`);
        }
        ts.forEachChild(node, walk);
    };
    walk(sf);
    return out;
}

describe('liveness: one mapping', () => {
    it('detects the patterns it guards against', () => {
        const bad = [
            `const a = s.has_ever_run ? 'Alive' : 'Never run';`,
            `const b = s['is_silent'];`,
            `const { is_silent } = status;`,
            `const LABELS = { alive: 'Alive', silent: 'Silent', idle: 'Idle' };`,
        ];
        for (const src of bad) expect(findings(src, 'components/Bad.tsx')).toHaveLength(1);

        const ok = [
            // Declaring the wire field is fine; reading it is not.
            `interface LivenessStatus { has_ever_run: boolean; is_silent: boolean }`,
            `const label = livenessPresentation(status.state).label;`,
            // Health states share one word with liveness — not a liveness map.
            `const HEALTH = { healthy: 'x', silent: 'y', degraded: 'z' };`,
        ];
        for (const src of ok) expect(findings(src, 'components/Ok.tsx')).toEqual([]);
        // The mapping's own home may define it.
        expect(findings(bad[3], MAPPING_HOME)).toEqual([]);
    });

    it('no source file derives a liveness label outside lib/livenessState.ts', () => {
        // Both editions' trees.
        const all = scanRoots((root) => files(root)).flatMap((f) => findings(readFileSync(f, 'utf8'), sourceName(f)));
        expect(all).toEqual([]);
    }, 180_000);
});

describe('liveness: the mapping itself', () => {
    it('covers exactly the five server states', () => {
        expect([...LIVENESS_STATES].sort()).toEqual([...STATE_KEYS].sort());
    });

    it.each([
        ['alive', 'Alive', 'ok'],
        ['silent', 'Silent', 'alert'],
        ['idle', 'Idle', 'neutral'],
        ['unstarted', 'Unstarted', 'neutral'],
        ['not_configured', 'Not configured', 'neutral'],
    ] as const)('%s → "%s" (%s)', (state, label, tone) => {
        const p = livenessPresentation(state);
        expect(p).toBe(LIVENESS_STATE_PRESENTATION[state]);
        expect(p.state).toBe(state);
        expect(p.label).toBe(label);
        expect(p.tone).toBe(tone);
        expect(p.icon).toBeTruthy();
    });

    it('only alive is green and only silent is a finding', () => {
        const tones = LIVENESS_STATES.map((s) => [s, livenessPresentation(s).tone]);
        expect(tones.filter(([, t]) => t === 'ok').map(([s]) => s)).toEqual(['alive']);
        expect(tones.filter(([, t]) => t === 'alert').map(([s]) => s)).toEqual(['silent']);
    });

    it('a missing or unknown state renders neutral "—", never green', () => {
        for (const s of [null, undefined, '', 'healthy', 'toString', '__proto__']) {
            const p = livenessPresentation(s);
            expect(p.label).toBe('—');
            expect(p.tone).toBe('neutral');
            expect(p.state).toBeNull();
        }
    });
});

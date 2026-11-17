/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Connection status is ONE mapping.
 *
 * The Simple graph, the Diagram and the List all show the system's
 * connection to each node. The bug class this guards: a view growing its
 * own status → colour/label map, so one view reads "Degraded" in orange
 * where another reads "Down" in red for the same connection — or paints
 * `idle` (no evidence) green.
 *
 * Two static rules over every non-test source file (TypeScript AST):
 *   1. No second status map: an object literal keyed by three or more of
 *      the four connection statuses may only live in
 *      `lib/compositionConnection.ts`.
 *   2. No view builds a status colour class itself: a string or template
 *      naming `composition-connection-<status>` (or
 *      `composition-connection-${…}`) may only live there too.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import { NOT_SOURCE_DIRS, scanRoots, sourceName } from '../../test/sourceRoots';

import {
  CONNECTION_STATUS_META,
  CONNECTION_STATUS_ORDER,
  NO_EVIDENCE,
  connectionTitle,
  statusMix,
} from '../compositionConnection';

const STATUS_KEYS = new Set<string>(['ok', 'degraded', 'down', 'idle']);
// Named as `sourceName` names it: the one file, in the Community tree.
const MAPPING_HOME = 'lib/compositionConnection.ts';
const STATUS_CLASS =
  /composition-connection-(ok|degraded|down|idle)\b|composition-connection-\$\{|composition-connection-$/;

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
  const sf = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX
  );
  const out: string[] = [];
  const at = (n: ts.Node, what: string) => {
    const { line } = sf.getLineAndCharacterOfPosition(n.getStart(sf));
    out.push(`${fileName}:${line + 1} ${what}`);
  };
  const visit = (n: ts.Node) => {
    if (ts.isObjectLiteralExpression(n)) {
      const keys = n.properties
        .map((p) =>
          ts.isPropertyAssignment(p) || ts.isShorthandPropertyAssignment(p)
            ? propName(p.name)
            : null
        )
        .filter((k): k is string => k !== null && STATUS_KEYS.has(k));
      if (new Set(keys).size >= 3)
        at(n, 'defines its own connection-status map');
    }
    if (
      (ts.isStringLiteral(n) ||
        ts.isNoSubstitutionTemplateLiteral(n) ||
        ts.isTemplateHead(n) ||
        ts.isTemplateMiddle(n) ||
        ts.isTemplateTail(n)) &&
      STATUS_CLASS.test(n.text)
    ) {
      at(n, `builds a connection-status colour class ("${n.text}")`);
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return out;
}

describe('connection status — one mapping', () => {
  it('no module but lib/compositionConnection.ts maps a connection status', () => {
    // Both editions' trees.
    const found = scanRoots((root) => files(root))
      .filter((p) => sourceName(p) !== MAPPING_HOME)
      .map((p) => [p, readFileSync(p, 'utf8')] as const)
      // Cheap pre-filter: a file that names neither a status class nor
      // two of the statuses cannot violate either rule.
      .filter(
        ([, src]) =>
          src.includes('composition-connection-') ||
          (/\bdegraded\b/.test(src) && /\bidle\b/.test(src))
      )
      .flatMap(([p, src]) => findings(src, sourceName(p)));
    expect(
      found,
      `Read CONNECTION_STATUS_META instead:\n${found.join('\n')}`
    ).toEqual([]);
  }, 60_000);

  it('the guard catches both shapes it exists for', () => {
    const bad = [
      "const C = { ok: 'text-green-700', degraded: 'text-orange-700', down: 'text-red-700', idle: 'text-gray-500' };",
      'const k = (s: string) => `composition-connection-${s}`;',
      "const d = 'composition-connection-down';",
    ].join('\n');
    const v = findings(bad, 'Bad.tsx');
    expect(
      v.filter((x) => x.includes('own connection-status map'))
    ).toHaveLength(1);
    expect(v.filter((x) => x.includes('colour class'))).toHaveLength(2);
    // The colour-neutral utilities are not a status map.
    expect(
      findings("const s = 'composition-connection-stroke';", 'Ok.tsx')
    ).toEqual([]);
  });

  it('covers every status, each with its own colour class and line style', () => {
    expect([...CONNECTION_STATUS_ORDER].sort()).toEqual(
      [...STATUS_KEYS].sort()
    );
    const classes = CONNECTION_STATUS_ORDER.map(
      (s) => CONNECTION_STATUS_META[s].className
    );
    expect(new Set(classes).size).toBe(4);
    for (const s of CONNECTION_STATUS_ORDER) {
      expect(CONNECTION_STATUS_META[s].className).toBe(
        `composition-connection-${s}`
      );
    }
    const styles = CONNECTION_STATUS_ORDER.map(
      (s) =>
        `${CONNECTION_STATUS_META[s].dash ?? 'solid'}|${CONNECTION_STATUS_META[s].glyph ?? ''}`
    );
    expect(new Set(styles).size).toBe(4);
  });

  it('states the one down rule the control plane applies', () => {
    expect(CONNECTION_STATUS_META.down.meaning).toContain(
      'the last 3 calls all failed'
    );
    expect(CONNECTION_STATUS_META.degraded.meaning).toContain(
      'One of the last 3 calls failed'
    );
    expect(CONNECTION_STATUS_META.idle.meaning).toContain(
      'not the same as all good'
    );
  });

  it('a missing status reads as no evidence, never ok', () => {
    expect(NO_EVIDENCE.status).toBe('idle');
  });

  it('titles and mixes', () => {
    expect(
      connectionTitle('Loan Core', {
        ...NO_EVIDENCE,
        status: 'down',
        reason: 'Last 3 calls failed',
      })
    ).toBe('Loan Core — Down: Last 3 calls failed');
    expect(
      statusMix([
        { connection: { ...NO_EVIDENCE, status: 'down' } },
        { connection: { ...NO_EVIDENCE, status: 'ok' } },
      ])
    ).toBe('1 ok, 1 down');
  });
});

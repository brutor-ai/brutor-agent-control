/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What an operator reads may not point at things an operator cannot see:
 * internal service and library names, database tables, or the mechanism a
 * feature happens to be built on. Say what the product does instead.
 *
 * The scan reads every string literal, template chunk and JSX text through
 * the TypeScript parser (comments are not strings, so they may name a
 * service or a table). `console.*` arguments are skipped: they are for
 * developers.
 *
 * References to records nobody outside can open (numbered design documents,
 * plan steps, sprints) are refused everywhere, comments included, by
 * `noPrivateReferences.test.ts`.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import { NOT_SOURCE_DIRS, scanRoots, sourceName } from '../test/sourceRoots';

const SKIP_DIRS = new Set(['__tests__', 'generated', 'test', 'tests', '__mocks__', ...NOT_SOURCE_DIRS]);

const INTERNAL =
    /\bgateway-(core|control)\b|\bPostgres NOTIFY\b|\bvia NOTIFY\b|\bLiteLLM\b|\bRust proxy\b|\bproxy_logs?\b|\busage_logs\b|\b(tenant_admin_sessions|system_operation_log|scheduler_runtime_state|action_records)\b/;

const RETIRED = /\bAI Gateway\b|\bAI Control Plane\b|\bPortkey\b|\bHelicone\b|\bAdmin123/;

function sourceFiles(dir: string, out: string[] = []): string[] {
    for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
            if (!SKIP_DIRS.has(entry)) sourceFiles(full, out);
        } else if (/\.tsx?$/.test(entry) && !/\.(test|spec|d)\.tsx?$/.test(entry)) {
            out.push(full);
        }
    }
    return out;
}

function isLogArgument(node: ts.Node): boolean {
    let cur: ts.Node | undefined = node.parent;
    while (cur && !ts.isCallExpression(cur) && !ts.isStatement(cur)) cur = cur.parent;
    return !!cur && ts.isCallExpression(cur) && /^(console|log|logger)\./.test(cur.expression.getText());
}

export function readableStrings(file: string, source: string): Array<{ line: number; text: string }> {
    const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    const out: Array<{ line: number; text: string }> = [];
    const visit = (node: ts.Node): void => {
        let text: string | null = null;
        if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
            const p = node.parent;
            if (!ts.isImportDeclaration(p) && !ts.isExportDeclaration(p) && !ts.isLiteralTypeNode(p) && !isLogArgument(node)) text = node.text;
        } else if (
            node.kind === ts.SyntaxKind.TemplateHead ||
            node.kind === ts.SyntaxKind.TemplateMiddle ||
            node.kind === ts.SyntaxKind.TemplateTail
        ) {
            if (!isLogArgument(node)) text = (node as ts.TemplateLiteralLikeNode).text;
        } else if (ts.isJsxText(node)) {
            text = node.text;
        }
        // A string without a space is an identifier, a route or a key, not prose.
        if (text && /\s/.test(text.trim())) {
            out.push({ line: sf.getLineAndCharacterOfPosition(node.getStart()).line + 1, text: text.replace(/\s+/g, ' ').trim() });
        }
        ts.forEachChild(node, visit);
    };
    visit(sf);
    return out;
}

describe('UI text does not reference implementation details', () => {
    // Both editions' sources: what an operator reads is held to the rule
    // wherever it is written.
    const files = scanRoots((root) => sourceFiles(root));

    it('scans the app', () => {
        expect(files.length).toBeGreaterThan(50);
    });

    it('no readable string names an internal document, phase, service, library or table', () => {
        const offenders: string[] = [];
        for (const file of files) {
            for (const { line, text } of readableStrings(file, readFileSync(file, 'utf8'))) {
                const hit = INTERNAL.exec(text);
                if (hit) offenders.push(`${sourceName(file)}:${line}: "${text.slice(0, 90)}" (${hit[0]})`);
            }
        }
        const report = offenders.length
            ? 'Operators cannot look up internal service names or tables. ' +
              `Describe what the product does instead.\n${offenders.join('\n')}`
            : '';
        expect(report).toBe('');
    }, 180_000);

    // The product is "Brutor Agent Control". What it was called before,
    // another vendor's product held up for comparison, and a password that
    // no installation has are not for an operator to read either.
    it('no readable string carries an earlier product name, a vendor comparison or a stock password', () => {
        const offenders: string[] = [];
        for (const file of files) {
            for (const { line, text } of readableStrings(file, readFileSync(file, 'utf8'))) {
                const hit = RETIRED.exec(text);
                if (hit) offenders.push(`${sourceName(file)}:${line}: "${text.slice(0, 90)}" (${hit[0]})`);
            }
        }
        expect(offenders.join('\n')).toBe('');
    }, 180_000);

    it('the retired-wording scan finds each kind and leaves the product name alone', () => {
        const src = [
            "const a = 'Welcome to Brutor AI Gateway';",
            'const b = <h2>AI Control Plane Admin</h2>;',
            "const d = 'the conservative consensus (Portkey, Helicone)';",
            'const e = <p>Password: Admin123!</p>;',
            "const ok = 'Brutor Agent Control Admin Console';",
            "const alsoOk = 'The gateway forwards the call';",
        ].join('\n');
        const hits = readableStrings('x.tsx', src).filter((s) => RETIRED.test(s.text)).map((s) => s.text);
        expect(hits).toEqual([
            'Welcome to Brutor AI Gateway',
            'AI Control Plane Admin',
            'the conservative consensus (Portkey, Helicone)',
            'Password: Admin123!',
        ]);
    });

    it('the scanner reads prose and ignores comments, identifiers and logs', () => {
        const src = [
            '// gateway-core in a comment is fine',
            "const a = 'the Rust proxy forwards the call';",
            "const key = 'proxy_logs';",
            "console.warn('gateway-core did not answer in time');",
            'const el = <p>Stored in the usage_logs table</p>;',
            "const ok = 'The public key is served as a JWKS (RFC 7517).';",
        ].join('\n');
        const hits = readableStrings('x.tsx', src).filter((s) => INTERNAL.test(s.text)).map((s) => s.text);
        expect(hits).toEqual(['the Rust proxy forwards the call', 'Stored in the usage_logs table']);
    });
});

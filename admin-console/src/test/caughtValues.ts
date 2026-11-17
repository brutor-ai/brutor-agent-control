/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Where the console drops a caught value, for the guards that hold the list
 * of such places (`caughtValuesDropped.test.ts`, one per edition).
 *
 * A place where a failure is caught is a `catch` clause or the rejection
 * handler of `.catch(…)` / `.then(_, …)`. It DROPS the caught value when it
 * neither throws nor reads the value anywhere but in a call of `console`:
 * nothing the other side said can reach the user from there. Either the
 * failure is not shown at all, or what is shown is a sentence of the
 * console's own with the Control Plane's reason left out.
 *
 * A place that reads the value (hands it to `errorMessage`, to a reporter,
 * to a state setter) or throws is not listed: what it does with it is that
 * code's to answer for and a test's to hold.
 *
 * `Promise.allSettled` is a third such place, with no clause to find: its
 * rejected members are held to the same rule by `droppedSettledIn`.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

import ts from 'typescript';

import { NOT_SOURCE_DIRS, REPO_ROOT } from './sourceRoots';

export interface DroppedCatch {
    /** The file, from the repository root. */
    file: string;
    /** The function or the variable the place is written in; empty at the top of a module. */
    in: string;
    /** The start of the code whose failure is caught, on one line. */
    guards: string;
}

const NOT_PRODUCTION = new Set([...NOT_SOURCE_DIRS, '__tests__', 'generated', 'ui-style', 'test']);

/** The production source files under `root`: no test, no test helper, no generated or vendored file. */
export function productionSources(root: string, out: string[] = []): string[] {
    for (const name of readdirSync(root).sort()) {
        const full = path.join(root, name);
        if (statSync(full).isDirectory()) {
            if (!NOT_PRODUCTION.has(name)) productionSources(full, out);
        } else if (/\.tsx?$/.test(name) && !/\.(test|spec)\.tsx?$/.test(name) && !name.endsWith('.d.ts')) {
            out.push(full);
        }
    }
    return out;
}

function isRejectionHandler(fn: ts.Node): boolean {
    const call = fn.parent;
    if (!ts.isCallExpression(call) || !ts.isPropertyAccessExpression(call.expression)) return false;
    const method = call.expression.name.text;
    return (method === 'catch' && call.arguments[0] === fn) || (method === 'then' && call.arguments[1] === fn);
}

function isConsoleCall(node: ts.Node): node is ts.CallExpression {
    return (
        ts.isCallExpression(node) &&
        ts.isPropertyAccessExpression(node.expression) &&
        ts.isIdentifier(node.expression.expression) &&
        node.expression.expression.text === 'console'
    );
}

const oneLine = (text: string): string => text.replace(/\s+/g, ' ').trim();

/** The name of what `node` is written in. */
function enclosing(node: ts.Node, source: ts.SourceFile): string {
    for (let at = node.parent; at; at = at.parent) {
        if ((ts.isFunctionDeclaration(at) || ts.isMethodDeclaration(at)) && at.name) return at.name.getText(source);
        if (ts.isVariableDeclaration(at) && ts.isIdentifier(at.name) && at.initializer) {
            const init = at.initializer;
            if (ts.isArrowFunction(init) || ts.isFunctionExpression(init) || ts.isCallExpression(init)) return at.name.text;
        }
    }
    return '';
}

/** Every place of `file` that drops a caught value, in the order it is written. */
export function droppedCatchesIn(file: string): DroppedCatch[] {
    const text = readFileSync(file, 'utf8');
    const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    const found: DroppedCatch[] = [];

    const visit = (node: ts.Node): void => {
        let body: ts.Node | undefined;
        let name: string | undefined;
        let guarded: ts.Node | undefined;
        if (ts.isCatchClause(node)) {
            body = node.block;
            const bound = node.variableDeclaration?.name;
            name = bound && ts.isIdentifier(bound) ? bound.text : undefined;
            guarded = node.parent.tryBlock;
        } else if ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && isRejectionHandler(node)) {
            body = node.body;
            const bound = node.parameters[0]?.name;
            name = bound && ts.isIdentifier(bound) ? bound.text : undefined;
            guarded = ((node.parent as ts.CallExpression).expression as ts.PropertyAccessExpression).expression;
        }
        if (body && guarded) {
            let read = false;
            let throws = false;
            const walk = (at: ts.Node, inConsole: boolean): void => {
                if (ts.isThrowStatement(at)) throws = true;
                if (isConsoleCall(at)) {
                    at.arguments.forEach((argument) => walk(argument, true));
                    return;
                }
                if (name && !inConsole && ts.isIdentifier(at) && at.text === name) {
                    const isPropertyName = ts.isPropertyAccessExpression(at.parent) && at.parent.name === at;
                    if (!isPropertyName) read = true;
                }
                ts.forEachChild(at, (child) => walk(child, inConsole));
            };
            walk(body, false);
            if (!read && !throws) {
                found.push({
                    file: path.relative(REPO_ROOT, file).split(path.sep).join('/'),
                    in: enclosing(node, source),
                    guards: oneLine(guarded.getText(source)).replace(/^\{\s*/, '').slice(0, 70),
                });
            }
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
    return found;
}

/** Every place under `root` that drops a caught value. */
export function droppedCatches(root: string): DroppedCatch[] {
    return productionSources(root).flatMap(droppedCatchesIn);
}

export interface DroppedSettled {
    /** The file, from the repository root. */
    file: string;
    /** The function or the variable the call is written in; empty at the top of a module. */
    in: string;
    /**
     * The members whose rejection is not read, by the name the result is
     * bound to; `#n` for the nth member where the pattern binds none, and
     * `(all)` where nothing takes the results.
     */
    members: string[];
}

function isAllSettled(node: ts.Node): node is ts.CallExpression {
    return (
        ts.isCallExpression(node) &&
        ts.isPropertyAccessExpression(node.expression) &&
        node.expression.name.text === 'allSettled' &&
        ts.isIdentifier(node.expression.expression) &&
        node.expression.expression.text === 'Promise'
    );
}

const isFunction = (node: ts.Node): node is ts.ArrowFunction | ts.FunctionExpression | ts.FunctionDeclaration | ts.MethodDeclaration =>
    ts.isArrowFunction(node) || ts.isFunctionExpression(node) || ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node);

/** Where the results of `call` are bound and the code that can read them; `'handed on'` where they leave the function, `null` where nothing takes them. */
function settledBinding(call: ts.CallExpression, source: ts.SourceFile): { name: ts.BindingName; scope: ts.Node } | 'handed on' | null {
    let at: ts.Node = call;
    while (ts.isParenthesizedExpression(at.parent) || ts.isAwaitExpression(at.parent)) at = at.parent;
    const parent = at.parent;
    if (ts.isVariableDeclaration(parent) && parent.initializer === at) {
        let scope: ts.Node = parent;
        while (scope.parent && !isFunction(scope)) scope = scope.parent;
        return { name: parent.name, scope: isFunction(scope) ? (scope.body ?? scope) : source };
    }
    if (ts.isPropertyAccessExpression(parent) && parent.name.text === 'then' && ts.isCallExpression(parent.parent)) {
        const handler = parent.parent.arguments[0];
        if (handler && (ts.isArrowFunction(handler) || ts.isFunctionExpression(handler))) {
            const first = handler.parameters[0];
            return first ? { name: first.name, scope: handler.body } : null;
        }
        return 'handed on';
    }
    if (ts.isReturnStatement(parent) || (ts.isArrowFunction(parent) && parent.body === at)) return 'handed on';
    return null;
}

/**
 * Every `Promise.allSettled` of `file` with a member whose rejection is not
 * read, in the order written. `text` is the file's source, read from the
 * file when it is left out.
 *
 * `Promise.allSettled` catches for the code that calls it: a member that
 * was rejected is a caught value, held in the member's `reason`. A member is
 * READ when its `reason` is read anywhere but in a call of `console`
 * (handed to the read-failure marks, to `failureSentence`, thrown), or when
 * the member itself is handed on (put in a list, passed to a function) to
 * code of the same function that reads the `reason` of what it is handed.
 * Asking a member for its `status` or its `value` reads nothing of the
 * failure.
 */
export function droppedSettledIn(file: string, text: string = readFileSync(file, 'utf8')): DroppedSettled[] {
    const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    const found: DroppedSettled[] = [];
    const report = (call: ts.CallExpression, members: string[]): void => {
        found.push({ file: path.relative(REPO_ROOT, file).split(path.sep).join('/'), in: enclosing(call, source), members });
    };

    const visit = (node: ts.Node): void => {
        if (isAllSettled(node)) {
            const binding = settledBinding(node, source);
            if (binding === null) report(node, ['(all)']);
            else if (binding !== 'handed on') {
                // The names whose `reason` is read, and the names used as a value of their own.
                const reasonRead = new Set<string>();
                const handedOn = new Set<string>();
                const walk = (at: ts.Node): void => {
                    if (isConsoleCall(at)) return;
                    if (ts.isPropertyAccessExpression(at) && at.name.text === 'reason' && ts.isIdentifier(at.expression)) {
                        reasonRead.add(at.expression.text);
                    }
                    if (ts.isIdentifier(at)) {
                        const asObject = ts.isPropertyAccessExpression(at.parent) && at.parent.expression === at;
                        const asPropertyName = ts.isPropertyAccessExpression(at.parent) && at.parent.name === at;
                        const declared = ts.isBindingElement(at.parent) || ts.isParameter(at.parent) || ts.isVariableDeclaration(at.parent);
                        if (!asObject && !asPropertyName && !declared) handedOn.add(at.text);
                    }
                    ts.forEachChild(at, walk);
                };
                walk(binding.scope);

                const dropped: string[] = [];
                if (ts.isIdentifier(binding.name)) {
                    // The whole list under one name: read where any reason is read in the code that has it.
                    if (reasonRead.size === 0) dropped.push(binding.name.text);
                } else if (ts.isArrayBindingPattern(binding.name)) {
                    const names = binding.name.elements.map((element) =>
                        ts.isBindingElement(element) && ts.isIdentifier(element.name) ? element.name.text : null,
                    );
                    const bound = new Set(names.filter((name): name is string => name !== null));
                    const readElsewhere = [...reasonRead].some((name) => !bound.has(name));
                    const rest = binding.name.elements.some((element) => ts.isBindingElement(element) && element.dotDotDotToken !== undefined);
                    const list = node.arguments[0];
                    const count = list && ts.isArrayLiteralExpression(list) ? list.elements.length : names.length;
                    for (let i = 0; i < Math.max(count, names.length); i++) {
                        const name = names[i] ?? null;
                        if (name === null) {
                            if (!(rest && i >= names.length - 1)) dropped.push(`#${i + 1}`);
                        } else if (!reasonRead.has(name) && !(handedOn.has(name) && readElsewhere)) {
                            dropped.push(name);
                        }
                    }
                }
                if (dropped.length > 0) report(node, dropped);
            }
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
    return found;
}

/** Every `Promise.allSettled` under `root` with a member whose rejection is not read. */
export function droppedSettled(root: string): DroppedSettled[] {
    return productionSources(root).flatMap((file) => droppedSettledIn(file));
}

/**
 * Why a listed place may drop the caught value. Every entry of a recorded
 * list names one of these.
 */
export const REASONS = {
    local: 'Nothing was asked of a server: a parse, the storage or the clipboard of the browser, a date or an address being formatted or checked. Where the user is told, the sentence is the form\'s own.',
    'shown by the callee': 'The function called adds the failure, with what the other side said, to the errors of the page before it throws.',
    'shown by the caller': 'The callback called shows the failure, with what the other side said, before it throws.',
    'follow-up': 'A read or a clean-up after an action whose own outcome was already told: a reload, a statistic, a resolution.',
    probe: 'The request asks whether something is there or can be reached, and its failure is the answer the code goes on with.',
    'own sentence': 'The user is told in the console\'s own sentence, which says what is done instead (a fallback).',
    badge: 'A count shown beside a list that reads itself and says its own failure: without the count the badge is absent or at its last value.',
    appearance: 'How the console is drawn (the theme of the tenant): without it the default is used.',
    'help text': 'Explanations behind an information mark: without them the mark has no text and the page is whole.',
} as const;

/**
 * The reasons of a read that fails quietly because what it fetches is
 * decoration. Every other read that fails says so where its data would be
 * (`ReadFailureNotice`). Each guard caps how many places name one of these.
 */
export const DECORATION: readonly string[] = ['badge', 'appearance', 'help text'] satisfies readonly (keyof typeof REASONS)[];

export type Reason = keyof typeof REASONS;

/** A recorded list: file, then each place of it in the order written, with its reason. */
export type RecordedDrops = Record<string, (Omit<DroppedCatch, 'file'> & { why: Reason })[]>;

/** `found` as a recorded list holds it, without the reasons. */
export function byFile(found: DroppedCatch[]): Record<string, Omit<DroppedCatch, 'file'>[]> {
    const out: Record<string, Omit<DroppedCatch, 'file'>[]> = {};
    for (const { file, ...place } of found) (out[file] ??= []).push(place);
    return out;
}

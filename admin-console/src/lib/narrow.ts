/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Narrowing helpers for values the type system cannot vouch for.
 *
 * Everything crossing a runtime boundary — `JSON.parse`, a caught error, a
 * `postMessage` payload, an SSE frame — arrives as `any` or `unknown`. A
 * value checked defensively (`typeof x.layer === 'string' ? ... :
 * 'unknown'`) but declared `any` has its checks thrown away by TypeScript,
 * and every downstream expression inherits `any`.
 *
 * Declaring the value `unknown` and narrowing through these helpers makes the
 * same checks load-bearing: the compiler verifies that a field was proven
 * to be a string before it is used as one.
 */

/** True when `value` is a non-null, non-array object with string keys. */
export function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Read a string property, or `undefined` if absent or another type. */
export function getString(value: unknown, key: string): string | undefined {
    if (!isRecord(value)) return undefined;
    const v = value[key];
    return typeof v === 'string' ? v : undefined;
}

/** Read a finite number property, or `undefined`. `NaN` is not a number here. */
export function getNumber(value: unknown, key: string): number | undefined {
    if (!isRecord(value)) return undefined;
    const v = value[key];
    return typeof v === 'number' && Number.isFinite(v) ? v : undefined;
}

/** Read a boolean property, or `undefined`. */
export function getBoolean(value: unknown, key: string): boolean | undefined {
    if (!isRecord(value)) return undefined;
    const v = value[key];
    return typeof v === 'boolean' ? v : undefined;
}

/** Read a nested record property, or `undefined`. */
export function getRecord(
    value: unknown,
    key: string
): Record<string, unknown> | undefined {
    if (!isRecord(value)) return undefined;
    const v = value[key];
    return isRecord(v) ? v : undefined;
}

/** Read an array property, or `undefined`. Elements stay `unknown`. */
export function getArray(value: unknown, key: string): unknown[] | undefined {
    if (!isRecord(value)) return undefined;
    const v = value[key];
    return Array.isArray(v) ? (v as unknown[]) : undefined;
}

/**
 * Best-effort human-readable message for a caught value.
 *
 * `catch` binds `unknown` under `strict`, and a thrown value is not required
 * to be an `Error` — a rejected fetch, a string throw and a JSON error body
 * all reach here. Returns `fallback` only when nothing usable is present, so
 * callers never render "undefined" to a user.
 */
export function errorMessage(value: unknown, fallback = 'Unknown error'): string {
    if (typeof value === 'string' && value.length > 0) return value;
    if (value instanceof Error && value.message) return value.message;
    const message = getString(value, 'message');
    if (message) return message;
    const detail = getString(value, 'detail');
    if (detail) return detail;
    const error = getString(value, 'error');
    if (error) return error;
    return fallback;
}

/**
 * What failed and why, for the user: `what`, then what the other side said
 * (`Failed to load the models: The gateway is not reachable`). `what` alone
 * where the failure carries no sentence.
 */
export function failureSentence(what: string, value: unknown): string {
    const reason = errorMessage(value, '');
    return reason ? `${what}: ${reason}` : what;
}

/**
 * The reasons of several failures of one action, for one sentence: each
 * reason once with how many failures gave it (`3× forbidden; 1× timeout`),
 * the most frequent first, at most `limit` kinds, then how many kinds were
 * left out. A failure that says nothing counts under "no reason given".
 */
export function groupedReasons(failures: readonly unknown[], limit = 3): string {
    const counts = new Map<string, number>();
    for (const failure of failures) {
        const reason = errorMessage(failure, '') || 'no reason given';
        counts.set(reason, (counts.get(reason) ?? 0) + 1);
    }
    // A stable sort: reasons given as often keep the order they came in.
    const kinds = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    const told = kinds.slice(0, limit).map(([reason, count]) => `${count}× ${reason}`);
    const rest = kinds.length - told.length;
    if (rest > 0) told.push(`and ${rest} other reason${rest === 1 ? '' : 's'}`);
    return told.join('; ');
}

/**
 * What a caught value is, for a line on the browser's console: the name of
 * its class and, where it carries one, its numeric status or code
 * (`GatewayAPIError 403`, `TypeError`).
 *
 * Never its message, its stack or any other field: the message of a failed
 * request repeats what the other side answered, and that answer can hold a
 * credential or what a user or a tool wrote. The user reads the sentence in
 * the page; the console's line says which kind of failure it was.
 */
export function errorKind(value: unknown): string {
    const name = value instanceof Error ? value.name : typeof value;
    const status = errorStatus(value);
    return status === undefined ? name : `${name} ${status}`;
}

/**
 * HTTP-ish status carried by an error, from any of the shapes the gateway and
 * `fetch` produce (`status`, `code`, `statusCode`). `undefined` when absent.
 */
export function errorStatus(value: unknown): number | undefined {
    return (
        getNumber(value, 'status') ??
        getNumber(value, 'code') ??
        getNumber(value, 'statusCode')
    );
}

/** `JSON.parse` that yields `unknown` instead of `any`, and never throws. */
export function parseJson(text: string): unknown {
    try {
        return JSON.parse(text) as unknown;
    } catch {
        return undefined;
    }
}

/**
 * A member of a string enum as the string it is. An enum's type is not
 * assignable to the union of its values, which is how a generated request
 * type names them: this gives the value that type, so the compiler compares
 * the enum's values with the ones the contract accepts.
 */
export function enumValue<E extends string>(value: E): `${E}` {
    return value as `${E}`;
}

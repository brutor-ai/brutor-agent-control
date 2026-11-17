/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Settings of a stored document (a group's limits or governance) that the
 * console no longer offers, because the gateway does not act on them in this
 * release. The Control Plane refuses a value for one; a document stored
 * before keeps it.
 *
 * A form shows each one the document holds, read-only (`storedUnavailable`),
 * and leaves it out of what it hands on to be saved (`withoutUnavailable`):
 * the save removes it, and the form says so.
 */

/** One such setting: where it is in the document, and what the console calls it. */
export interface UnavailableSetting {
    /** The keys from the document's root to the value, e.g. `['execution', 'memory_limit_mb']`. */
    path: readonly string[];
    label: string;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
    typeof value === 'object' && value !== null && !Array.isArray(value);

function valueAt(doc: unknown, path: readonly string[]): unknown {
    let at: unknown = doc;
    for (const key of path) {
        if (!isObject(at)) return undefined;
        at = at[key];
    }
    return at;
}

/** The settings of `settings` that `doc` holds a value for (not null, not undefined), with the value. */
export function storedUnavailable(
    doc: unknown,
    settings: readonly UnavailableSetting[],
): Array<UnavailableSetting & { value: unknown }> {
    return settings.flatMap((setting) => {
        const value = valueAt(doc, setting.path);
        return value === undefined || value === null ? [] : [{ ...setting, value }];
    });
}

/** `doc` without the settings of `settings`; a section left empty by that is left out too. */
export function withoutUnavailable<T>(doc: T, settings: readonly UnavailableSetting[]): T {
    if (!isObject(doc)) return doc;
    const copy: Record<string, unknown> = structuredClone(doc);
    for (const { path } of settings) {
        const parents: Array<[Record<string, unknown>, string]> = [];
        let at: unknown = copy;
        for (const key of path.slice(0, -1)) {
            if (!isObject(at)) break;
            parents.push([at, key]);
            at = at[key];
        }
        if (!isObject(at)) continue;
        delete at[path[path.length - 1]];
        for (let i = parents.length - 1; i >= 0; i--) {
            const [parent, key] = parents[i];
            const child = parent[key];
            if (isObject(child) && Object.keys(child).length === 0) delete parent[key];
            else break;
        }
    }
    return copy as T;
}

/** A stored value as the read-only note shows it. */
export function shownValue(value: unknown): string {
    if (typeof value === 'boolean') return value ? 'on' : 'off';
    if (typeof value === 'string' || typeof value === 'number') return String(value);
    return JSON.stringify(value);
}

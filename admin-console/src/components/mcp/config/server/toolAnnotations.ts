/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * MCP tool annotations (behaviour hints) as the operator should read them.
 *
 * The gateway core gates on these: a tool declaring `readOnlyHint: true` is a
 * read; anything else — including a tool that declares nothing — is treated
 * as side-effecting (fail closed, MCP spec defaults: destructive, open-world)
 * and is held or denied under approval-required / restricted autonomy.
 * `annotations` is upstream-chosen JSON, so every read narrows it.
 */

import { getBoolean } from '../../../../lib/narrow';

export type HintTone = 'safe' | 'risk' | 'neutral';

export interface HintBadge {
    key: string;
    label: string;
    tone: HintTone;
}

export const UNANNOTATED_WARNING = 'No annotations declared — treated as side-effecting';

const HINT_KEYS = ['readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint'] as const;

/** The behaviour hints a tool declares, as `[key, value]` pairs. */
export function declaredHints(annotations: unknown): [string, boolean][] {
    const out: [string, boolean][] = [];
    for (const k of HINT_KEYS) {
        const v = getBoolean(annotations, k);
        if (v !== undefined) out.push([k, v]);
    }
    return out;
}

/**
 * Readable badges for the hints a tool DECLARES. `null` when it declares
 * none — the caller must then say out loud that the tool is treated as
 * side-effecting rather than show an empty row.
 */
export function annotationBadges(annotations: unknown): HintBadge[] | null {
    if (declaredHints(annotations).length === 0) return null;
    const readOnly = getBoolean(annotations, 'readOnlyHint');
    const destructive = getBoolean(annotations, 'destructiveHint');
    const idempotent = getBoolean(annotations, 'idempotentHint');
    const openWorld = getBoolean(annotations, 'openWorldHint');
    const badges: HintBadge[] = [];
    if (readOnly === true) badges.push({ key: 'readOnlyHint', label: 'Read-only', tone: 'safe' });
    // The spec makes destructiveHint meaningless on a read-only tool.
    if (readOnly !== true && destructive === true) {
        badges.push({ key: 'destructiveHint', label: 'Destructive', tone: 'risk' });
    }
    if (readOnly !== true && destructive === false) {
        badges.push({ key: 'destructiveHint', label: 'Non-destructive', tone: 'neutral' });
    }
    if (idempotent === true) badges.push({ key: 'idempotentHint', label: 'Idempotent', tone: 'neutral' });
    if (openWorld === true) badges.push({ key: 'openWorldHint', label: 'Open world', tone: 'risk' });
    if (openWorld === false) badges.push({ key: 'openWorldHint', label: 'Closed world', tone: 'neutral' });
    return badges;
}

/** Mirrors the core's `autonomy::classify`: only a declared read-only tool is a read. */
export function isReadOnlyTool(annotations: unknown): boolean {
    return getBoolean(annotations, 'readOnlyHint') === true;
}

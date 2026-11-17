/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * How long a human-approval hold stays open.
 *
 * The core resolves every hold's window through one function
 * (`services/approval_window.rs`): the policy's own window if it states one
 * (a capability filter row, the group-wide MCP default, the skill execution
 * mode) > the Resource Group chain, **min-wins** (a parent's window is a
 * ceiling) > the tenant default > the platform's 300 s. The control plane
 * refuses a child window longer than the one it inherits, and this module
 * mirrors that check so the console says so before Save.
 */

import type { ApiResponse } from './api/controlPlaneTypes';
import { humanDurationMs } from './aiSystemTypes';

export type ApprovalWindow = ApiResponse<'/v1/admin/resource-groups/{group_id}/approval-window', 'get'>;

/** The control plane's bounds (a database CHECK, and the core's clamp). */
export const APPROVAL_WINDOW_MIN_SECONDS = 60;
export const APPROVAL_WINDOW_MAX_SECONDS = 7 * 24 * 3600;
export const PLATFORM_DEFAULT_APPROVAL_WINDOW_SECONDS = 300;

/** Common choices, in seconds. */
export const APPROVAL_WINDOW_PRESETS: ReadonlyArray<{ label: string; seconds: number }> = [
    { label: '1 hour', seconds: 3600 },
    { label: '8 hours', seconds: 8 * 3600 },
    { label: '24 hours', seconds: 24 * 3600 },
    { label: '3 days', seconds: 3 * 24 * 3600 },
    { label: '7 days', seconds: APPROVAL_WINDOW_MAX_SECONDS },
];

/** `86400` → `1 d 0 h`; `300` → `5 m 00 s`. */
export function windowText(seconds: number): string {
    return humanDurationMs(seconds * 1000);
}

const SOURCE_TEXT: Record<string, string> = {
    resource_group: 'set on this group or inherited from an ancestor',
    tenant: 'the tenant default',
    platform: 'the platform default',
    policy: "the policy's own window",
};

/** "Holds last 1 d 0 h — inherited from 'Borealis' (the smallest window in the chain)." */
export function effectiveText(w: ApprovalWindow): string {
    const base = `Holds that name no window of their own last ${windowText(w.effective_seconds)}`;
    if (w.effective_source === 'resource_group') {
        if (w.approval_window_seconds != null && w.approval_window_seconds === w.effective_seconds) {
            return `${base} — set on this group.`;
        }
        const from = w.inherited_from_group_name ?? w.inherited_from_group_id ?? 'an ancestor';
        return `${base} — inherited from '${from}' (the smallest window in the chain).`;
    }
    return `${base} — ${SOURCE_TEXT[w.effective_source] ?? w.effective_source}.`;
}

/** Why `seconds` may not be saved on this group, or null. Mirrors the
 *  control plane (range → 422, longer than inherited → 400). */
export function windowProblem(seconds: number | null, inherited: number | null | undefined): string | null {
    if (seconds == null) return null;
    if (!Number.isInteger(seconds)) return 'Enter a whole number of minutes.';
    if (seconds < APPROVAL_WINDOW_MIN_SECONDS || seconds > APPROVAL_WINDOW_MAX_SECONDS) {
        return 'The window must be between 1 minute and 7 days.';
    }
    if (inherited != null && seconds > inherited) {
        return `Longer than the ${windowText(inherited)} inherited from a parent group — a group may only shorten the window it inherits.`;
    }
    return null;
}

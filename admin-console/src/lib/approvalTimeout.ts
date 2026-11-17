/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The approval timeout of a held call: how long it waits for a person's
 * decision. The Control Plane accepts a whole number of seconds from 1 to
 * 604800 (7 days) and refuses anything else, on every surface that holds a
 * call: an MCP capability, MCP governance, skill governance and A2A
 * governance. The forms bound the field to the same range and say it beside
 * a value outside it.
 */

export const APPROVAL_TIMEOUT_MIN_SECONDS = 1;
export const APPROVAL_TIMEOUT_MAX_SECONDS = 604_800;

export const APPROVAL_TIMEOUT_RANGE = `An approval timeout is a whole number of seconds from ${APPROVAL_TIMEOUT_MIN_SECONDS} to ${APPROVAL_TIMEOUT_MAX_SECONDS} (7 days).`;

/** Why `value` is not an approval timeout, or null when it is one or none is set. */
export function approvalTimeoutProblem(value: unknown): string | null {
    if (value === undefined || value === null) return null;
    const ok =
        typeof value === 'number' &&
        Number.isInteger(value) &&
        value >= APPROVAL_TIMEOUT_MIN_SECONDS &&
        value <= APPROVAL_TIMEOUT_MAX_SECONDS;
    return ok ? null : APPROVAL_TIMEOUT_RANGE;
}

/**
 * What is wrong with the approval timeout a policy holds, or `null`.
 *
 * A policy holds it at the top (`approval_timeout_seconds`) or in one of
 * its sections (`capability.approval_timeout_seconds`). The form that
 * edits a policy shows this sentence under the field; the page or the
 * dialog that holds the form keeps its Save off while there is one, so a
 * timeout the Control Plane refuses is not sent.
 */
export function governanceApprovalTimeoutProblem(governance: unknown): string | null {
    if (typeof governance !== 'object' || governance === null || Array.isArray(governance)) return null;
    for (const [key, value] of Object.entries(governance)) {
        const problem =
            key === 'approval_timeout_seconds'
                ? approvalTimeoutProblem(value)
                : typeof value === 'object' && value !== null && !Array.isArray(value) && 'approval_timeout_seconds' in value
                  ? approvalTimeoutProblem((value as { approval_timeout_seconds: unknown }).approval_timeout_seconds)
                  : null;
        if (problem) return problem;
    }
    return null;
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * An MCP server's `max_concurrent_requests`: how many requests to the server
 * one gateway instance has in flight at once (every MCP request counts; a
 * listener stream gives its place back once its answer starts). Each instance
 * of a cluster allows this many. The Control Plane takes a positive whole
 * number, or null for its default of 10.
 */

/** What the gateway allows when the server states none. */
export const MAX_CONCURRENT_REQUESTS_DEFAULT = 10;

export const MAX_CONCURRENT_REQUESTS_LABEL = 'Max concurrent requests (per gateway instance)';

export const MAX_CONCURRENT_REQUESTS_PROBLEM = `Max concurrent requests must be a positive whole number, or empty for ${MAX_CONCURRENT_REQUESTS_DEFAULT}.`;

/**
 * The text of the field as a save sends it: null when empty (the default),
 * the number when it is a positive whole number, and undefined when it is
 * neither, which is not sent.
 */
export function maxConcurrentRequestsFromText(text: string): number | null | undefined {
    const typed = text.trim();
    if (typed === '') return null;
    if (!/^\d+$/.test(typed)) return undefined;
    const value = Number(typed);
    return Number.isSafeInteger(value) && value >= 1 ? value : undefined;
}

/** The field's text for a stored value: empty for none. */
export function maxConcurrentRequestsText(value: number | null | undefined): string {
    return value === null || value === undefined ? '' : String(value);
}

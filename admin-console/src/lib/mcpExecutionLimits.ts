/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The MCP execution and data limits the gateway enforces, as an
 * administrator states them on a group (`mcp_global_limits`) or on the
 * group's binding of a server: what each one does, and the values it can
 * read. A stated value the gateway cannot read (not a positive number, or
 * not a whole one for a concurrency allowance) refuses every call of the
 * group to the server, so the console keeps such a value from being saved.
 */

/** Where the limits are stated: the group's own, or its binding of one server. */
export type McpLimitsPlace = 'group' | 'binding';

interface ExecutionField {
    section: 'execution' | 'data';
    field: string;
    label: string;
    whole: boolean;
}

const FIELDS: readonly ExecutionField[] = [
    { section: 'execution', field: 'timeout_seconds', label: 'Timeout (seconds)', whole: false },
    { section: 'execution', field: 'max_concurrency_per_user', label: 'Max Concurrency (per user)', whole: true },
    { section: 'execution', field: 'max_concurrency_per_group', label: 'Max Concurrency (per group)', whole: true },
    { section: 'data', field: 'max_payload_size_mb', label: 'Max Payload Size (MB)', whole: false },
];

const TIMEOUT =
    "The total time of a call or of a POST stream; with the server's own timeout it also caps the wait for the answer to start. The listener stream is exempt. Past it the call is refused with 504, and a streamed call ends with a timeout event.";

const PER_USER = (scope: string) =>
    `Simultaneous tools/call per identity, in this order: the agent, the user (all of a user's keys share one allowance), the identity-provider subject, the group key; any other caller shares one allowance. Only tools/call counts: protocol traffic and the listener stream do not. Past it: 429 rate_limit_exceeded, scope ${scope}, with Retry-After.`;

const PAYLOAD =
    'Caps the request and the answer. A request is never above 10 MiB, and with no value here an answer is not capped. A request too large is refused with 413; an answer too large with 502 payload_too_large (a streamed answer ends with an error event).';

/** What each limit does, keyed `section.field`. */
export const MCP_LIMIT_HELP: Record<McpLimitsPlace, Record<string, string>> = {
    group: {
        'execution.timeout_seconds': TIMEOUT,
        'execution.max_concurrency_per_user': PER_USER('group-user'),
        'execution.max_concurrency_per_group':
            'Simultaneous tools/call in this group, across the cluster. A value set here applies to each descendant group separately. Past it: 429 rate_limit_exceeded, scope group, with Retry-After.',
        'data.max_payload_size_mb': PAYLOAD,
    },
    binding: {
        'execution.timeout_seconds': TIMEOUT,
        'execution.max_concurrency_per_user': PER_USER('server-user'),
        'execution.max_concurrency_per_group':
            'Simultaneous tools/call to this server from this group, across the cluster. Past it: 429 rate_limit_exceeded, scope server, with Retry-After.',
        'data.max_payload_size_mb': PAYLOAD,
    },
};

const CONSEQUENCE = 'The gateway refuses every call of the group to the server while a stated limit is not one.';

/**
 * The first stated execution or data limit the gateway cannot read, as a
 * sentence; null when every stated one is readable. An unset field
 * (absent, null or undefined) is no limit and is fine.
 */
export function mcpExecutionLimitsProblem(limits: unknown): string | null {
    if (typeof limits !== 'object' || limits === null) return null;
    const doc = limits as Record<string, unknown>;
    for (const { section, field, label, whole } of FIELDS) {
        const sec = doc[section];
        if (typeof sec !== 'object' || sec === null) continue;
        const value = (sec as Record<string, unknown>)[field];
        if (value === undefined || value === null) continue;
        const readable =
            typeof value === 'number' && Number.isFinite(value) && (whole ? Number.isInteger(value) && value >= 1 : value > 0);
        if (!readable) {
            const need = whole ? 'a whole number of at least 1' : 'a positive number';
            return `${label} must be ${need}. ${CONSEQUENCE}`;
        }
    }
    return null;
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tool approval requests: an agent's call that policy parked for a person.
 * The shapes are the control plane's (`/v1/admin/approvals`), taken from
 * the generated API types so they cannot drift from it.
 */

import type { components } from './generated/control-plane-api';

export type ToolApprovalSummary = components['schemas']['ToolApprovalSummary'];
export type ToolApprovalDetail = components['schemas']['ToolApprovalDetail'];
export type ToolApprovalListResponse = components['schemas']['ToolApprovalListResponse'];
export type ToolApprovalResolveResponse = components['schemas']['ToolApprovalResolveResponse'];

/**
 * The list filters the control plane accepts (`status_filter`). `pending`
 * excludes a request past its deadline; `expired` includes it; `approved`
 * includes an approval whose token the agent has already redeemed.
 */
export const TOOL_APPROVAL_FILTERS = ['pending', 'approved', 'rejected', 'expired', 'all'] as const;
export type ToolApprovalFilter = (typeof TOOL_APPROVAL_FILTERS)[number];

/** What a request is, for a person: derived by the server from status and deadline. */
/** The holds of a list raised by one call: those with the same trace root, or a hold on its own. */
export interface ApprovalCall<T> {
    /** The call's trace root; null for a hold raised outside a traced call. */
    traceRoot: string | null;
    holds: T[];
}

/**
 * The holds of a list by the call that raised them. Holds with the same
 * `originating_trace_root_id` are one call's, together where the first of
 * them is in the list; a hold without one is a call of its own.
 */
export function approvalsByCall<T extends { originating_trace_root_id?: string | null }>(items: readonly T[]): ApprovalCall<T>[] {
    const calls: ApprovalCall<T>[] = [];
    const byRoot = new Map<string, ApprovalCall<T>>();
    for (const item of items) {
        const root = item.originating_trace_root_id ?? null;
        const known = root === null ? undefined : byRoot.get(root);
        if (known) {
            known.holds.push(item);
            continue;
        }
        const call = { traceRoot: root, holds: [item] };
        calls.push(call);
        if (root !== null) byRoot.set(root, call);
    }
    return calls;
}

export type ToolApprovalDecision = 'pending' | 'approved' | 'rejected' | 'expired' | 'unknown';

const DECISIONS: readonly string[] = ['pending', 'approved', 'rejected', 'expired'];

/**
 * The server's `decision`, or `unknown` for a value this console does not
 * know. Never falls back to the raw `status`: a lapsed request's status is
 * still `pending`, and showing it as actionable is the bug the server-side
 * `decision` exists to prevent.
 */
export function approvalDecision(item: { decision?: string | null }): ToolApprovalDecision {
    return item.decision && DECISIONS.includes(item.decision) ? (item.decision as ToolApprovalDecision) : 'unknown';
}

/**
 * What kind of call is held. The gateway core keeps every hold in one table
 * and tells them apart by `capability_type` (its `agent_approval.rs`):
 * `a2a_call` for a call to another agent, `skill_exec` for a skill execution,
 * anything else (tool, resource, prompt, …) for a capability of an MCP server.
 */
export type ToolApprovalSurface = 'mcp' | 'a2a' | 'skill';

export function approvalSurface(item: { capability_type: string }): ToolApprovalSurface {
    if (item.capability_type === 'a2a_call') return 'a2a';
    if (item.capability_type === 'skill_exec') return 'skill';
    return 'mcp';
}

/**
 * What is held and on what, in words. `server_name` is the MCP server's name
 * only for an MCP hold: for an A2A hold it is the agent card's id, for a
 * skill hold the skill's id. `mcpNoun` is what precedes an MCP server's name.
 */
export function approvalSubject(item: { capability_type: string; server_name?: string | null }, mcpNoun = ''): string {
    const ref = item.server_name;
    switch (approvalSurface(item)) {
        case 'a2a':
            return `Agent call${ref ? ` to agent ${ref}` : ''}`;
        case 'skill':
            return `Skill execution${ref ? ` of skill ${ref}` : ''}`;
        default:
            return `${item.capability_type}${ref ? ` on ${mcpNoun}${ref}` : ''}`;
    }
}

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);

/** A held skill execution, as a skill. */
export interface SkillHoldView {
    /** The skill's id (the hold's `server_name`), when the request carries it. */
    id: string | null;
    /** The skill's name: the hold's `capability_name` when that is not the id again, else null. */
    name: string | null;
    /** The Resource Group the skill is to run in. */
    groupId: string;
    /** The settings that ask for a person before the skill runs, in words; empty when the request names none. */
    requiredBy: string[];
}

/** The sources of a skill's execution mode, as the gateway names them, in words. */
const EXECUTION_MODE_SOURCES: Record<string, string> = {
    group: "the Resource Group's skill governance",
    skill: "the skill's own setting",
    assignment: "the skill's assignment to a Resource Group",
};

/**
 * A skill hold read as a skill; null for a hold of another kind.
 *
 * The gateway writes which settings asked for a person in the request's
 * metadata, as `execution_mode.required_by`: a list of `group`, `skill` and
 * `assignment`. A source this console has no words for is shown by its name.
 * A hold raised by the agent's grant or by an argument policy has no such
 * list, and none is shown.
 */
export function skillHold(item: {
    capability_type: string;
    capability_name: string;
    server_name?: string | null;
    requester_group_id: string;
    request_metadata?: unknown;
}): SkillHoldView | null {
    if (approvalSurface(item) !== 'skill') return null;
    const id = item.server_name || null;
    const mode = isRecord(item.request_metadata) ? item.request_metadata.execution_mode : null;
    const sources = isRecord(mode) && Array.isArray(mode.required_by) ? (mode.required_by as unknown[]) : [];
    return {
        id,
        name: item.capability_name && item.capability_name !== id ? item.capability_name : null,
        groupId: item.requester_group_id,
        requiredBy: sources
            .filter((source): source is string => typeof source === 'string' && source !== '')
            .map((source) => EXECUTION_MODE_SOURCES[source] ?? source),
    };
}

/** One part of an A2A message, as the Approvals detail shows it. */
export interface MessagePartView {
    /** "Text", "Data", "File at an address", "File content", or "Other". */
    kind: string;
    /** The part's file name and media type, where it states them. */
    labels: string[];
    /** What to show of it. Inline file content is never shown: its length is. */
    body: string;
}

/** A held A2A message: its parts, and its metadata when it has some. */
export interface HeldMessageView {
    parts: MessagePartView[];
    metadata: Record<string, unknown> | null;
}

function messagePartView(part: unknown): MessagePartView {
    if (!isRecord(part)) return { kind: 'Other', labels: [], body: JSON.stringify(part, null, 2) ?? '' };
    const labels = [part.filename, part.mediaType].filter((label): label is string => typeof label === 'string' && label !== '');
    if (typeof part.text === 'string') return { kind: 'Text', labels, body: part.text };
    if ('data' in part) return { kind: 'Data', labels, body: JSON.stringify(part.data, null, 2) ?? '' };
    if (typeof part.url === 'string') return { kind: 'File at an address', labels, body: part.url };
    if (typeof part.raw === 'string') return { kind: 'File content', labels, body: `${part.raw.length} characters of base64, not shown` };
    return { kind: 'Other', labels, body: JSON.stringify(part, null, 2) };
}

/**
 * The arguments of a held skill execution that came over A2A, as a message.
 *
 * The gateway holds such a call with what the message asks for: exactly
 * `{parts, metadata}`, every part of the message and its metadata. A part
 * carries one of `text`, `data` (any JSON value), `url` or `raw` (inline
 * content, base64), and may state a `filename` and a `mediaType`.
 *
 * Null for anything else: a hold of another kind (an MCP tool may take
 * `parts` as an ordinary argument), and a skill input that has other fields.
 */
export function heldMessage(item: { capability_type: string; call_arguments?: unknown }): HeldMessageView | null {
    if (approvalSurface(item) !== 'skill') return null;
    const held = item.call_arguments;
    if (!isRecord(held) || !Array.isArray(held.parts)) return null;
    if (Object.keys(held).some((key) => key !== 'parts' && key !== 'metadata')) return null;
    const metadata = isRecord(held.metadata) && Object.keys(held.metadata).length > 0 ? held.metadata : null;
    return { parts: (held.parts as unknown[]).map(messagePartView), metadata };
}

/** The permission that lets an administrator approve or reject. */
export const TOOL_APPROVAL_MANAGE = 'tool_approval:manage';
/** The permission that shows the queue. */
export const TOOL_APPROVAL_READ = 'tool_approval:read';

/**
 * How long a pending request still has, in words, or null once the deadline
 * has passed (or there is none to count down to).
 */
export function timeLeft(expiresAt: string | null | undefined, nowMs: number): string | null {
    if (!expiresAt) return null;
    const ms = Date.parse(expiresAt) - nowMs;
    if (!Number.isFinite(ms) || ms <= 0) return null;
    const minutes = Math.floor(ms / 60_000);
    if (minutes < 1) return 'under a minute';
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours} h ${minutes % 60} min`;
    return `${Math.floor(hours / 24)} days`;
}

/** How long ago a request was raised, in words. */
export function timeSince(createdAt: string | null | undefined, nowMs: number): string {
    if (!createdAt) return '';
    const ms = nowMs - Date.parse(createdAt);
    if (!Number.isFinite(ms)) return '';
    const minutes = Math.max(0, Math.floor(ms / 60_000));
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours} h ago`;
    return `${Math.floor(hours / 24)} days ago`;
}

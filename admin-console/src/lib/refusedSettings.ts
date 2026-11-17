/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the console takes out of a limits or governance document before it
 * sends one: every setting the Control Plane refuses because the gateway
 * does not enforce it in this release (its `release_scope`).
 *
 * A document read from the Control Plane may hold such a value, stored
 * before it was refused. A form that hands the document back as it was
 * read (a save without an edit, or an edit of another field) would send it
 * again, and the save would be refused. Every form that sends one of these
 * documents passes it through `withoutRefused` first, so the save goes
 * through and the value is removed.
 *
 * The rules, per document:
 *
 *   mcp_limits        `execution.memory_limit_mb`, `execution.cpu_limit_percent`,
 *                     `data.max_context_window_percent`, `data.max_rows_per_query`,
 *                     `data.max_items_per_response`; and every other key the
 *                     contract's `McpLimitsDocument` does not define, at the top
 *                     and in `execution` and `data` (the document takes no other)
 *   media_limits      the whole `embedding` mode; in every mode each `monthly_*`
 *                     key, `daily_warning_percent`, and `hard_stop: false`
 *   media_governance  `embedding.max_input_tokens_per_item`,
 *                     `audio_transcription.require_prompt_guardrail: false`
 *   llm_limits        `budget.hard_stop: false`
 *   llm_governance    `context_window.max_input_tokens` unless `enforce` is `reject`
 *                     (an input cannot be shortened, so under `clamp` it only warned)
 *   a2a_limits        `invocations.daily_warning_percent`,
 *                     `invocations.monthly_warning_percent`, `invocations.hard_stop: false`
 *
 * A section left empty by a removal is removed too. The document given is
 * not changed.
 */

import type { components } from './generated/control-plane-api';

export type RefusedDocument = 'mcp_limits' | 'media_limits' | 'media_governance' | 'llm_limits' | 'llm_governance' | 'a2a_limits';

type Doc = Record<string, unknown>;

const isObject = (value: unknown): value is Doc => typeof value === 'object' && value !== null && !Array.isArray(value);

type McpLimitsDocument = components['schemas']['McpLimitsDocument'];
type Section<K extends keyof McpLimitsDocument> = keyof NonNullable<McpLimitsDocument[K]>;

/**
 * The keys an MCP limits document defines (the contract's `McpLimitsDocument`,
 * `McpExecutionLimitsDocument` and `McpDataLimitsDocument`), each list held to
 * the generated type both ways. `frequency` and `per_tool` are open records.
 */
const MCP_DOCUMENT_KEYS = ['execution', 'data', 'frequency', 'per_tool'] as const satisfies readonly (keyof McpLimitsDocument)[];
const MCP_SECTION_KEYS = {
    execution: ['timeout_seconds', 'max_concurrency_per_user', 'max_concurrency_per_group'],
    data: ['max_payload_size_mb'],
} as const satisfies { execution: readonly Section<'execution'>[]; data: readonly Section<'data'>[] };
const EVERY_MCP_KEY_IS_LISTED: [
    Exclude<keyof McpLimitsDocument, (typeof MCP_DOCUMENT_KEYS)[number]>,
    Exclude<Section<'execution'>, (typeof MCP_SECTION_KEYS.execution)[number]>,
    Exclude<Section<'data'>, (typeof MCP_SECTION_KEYS.data)[number]>,
] extends [never, never, never] ? true : false = true;
void EVERY_MCP_KEY_IS_LISTED;

/** `doc[section]` without the keys `refused` says of it; the section is dropped when that leaves it empty. */
function prune(doc: Doc, section: string, refused: (key: string, value: unknown) => boolean): void {
    const block = doc[section];
    if (!isObject(block)) return;
    const kept = Object.fromEntries(Object.entries(block).filter(([key, value]) => !refused(key, value)));
    if (Object.keys(kept).length === 0) delete doc[section];
    else doc[section] = kept;
}

const STRIP: Record<RefusedDocument, (doc: Doc) => void> = {
    mcp_limits: (doc) => {
        for (const key of Object.keys(doc)) {
            if (!(MCP_DOCUMENT_KEYS as readonly string[]).includes(key)) delete doc[key];
        }
        for (const section of ['execution', 'data'] as const) {
            const defined: readonly string[] = MCP_SECTION_KEYS[section];
            // The five refused settings are among the keys the document does not define.
            prune(doc, section, (key) => !defined.includes(key));
        }
    },
    media_limits: (doc) => {
        delete doc.embedding;
        for (const mode of Object.keys(doc)) {
            prune(
                doc,
                mode,
                (key, value) => key.startsWith('monthly_') || key === 'daily_warning_percent' || (key === 'hard_stop' && value === false),
            );
        }
    },
    media_governance: (doc) => {
        prune(doc, 'embedding', (key) => key === 'max_input_tokens_per_item');
        prune(doc, 'audio_transcription', (key, value) => key === 'require_prompt_guardrail' && value === false);
    },
    llm_limits: (doc) => {
        prune(doc, 'budget', (key, value) => key === 'hard_stop' && value === false);
    },
    llm_governance: (doc) => {
        const ctx = doc.context_window;
        if (!isObject(ctx) || (ctx.enforce ?? 'clamp') === 'reject') return;
        prune(doc, 'context_window', (key) => key === 'max_input_tokens');
    },
    a2a_limits: (doc) => {
        prune(
            doc,
            'invocations',
            (key, value) => key === 'daily_warning_percent' || key === 'monthly_warning_percent' || (key === 'hard_stop' && value === false),
        );
    },
};

/** `doc` without what the Control Plane refuses in a `kind` document in this release. */
export function withoutRefused<T>(kind: RefusedDocument, doc: T): T {
    if (!isObject(doc)) return doc;
    const copy: Doc = structuredClone(doc);
    STRIP[kind](copy);
    return copy as T;
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A stand-in for the Control Plane's save-time refusals of settings the
 * gateway does not enforce (`app/services/release_scope.py` at the contract's
 * commit), written from that module and not from the console's own helper,
 * so a test can say whether the Control Plane would take a document the
 * console sends. Each check answers the sentence of the refusal, or null.
 */

import type { RefusedDocument } from '../lib/refusedSettings';

type Doc = Record<string, unknown>;

const isObject = (value: unknown): value is Doc => typeof value === 'object' && value !== null && !Array.isArray(value);
const set = (value: unknown) => value !== null && value !== undefined;
const notAvailable = (setting: string) => `${setting} is not available in this release`;

const CHECKS: Record<RefusedDocument, (doc: Doc) => string | null> = {
    mcp_limits: (doc) => {
        for (const [section, key] of [
            ['execution', 'memory_limit_mb'],
            ['execution', 'cpu_limit_percent'],
            ['data', 'max_context_window_percent'],
            ['data', 'max_rows_per_query'],
            ['data', 'max_items_per_response'],
        ]) {
            const block = doc[section];
            if (isObject(block) && set(block[key])) return notAvailable(`MCP limit ${section}.${key}`);
        }
        return null;
    },
    media_limits: (doc) => {
        for (const [mode, block] of Object.entries(doc)) {
            if (!isObject(block)) continue;
            const keys = Object.keys(block).filter((key) => set(block[key])).sort();
            if (mode === 'embedding' && keys.length > 0) return notAvailable(`Media limit embedding.${keys[0]}`);
            for (const key of keys) {
                if (key.startsWith('monthly_') && key !== 'monthly_warning_percent') return notAvailable(`Media limit ${mode}.${key}`);
                if (key === 'daily_warning_percent' || key === 'monthly_warning_percent') return notAvailable(`Media limit ${mode}.${key}`);
            }
            if (block.hard_stop === false) return notAvailable(`Media limit ${mode}.hard_stop: false`);
        }
        return null;
    },
    media_governance: (doc) => {
        const embedding = doc.embedding;
        if (isObject(embedding) && set(embedding.max_input_tokens_per_item)) {
            return notAvailable('Media governance embedding.max_input_tokens_per_item');
        }
        const transcription = doc.audio_transcription;
        if (isObject(transcription) && transcription.require_prompt_guardrail === false) {
            return notAvailable('Media governance audio_transcription.require_prompt_guardrail: false');
        }
        return null;
    },
    llm_limits: (doc) => (isObject(doc.budget) && doc.budget.hard_stop === false ? notAvailable('LLM budget hard_stop: false') : null),
    llm_governance: (doc) => {
        const ctx = doc.context_window;
        return isObject(ctx) && set(ctx.max_input_tokens) && (ctx.enforce ?? 'clamp') !== 'reject'
            ? notAvailable("context_window.max_input_tokens with enforce 'clamp'")
            : null;
    },
    a2a_limits: (doc) => {
        const inv = doc.invocations;
        if (!isObject(inv)) return null;
        for (const key of ['daily_warning_percent', 'monthly_warning_percent']) {
            if (set(inv[key])) return notAvailable(`A2A limit invocations.${key}`);
        }
        return inv.hard_stop === false ? notAvailable('A2A limit invocations.hard_stop: false') : null;
    },
};

/** The Control Plane's refusal of `doc` as a `kind` document, or null when it takes it. */
export function controlPlaneRefusal(kind: RefusedDocument, doc: unknown): string | null {
    return isObject(doc) ? CHECKS[kind](doc) : null;
}

/** Stored documents of each kind, each with every refused setting set and one setting the gateway enforces. */
export const STORED_WITH_REFUSED: Record<RefusedDocument, Doc> = {
    mcp_limits: {
        execution: { timeout_seconds: 30, memory_limit_mb: 512, cpu_limit_percent: 50 },
        data: { max_payload_size_mb: 4, max_context_window_percent: 20, max_rows_per_query: 100, max_items_per_response: 50 },
    },
    media_limits: {
        image_generation: { daily_images: 10, monthly_images: 100, daily_warning_percent: 80, monthly_warning_percent: 90, hard_stop: false },
        embedding: { daily_input_tokens: 1000 },
    },
    media_governance: {
        embedding: { max_input_tokens_per_item: 512 },
        audio_transcription: { require_prompt_guardrail: false },
        image_generation: { max_images_per_request: 2 },
    },
    llm_limits: { budget: { daily_limit_usd: 10, hard_stop: false } },
    llm_governance: { context_window: { max_input_tokens: 8000, max_output_tokens: 1000, enforce: 'clamp' } },
    a2a_limits: { invocations: { daily_limit: 100, daily_warning_percent: 80, monthly_warning_percent: 90, hard_stop: false } },
};

/** What `STORED_WITH_REFUSED[kind]` keeps once the refused settings are out. */
export const STORED_KEPT: Record<RefusedDocument, Doc> = {
    mcp_limits: { execution: { timeout_seconds: 30 }, data: { max_payload_size_mb: 4 } },
    media_limits: { image_generation: { daily_images: 10 } },
    media_governance: { image_generation: { max_images_per_request: 2 } },
    llm_limits: { budget: { daily_limit_usd: 10 } },
    llm_governance: { context_window: { max_output_tokens: 1000, enforce: 'clamp' } },
    a2a_limits: { invocations: { daily_limit: 100 } },
};

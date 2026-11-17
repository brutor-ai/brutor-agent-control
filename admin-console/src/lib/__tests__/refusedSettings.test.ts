/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The one helper every form that sends a limits or governance document
 * passes it through: it takes out what the Control Plane refuses in this
 * release, and nothing the gateway enforces.
 */

import { describe, expect, it } from 'vitest';

import { withoutRefused, type RefusedDocument } from '../refusedSettings';
import { STORED_KEPT, STORED_WITH_REFUSED, controlPlaneRefusal } from '../../test/controlPlaneRefusal';

const KINDS = Object.keys(STORED_WITH_REFUSED) as RefusedDocument[];

describe('withoutRefused', () => {
    it.each(KINDS)('%s: takes out every refused setting, keeps the rest, and the Control Plane takes what is left', (kind) => {
        const stored = STORED_WITH_REFUSED[kind];
        expect(controlPlaneRefusal(kind, stored)).not.toBeNull();
        const before = JSON.stringify(stored);
        const sent = withoutRefused(kind, stored);
        expect(sent).toEqual(STORED_KEPT[kind]);
        expect(controlPlaneRefusal(kind, sent)).toBeNull();
        // The document given is not changed.
        expect(JSON.stringify(stored)).toBe(before);
    });

    it('keeps what the gateway enforces under the same names', () => {
        expect(withoutRefused('llm_governance', { context_window: { max_input_tokens: 8000, enforce: 'reject' } })).toEqual({
            context_window: { max_input_tokens: 8000, enforce: 'reject' },
        });
        expect(withoutRefused('llm_limits', { budget: { hard_stop: true } })).toEqual({ budget: { hard_stop: true } });
        expect(withoutRefused('a2a_limits', { invocations: { hard_stop: true, monthly_limit: 5 } })).toEqual({
            invocations: { hard_stop: true, monthly_limit: 5 },
        });
        expect(withoutRefused('media_governance', { audio_transcription: { require_prompt_guardrail: true } })).toEqual({
            audio_transcription: { require_prompt_guardrail: true },
        });
    });

    it('leaves a document that is not an object as it is', () => {
        expect(withoutRefused('mcp_limits', null)).toBeNull();
        expect(withoutRefused('mcp_limits', {})).toEqual({});
    });
});

describe('withoutRefused: an MCP limits document holds only what the contract defines', () => {
    it('takes out every key the document does not define, at the top and in execution and data', () => {
        const stored = {
            cost: { daily_usd: 5 },
            execution: { timeout_seconds: 5, retries: 3 },
            data: { max_payload_size_mb: 2, compression: 'gzip' },
            frequency: { max_calls_per_minute: 10, daily_warning_percent: 80 },
            per_tool: { search: { max_calls_per_minute: 2 } },
        };
        expect(withoutRefused('mcp_limits', stored)).toEqual({
            execution: { timeout_seconds: 5 },
            data: { max_payload_size_mb: 2 },
            frequency: { max_calls_per_minute: 10, daily_warning_percent: 80 },
            per_tool: { search: { max_calls_per_minute: 2 } },
        });
    });

    it('drops a section left empty', () => {
        expect(withoutRefused('mcp_limits', { execution: { retries: 3 }, data: { compression: 'gzip' } })).toEqual({});
    });
});

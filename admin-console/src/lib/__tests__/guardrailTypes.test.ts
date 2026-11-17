/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The surfaces of a guardrail: the names the gateway asks for (`chat_input`,
 * `embeddings_input`, …, `a2a_outbound`), and the coarse groups the lists
 * show for them.
 */

import { readFileSync } from 'node:fs';

import { describe, it, expect } from 'vitest';

import { apiContract } from '../../test/apiContract';
import {
    ALL_CHECK_SURFACES,
    GUARDRAIL_ACTIONS,
    LLM_INPUT_MODE_SURFACES,
    LLM_TEXT_OUTPUT_MODE_SURFACES,
    SURFACE_METADATA,
    countActiveSurfaces,
    countEnabledChecks,
    getActionConfig,
    surfaceGroupLabels,
    type GuardrailAction,
    type GuardrailConfigResponse,
    type GuardrailSurfaceToggles,
} from '../guardrailTypes';

const surfaces = (on: Record<string, boolean> = {}): GuardrailSurfaceToggles =>
    Object.fromEntries(
        [...ALL_CHECK_SURFACES, 'mcp_registration'].map((s) => [s, on[s] ?? false]),
    ) as unknown as GuardrailSurfaceToggles;

const config = (over: Partial<GuardrailConfigResponse> = {}): GuardrailConfigResponse =>
    ({
        surfaces: surfaces(),
        builtin: {
            pii: { enabled: false },
            prompt_injection: { enabled: false },
            jailbreak: { enabled: false },
            toxic_content: { enabled: false },
            banned_words: [],
            banned_patterns: [],
        },
        providers: [],
        ...over,
    }) as unknown as GuardrailConfigResponse;

describe('surfaceGroupLabels', () => {
    it('shows LLM for any per-mode LLM surface, not just chat', () => {
        const llmModes = [...LLM_INPUT_MODE_SURFACES, ...LLM_TEXT_OUTPUT_MODE_SURFACES];
        const missing = llmModes.filter(
            (s) => !surfaceGroupLabels(surfaces({ [s]: true })).includes('LLM'),
        );
        expect(missing).toEqual([]);
        expect(surfaceGroupLabels(surfaces({ moderation_input: true }))).toEqual(['LLM']);
    });

    it('names MCP, Skill and the two A2A directions separately', () => {
        expect(surfaceGroupLabels(surfaces({ mcp_output: true }))).toEqual(['MCP']);
        expect(surfaceGroupLabels(surfaces({ skill_input: true }))).toEqual(['Skill']);
        expect(surfaceGroupLabels(surfaces({ a2a_inbound: true }))).toEqual(['A2A in']);
        expect(surfaceGroupLabels(surfaces({ a2a_outbound: true }))).toEqual(['A2A out']);
    });

    it('does not count MCP registration as an MCP traffic surface', () => {
        // Registration scanning is a catalog check, not inline tool traffic.
        expect(surfaceGroupLabels(surfaces({ mcp_registration: true }))).toEqual([]);
    });

    it('lists every active group once, in a stable order', () => {
        expect(
            surfaceGroupLabels(
                surfaces({
                    chat_input: true,
                    chat_output: true,
                    mcp_input: true,
                    skill_output: true,
                    a2a_inbound: true,
                    a2a_outbound: true,
                }),
            ),
        ).toEqual(['LLM', 'MCP', 'Skill', 'A2A in', 'A2A out']);
    });

    it('is empty for no surfaces at all', () => {
        expect(surfaceGroupLabels(null)).toEqual([]);
        expect(surfaceGroupLabels(undefined)).toEqual([]);
        expect(surfaceGroupLabels({})).toEqual([]);
        expect(surfaceGroupLabels(surfaces())).toEqual([]);
    });

    it('accepts the loose record the effective-guardrails endpoint returns', () => {
        expect(surfaceGroupLabels({ chat_input: true, mcp_input: true })).toEqual(['LLM', 'MCP']);
    });
});

describe('countActiveSurfaces', () => {
    it('counts groups, not individual toggles', () => {
        expect(
            countActiveSurfaces(
                config({ surfaces: surfaces({ chat_input: true, chat_output: true, batch_input: true }) }),
            ),
        ).toBe(1);
    });

    it('counts MCP registration as part of the MCP group', () => {
        // Unlike the badge list, the surface COUNT does include registration.
        expect(
            countActiveSurfaces(config({ surfaces: surfaces({ mcp_registration: true }) })),
        ).toBe(1);
    });

    it('adds up the five groups independently', () => {
        expect(
            countActiveSurfaces(
                config({
                    surfaces: surfaces({
                        chat_input: true,
                        mcp_input: true,
                        skill_input: true,
                        a2a_inbound: true,
                        a2a_outbound: true,
                    }),
                }),
            ),
        ).toBe(5);
    });

    it('is zero when nothing is switched on', () => {
        expect(countActiveSurfaces(config())).toBe(0);
    });
});

describe('countEnabledChecks', () => {
    it('is zero for a config that checks nothing', () => {
        expect(countEnabledChecks(config())).toBe(0);
    });

    it('counts each enabled builtin once', () => {
        expect(
            countEnabledChecks(
                config({
                    builtin: {
                        pii: { enabled: true },
                        prompt_injection: { enabled: true },
                        jailbreak: { enabled: false },
                        toxic_content: { enabled: true },
                        banned_words: [],
                        banned_patterns: [],
                    },
                } as unknown as Partial<GuardrailConfigResponse>),
            ),
        ).toBe(3);
    });

    it('counts a non-empty word or pattern list as one check each', () => {
        expect(
            countEnabledChecks(
                config({
                    builtin: {
                        pii: { enabled: false },
                        prompt_injection: { enabled: false },
                        jailbreak: { enabled: false },
                        toxic_content: { enabled: false },
                        banned_words: ['x', 'y'],
                        banned_patterns: ['^z'],
                    },
                } as unknown as Partial<GuardrailConfigResponse>),
            ),
        ).toBe(2);
    });

    it('adds only the enabled external providers', () => {
        expect(
            countEnabledChecks(
                config({
                    providers: [
                        { enabled: true },
                        { enabled: false },
                        { enabled: true },
                    ] as unknown as GuardrailConfigResponse['providers'],
                }),
            ),
        ).toBe(2);
    });
});

describe('getActionConfig', () => {
    it('returns the matching action entry', () => {
        expect(getActionConfig('block').label).toBe('Block');
        expect(getActionConfig('redact').bgColor).toContain('amber');
    });

    it('falls back to the harmless "allow" entry for an unknown action', () => {
        // Falling back to `block` would silently invent an enforcement the
        // config never asked for.
        expect(getActionConfig('quarantine' as GuardrailAction).value).toBe('allow');
    });

    it('offers each action exactly once', () => {
        expect(new Set(GUARDRAIL_ACTIONS.map((a) => a.value)).size).toBe(GUARDRAIL_ACTIONS.length);
    });
});

describe('the check surfaces', () => {
    // The contract names the surfaces a check's list may hold in the
    // description of `pii_surfaces`: the gateway's names, and no other.
    const generated = readFileSync(apiContract('community').types, 'utf8');
    const named = /Surfaces on which PII detection fires\. Subset of ([a-z_, 0-9]+)\./.exec(generated)?.[1].split(', ') ?? [];

    it('are exactly the surfaces the Control Plane accepts in a check list, in its order', () => {
        expect(named.length).toBeGreaterThan(10);
        expect(ALL_CHECK_SURFACES).toEqual(named);
    });

    it('do not include MCP registration, which is a switch of the configuration and no check runs on', () => {
        expect(ALL_CHECK_SURFACES).not.toContain('mcp_registration');
    });
});

describe('SURFACE_METADATA', () => {
    it('describes every check surface, and MCP registration, exactly once', () => {
        expect(SURFACE_METADATA.map((m) => m.surface).sort()).toEqual([...ALL_CHECK_SURFACES, 'mcp_registration'].sort());
    });

    it('locks moderation input — a safety classifier scanning itself would recurse', () => {
        expect(SURFACE_METADATA.find((m) => m.surface === 'moderation_input')?.locked).toBe(true);
        expect(SURFACE_METADATA.filter((m) => m.locked)).toHaveLength(1);
    });

    it('gives every surface a label and a description', () => {
        const incomplete = SURFACE_METADATA.filter((m) => !m.label || !m.description).map(
            (m) => m.surface,
        );
        expect(incomplete).toEqual([]);
    });

    it('keeps the LLM mode lists inside the full surface set', () => {
        const stray = [...LLM_INPUT_MODE_SURFACES, ...LLM_TEXT_OUTPUT_MODE_SURFACES].filter(
            (s) => !ALL_CHECK_SURFACES.includes(s),
        );
        expect(stray).toEqual([]);
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Per-surface override editor
 *
 * Pins the contract the admin UI carries on top of the JSONB
 * `*_per_surface` columns:
 *
 *   1. Existing per_surface entries preload as visible cards on the
 *      relevant check tab (no silent drop on edit).
 *   2. The submit payload echoes the per_surface map verbatim so a
 *      round-trip edit doesn't clobber what the operator stored.
 *   3. "Add surface override" seeds the new entry from the scalar
 *      values (so the dropdown isn't a no-op staring at empty fields)
 *      and "Remove" collapses the map to null on the last entry —
 *      matching the backend's NULL-not-empty-dict normalization.
 *
 * The Checks tab is split into per-check sub-tabs. The
 * tests below open the relevant sub-tab via its label before
 * interacting with the per-surface editor.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import GuardrailForm from '../components/guardrails/GuardrailForm';

const mockCreate = vi.fn().mockResolvedValue({ config: { id: 'gr-new' } });
const mockUpdate = vi.fn().mockResolvedValue({ config: { id: 'gr-existing' } });
const mockFetchGroups = vi.fn().mockResolvedValue([]);

vi.mock('../hooks/useGateway.ts', () => ({
    useGateway: () => ({
        createGuardrailConfig: mockCreate,
        updateGuardrailConfigById: mockUpdate,
        fetchGroupHierarchy: mockFetchGroups,
    }),
}));

vi.mock('../components/common/Toast', () => ({
    useToast: () => ({
        success: vi.fn(),
        error: vi.fn(),
        loading: vi.fn(),
        dismiss: vi.fn(),
    }),
}));


function buildExistingConfig(overrides: Partial<Record<string, unknown>> = {}) {
    return {
        id: 'gr-existing',
        tenant_id: 't1',
        name: 'PII per-surface fixture',
        description: '',
        is_default: false,
        priority: 0,
        scope_mcp_server_id: null,
        scope_tool_name: null,
        group_ids: [],
        groups: [],
        enabled: true,
        surfaces: {
            chat_input: true, chat_output: true,
            mcp_input: true, mcp_output: true,
            skill_input: true, skill_output: true,
            a2a_inbound: true, a2a_outbound: true,
        },
        builtin: {
            pii: {
                enabled: true, action: 'redact', provider: 'built_in', categories: null,
                surfaces: ['chat_input', 'chat_output', 'mcp_input', 'mcp_output'],
                per_surface: { chat_output: { action: 'block', provider: 'presidio' } },
            },
            prompt_injection: {
                enabled: true, action: 'block', sensitivity: 0.7, provider: 'built_in',
                surfaces: ['chat_input', 'mcp_input'],
            },
            jailbreak: {
                enabled: true, action: 'block', provider: 'built_in',
                surfaces: ['chat_input', 'mcp_input'],
            },
            toxic_content: {
                enabled: false, action: 'warn', threshold: 0.8, provider: 'built_in',
                category_thresholds: null,
                surfaces: ['chat_input', 'chat_output'],
            },
            banned_words: [],
            banned_patterns: [],
            banned_words_match_mode: null,
            banned_words_surfaces: ['chat_input', 'chat_output', 'mcp_input', 'mcp_output'],
            banned_patterns_surfaces: ['chat_input', 'chat_output', 'mcp_input', 'mcp_output'],
        },
        providers: [],
        max_check_timeout_ms: 2000,
        fail_open: true,
        fail_open_overrides: null,
        log_all_checks: false,
        ...overrides,
    } as unknown;
}

function renderForm(config: unknown = null) {
    return render(
        <GuardrailForm
            config={config as never}
            providers={[]}
            onClose={vi.fn()}
            onSave={vi.fn().mockResolvedValue(undefined)}
        />,
    );
}

function openChecksTab() {
    // Left-nav has "Checks" as one of six top-level tabs (TabId='builtin').
    fireEvent.click(screen.getByText('Checks'));
}

function openPiiSubTab() {
    // Sub-tab strip labels checks "PII Detection", "Prompt Injection", etc.
    openChecksTab();
    fireEvent.click(screen.getByRole('button', { name: 'PII Detection' }));
}

function fillName(value: string) {
    fireEvent.change(screen.getByPlaceholderText(/Strict External Partners/i), {
        target: { value },
    });
}

function clickSubmit() {
    const btn = screen.getByText(/Create Configuration|Save Configuration/);
    fireEvent.click(btn);
}


describe('GuardrailForm — per-surface override editor', () => {
    beforeEach(() => {
        mockCreate.mockClear();
        mockUpdate.mockClear();
    });

    it('preloads an existing per_surface entry as a visible card', () => {
        renderForm(buildExistingConfig());
        openPiiSubTab();
        // The override card surfaces a Remove button — uniquely
        // present per active override, so its count is a clean
        // existence assertion (no override → no Remove). The fixture
        // seeds exactly one PII override (chat_output → block +
        // presidio), so one Remove button is expected on the PII tab.
        const removeButtons = screen.getAllByText('Remove');
        expect(removeButtons.length).toBe(1);
    });

    it('round-trips per_surface unchanged through submit', async () => {
        renderForm(buildExistingConfig());
        openPiiSubTab();
        // No edits — just submit and confirm the payload preserves
        // the seeded override map.
        clickSubmit();
        await vi.waitFor(() => expect(mockUpdate).toHaveBeenCalled());
        const [, payload] = mockUpdate.mock.calls[0];
        expect(payload.builtin.pii_per_surface).toEqual({
            chat_output: { action: 'block', provider: 'presidio' },
        });
    });

    it('sends null on submit when no overrides are configured', async () => {
        renderForm();
        fillName('No overrides config');
        clickSubmit();
        await vi.waitFor(() => expect(mockCreate).toHaveBeenCalled());
        const payload = mockCreate.mock.calls[0][0];
        // Backend treats null = clear (PATCH-style). The form starts
        // with no overrides, so every per_surface field must serialize
        // as null — NOT omitted, NOT empty dict.
        expect(payload.builtin.pii_per_surface).toBeNull();
        expect(payload.builtin.prompt_injection_per_surface).toBeNull();
        expect(payload.builtin.jailbreak_per_surface).toBeNull();
        expect(payload.builtin.toxic_content_per_surface).toBeNull();
        expect(payload.builtin.secrets_per_surface).toBeNull();
        expect(payload.builtin.banned_words_per_surface).toBeNull();
        expect(payload.builtin.banned_patterns_per_surface).toBeNull();
    });

    it('hides the Add dropdown when every active surface already has an override', () => {
        // PII override pre-populated for ALL active surfaces — the
        // dropdown should be hidden because there's nothing left to add.
        const cfg = buildExistingConfig({
            builtin: {
                pii: {
                    enabled: true, action: 'redact', provider: 'built_in', categories: null,
                    surfaces: ['chat_output'],
                    per_surface: { chat_output: { action: 'block' } },
                },
                prompt_injection: { enabled: false, action: 'block', sensitivity: 0.7, provider: 'built_in', surfaces: [] },
                jailbreak: { enabled: false, action: 'block', provider: 'built_in', surfaces: [] },
                toxic_content: { enabled: false, action: 'warn', threshold: 0.8, provider: 'built_in', category_thresholds: null, surfaces: [] },
                banned_words: [], banned_patterns: [], banned_words_match_mode: null,
                banned_words_surfaces: [], banned_patterns_surfaces: [],
            },
        });
        renderForm(cfg);
        openPiiSubTab();
        // The Add control is the placeholder option "+ Add surface
        // override" — absent from the DOM entirely when the addable
        // list is empty (parent select hidden).
        expect(screen.queryByText(/\+ Add surface override/i)).toBeNull();
    });
});

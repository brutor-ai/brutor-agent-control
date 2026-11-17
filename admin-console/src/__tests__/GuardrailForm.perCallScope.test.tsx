/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Guardrail per-tool scope picker
 *
 * Covers the per-call scope of GuardrailForm:
 *
 *   1. Two optional inputs on the Access Control tab — one for
 *      MCP server ID, one for tool name. Both empty = config
 *      applies to all calls (the default).
 *
 *   2. Submit payload includes `scope_mcp_server_id` + `scope_tool_name`,
 *      with empty-string values normalised to `null`. The backend
 *      stores NULL for "no narrowing"; the literal string "" would
 *      be a bug because the Rust filter compares names exactly.
 *
 *   3. Preload: opening an existing config with scope set surfaces
 *      the values in the form.
 *
 * The scope inputs live on the Access Control tab (one of six tabs
 * in GuardrailForm). Tests click into that tab before interacting.
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
        updateGuardrailConfig: mockUpdate,
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
        name: 'PII on customer endpoint',
        description: '',
        is_default: false,
        priority: 0,
        scope_mcp_server_id: null,
        scope_tool_name: null,
        group_ids: [],
        groups: [],
        enabled: true,
        surfaces: {
            chat_input: true, chat_output: false,
            mcp_input: true, mcp_output: false,
            skill_input: true, skill_output: true,
            a2a_inbound: false, a2a_outbound: false,
        },
        builtin: {
            pii: { enabled: true, action: 'redact', provider: 'built_in', categories: null },
            prompt_injection: { enabled: true, action: 'block', sensitivity: 0.7, provider: 'built_in' },
            jailbreak: { enabled: true, action: 'block', provider: 'built_in' },
            toxic_content: { enabled: false, action: 'warn', threshold: 0.8, provider: 'openai_moderation', category_thresholds: null },
            banned_words: [],
            banned_patterns: [],
            banned_words_match_mode: null,
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

/** Switch to the Access Control tab where the scope picker lives. */
function openAccessTab() {
    fireEvent.click(screen.getByText('Access Control'));
}

/** Fill the minimum-required Name field. */
function fillName(value: string) {
    fireEvent.change(screen.getByPlaceholderText(/Strict External Partners/i), {
        target: { value },
    });
}

function clickSubmit() {
    // Submit button text is "Create Configuration" (create mode) or
    // "Save Configuration" (edit mode). Match either.
    const btn = screen.getByText(/Create Configuration|Save Configuration/);
    fireEvent.click(btn);
}


describe('GuardrailForm — per-tool scope picker', () => {
    beforeEach(() => {
        mockCreate.mockClear();
        mockUpdate.mockClear();
    });

    it('renders the new scope inputs on the Access Control tab', () => {
        renderForm();
        openAccessTab();
        // The "Per-tool scope (optional)" section heading is the
        // load-bearing tell — it's the panel that holds the per-call scope
        // inputs.
        expect(screen.getByText(/Per-tool scope/i)).toBeInTheDocument();
        expect(
            screen.getByPlaceholderText(/leave blank = all servers/i),
        ).toBeInTheDocument();
        expect(
            screen.getByPlaceholderText(/leave blank = all tools/i),
        ).toBeInTheDocument();
    });

    it('preloads existing scope values when editing', () => {
        renderForm(buildExistingConfig({
            scope_mcp_server_id: 'mcp-srv-customers',
            scope_tool_name: 'get_customer_record',
        }));
        openAccessTab();

        const serverInput = screen.getByPlaceholderText<HTMLInputElement>(/leave blank = all servers/i);
        expect(serverInput.value).toBe('mcp-srv-customers');
        const toolInput = screen.getByPlaceholderText<HTMLInputElement>(/leave blank = all tools/i);
        expect(toolInput.value).toBe('get_customer_record');
    });
});


describe('GuardrailForm — submit payload contract', () => {
    beforeEach(() => {
        mockCreate.mockClear();
        mockUpdate.mockClear();
    });

    it('sends null when both scope fields are left empty', async () => {
        renderForm();
        fillName('Test Config');
        clickSubmit();

        await vi.waitFor(() => expect(mockCreate).toHaveBeenCalled());
        const payload = mockCreate.mock.calls[0][0];
        // Empty inputs MUST normalise to null, not "" — the Rust
        // filter uses Option<&str> and "" would never match a real
        // server / tool name (so the config would silently never fire).
        expect(payload.scope_mcp_server_id).toBeNull();
        expect(payload.scope_tool_name).toBeNull();
    });

    it('sends the typed values when scope inputs are filled', async () => {
        renderForm();
        fillName('Customer PII Block');

        openAccessTab();
        const serverInput = screen.getByPlaceholderText(/leave blank = all servers/i);
        fireEvent.change(serverInput, { target: { value: 'mcp-srv-1' } });
        const toolInput = screen.getByPlaceholderText(/leave blank = all tools/i);
        fireEvent.change(toolInput, { target: { value: 'get_customer_record' } });

        clickSubmit();
        await vi.waitFor(() => expect(mockCreate).toHaveBeenCalled());

        const payload = mockCreate.mock.calls[0][0];
        expect(payload.scope_mcp_server_id).toBe('mcp-srv-1');
        expect(payload.scope_tool_name).toBe('get_customer_record');
    });

    it('trims whitespace from scope inputs and treats "   " as null', async () => {
        // Defensive against operators pasting trailing whitespace
        // ("mcp-srv-1\n") — the Rust filter does an exact compare.
        renderForm();
        fillName('Test Config');

        openAccessTab();
        const serverInput = screen.getByPlaceholderText(/leave blank = all servers/i);
        fireEvent.change(serverInput, { target: { value: '   ' } });
        const toolInput = screen.getByPlaceholderText(/leave blank = all tools/i);
        fireEvent.change(toolInput, { target: { value: '  get_customer  ' } });

        clickSubmit();
        await vi.waitFor(() => expect(mockCreate).toHaveBeenCalled());

        const payload = mockCreate.mock.calls[0][0];
        expect(payload.scope_mcp_server_id).toBeNull();  // whitespace-only → null
        expect(payload.scope_tool_name).toBe('get_customer');  // trimmed
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * LLM Catalog (system admin) — LLMCatalogAdminTab test.
 *
 * The catalog create/edit form is the shared `LLModelForm` rendered with
 * `variant="catalog"`.
 *
 * Scope:
 *   • Rows render (display name + provider + model name visible)
 *   • Empty state when the list is empty
 *   • "Add entry" → catalog form → createLLMCatalogEntry + refresh
 *     (and the field mapping display_name↔name etc.)
 *   • Edit → updateLLMCatalogEntry + refresh, provider/model immutable
 *   • Catalog form hides instance-only tabs (Access / API / Routing)
 *   • Delete → deleteLLMCatalogEntry + refresh
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import LLMCatalogAdminTab from '../components/llm/catalog/LLMCatalogAdminTab';
import type { LLMCatalogEntry } from '../lib/llmTypes';

// react-hot-toast is used directly by LLModelForm.
vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

// Singleton toast — stable reference across renders.
const _stableToast = {
    showToast: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
};
vi.mock('../components/common/Toast', () => ({
    useToast: () => _stableToast,
}));

// Confirm always resolves true (delete path).
vi.mock('../components/common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn().mockResolvedValue(true),
}));

// ProviderLogo pulls useLlmProviders/gateway — stub it out.
vi.mock('../components/llm/config/providerLogos', () => ({
    ProviderLogo: () => null,
}));

// LLModelForm reads the provider list via useLlmProviders.
vi.mock('../hooks/useLlmProviders', () => ({
    useLlmProviders: () => ({
        providers: [
            { id: 'p-openai', key: 'openai', display_name: 'OpenAI', category: 'chat', enabled: true },
            { id: 'p-anthropic', key: 'anthropic', display_name: 'Anthropic', category: 'chat', enabled: true },
        ],
        loading: false,
    }),
}));

const mockList = vi.fn();
const mockCreate = vi.fn();
const mockUpdate = vi.fn();
const mockDelete = vi.fn();

const _stableGatewayClient = {
    listLLMCatalog: mockList,
    createLLMCatalogEntry: mockCreate,
    updateLLMCatalogEntry: mockUpdate,
    deleteLLMCatalogEntry: mockDelete,
    // LLModelForm fetches groups / routing groups, but only on the instance
    // path — the catalog variant guards those out. Provide stubs anyway so a
    // stray call never throws.
    fetchGroupHierarchyTree: vi.fn().mockResolvedValue({ tree: [] }),
    listRoutingGroups: vi.fn().mockResolvedValue({ routing_groups: [] }),
};
vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: _stableGatewayClient }),
}));

function buildEntry(overrides: Partial<LLMCatalogEntry> = {}): LLMCatalogEntry {
    return {
        id: 'llmcat-1',
        provider_key: 'openai',
        model_name: 'gpt-4o',
        display_name: 'GPT-4o',
        description: null,
        mode: 'chat',
        capabilities: null,
        context_window: 128000,
        max_output_tokens: 4096,
        default_cost_per_input_token: 0.0025,
        default_cost_per_output_token: 0.01,
        icon_url: null,
        recommended: true,
        deprecated: false,
        requires_api_url: false,
        source: 'custom',
        created_at: '2026-06-01T00:00:00Z',
        updated_at: '2026-06-10T00:00:00Z',
        ...overrides,
    };
}

describe('LLMCatalogAdminTab', () => {
    beforeEach(() => {
        mockList.mockReset();
        mockCreate.mockReset();
        mockUpdate.mockReset();
        mockDelete.mockReset();
        _stableToast.success.mockReset();
        _stableToast.error.mockReset();
    });

    it('renders one row per entry with display name, provider and model name', async () => {
        mockList.mockResolvedValue({
            catalog: [
                buildEntry({ id: 'a', display_name: 'GPT-4o', model_name: 'gpt-4o' }),
                buildEntry({
                    id: 'b',
                    provider_key: 'anthropic',
                    display_name: 'Claude Sonnet 4.5',
                    model_name: 'claude-sonnet-4-5',
                    recommended: false,
                }),
            ],
            total: 2,
        });
        render(<LLMCatalogAdminTab />);
        await waitFor(() => {
            expect(screen.getByText('GPT-4o')).toBeInTheDocument();
            expect(screen.getByText('Claude Sonnet 4.5')).toBeInTheDocument();
        });
        expect(screen.getByText('gpt-4o')).toBeInTheDocument();
        expect(screen.getByText('claude-sonnet-4-5')).toBeInTheDocument();
    });

    it('shows the empty state when the list is empty', async () => {
        mockList.mockResolvedValue({ catalog: [], total: 0 });
        render(<LLMCatalogAdminTab />);
        await waitFor(() => {
            expect(screen.getByText(/No catalog entries/i)).toBeInTheDocument();
        });
    });

    it('opens the catalog form and hides instance-only tabs (Access / API / Routing)', async () => {
        mockList.mockResolvedValue({ catalog: [], total: 0 });
        render(<LLMCatalogAdminTab />);
        await waitFor(() => screen.getByText(/No catalog entries/i));

        fireEvent.click(screen.getByRole('button', { name: /Add entry/i }));

        // Catalog title.
        await waitFor(() =>
            expect(screen.getByText('Add catalog entry')).toBeInTheDocument(),
        );

        // Shown tabs.
        expect(screen.getByRole('button', { name: /^General$/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /^Cost$/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /^Retirement$/i })).toBeInTheDocument();

        // Hidden instance-only tabs.
        expect(screen.queryByRole('button', { name: /Access Control/i })).toBeNull();
        expect(screen.queryByRole('button', { name: /^API$/i })).toBeNull();
        expect(screen.queryByRole('button', { name: /^Routing$/i })).toBeNull();
    });

    it('creates an entry via the catalog form and refreshes (display_name↔name mapping)', async () => {
        mockList.mockResolvedValue({ catalog: [], total: 0 });
        mockCreate.mockResolvedValue(buildEntry());
        render(<LLMCatalogAdminTab />);
        await waitFor(() => screen.getByText(/No catalog entries/i));
        expect(mockList).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole('button', { name: /Add entry/i }));
        await waitFor(() =>
            expect(screen.getByText('Add catalog entry')).toBeInTheDocument(),
        );

        // Name → display_name. Provider defaults to the first option (openai).
        fireEvent.change(
            screen.getByPlaceholderText('e.g., Production GPT-4o'),
            { target: { value: 'GPT-5' } },
        );
        // Model name → model_name.
        fireEvent.change(
            screen.getByPlaceholderText('e.g., gpt-5.2'),
            { target: { value: 'gpt-5' } },
        );

        fireEvent.click(screen.getByRole('button', { name: /Save Model/i }));

        await waitFor(() => {
            expect(mockCreate).toHaveBeenCalledTimes(1);
        });
        expect(mockCreate).toHaveBeenCalledWith(
            expect.objectContaining({
                provider_key: 'openai',
                model_name: 'gpt-5',
                display_name: 'GPT-5',
                mode: 'chat',
            }),
        );
        // Create triggers a refresh (second list call).
        await waitFor(() => {
            expect(mockList).toHaveBeenCalledTimes(2);
        });
        expect(_stableToast.success).toHaveBeenCalled();
    });

    it('edits an entry — provider/model immutable, update body maps fields', async () => {
        mockList.mockResolvedValue({
            catalog: [buildEntry({ id: 'edit-me', display_name: 'GPT-4o' })],
            total: 1,
        });
        mockUpdate.mockResolvedValue(buildEntry({ id: 'edit-me' }));
        render(<LLMCatalogAdminTab />);
        await waitFor(() => screen.getByText('GPT-4o'));

        fireEvent.click(screen.getByRole('button', { name: /Edit GPT-4o/i }));

        await waitFor(() =>
            expect(
                screen.getByText(/Edit catalog entry — GPT-4o/i),
            ).toBeInTheDocument(),
        );

        // Change the display name and save.
        const nameInput = screen.getByPlaceholderText<HTMLInputElement>(
            'e.g., Production GPT-4o',
        );
        expect(nameInput.value).toBe('GPT-4o');
        fireEvent.change(nameInput, { target: { value: 'GPT-4o Renamed' } });

        fireEvent.click(screen.getByRole('button', { name: /Save Model/i }));

        await waitFor(() => expect(mockUpdate).toHaveBeenCalledTimes(1));
        const [id, body] = mockUpdate.mock.calls[0];
        expect(id).toBe('edit-me');
        expect(body.display_name).toBe('GPT-4o Renamed');
        // provider_key / model_name are immutable on update — not in the body.
        expect(body).not.toHaveProperty('provider_key');
        expect(body).not.toHaveProperty('model_name');
        // Cost + context-window mapping round-trips from the synthesized model.
        expect(body.context_window).toBe(128000);
        expect(body.max_output_tokens).toBe(4096);
        expect(body.default_cost_per_input_token).toBe(0.0025);
        expect(body.default_cost_per_output_token).toBe(0.01);

        await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
        expect(_stableToast.success).toHaveBeenCalled();
    });

    it('deletes an entry and refreshes', async () => {
        mockList.mockResolvedValue({
            catalog: [buildEntry({ id: 'del-me', display_name: 'Doomed' })],
            total: 1,
        });
        mockDelete.mockResolvedValue(undefined);
        render(<LLMCatalogAdminTab />);
        await waitFor(() => screen.getByText('Doomed'));
        expect(mockList).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole('button', { name: /Delete Doomed/i }));

        await waitFor(() => {
            expect(mockDelete).toHaveBeenCalledWith('del-me');
        });
        await waitFor(() => {
            expect(mockList).toHaveBeenCalledTimes(2);
        });
        expect(_stableToast.success).toHaveBeenCalled();
    });
});

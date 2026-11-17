/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLModelForm — the "Model" field's suggestions come from the GLOBAL model
 * catalog (`/v1/admin/llm-catalog?provider_key=…`), not from a list compiled
 * into the bundle.
 *
 * A static table in the bundle is independent of the database, so it would
 * keep offering ids the providers have retired long after the catalog has
 * dropped them. The catalog is the one place model ids are maintained; the
 * form reads it for the chosen provider.
 *
 * Coverage:
 *   1. The datalist holds exactly the catalog's `model_name`s for the chosen
 *      provider, and the catalog is asked for that provider.
 *   2. Changing the provider re-queries and replaces the suggestions.
 *   3. Nothing compiled in: an empty catalog means no suggestions at all.
 *   4. Catalog mode (writing a catalog entry) asks for nothing: an id the
 *      catalog already has is the one that cannot be added.
 *   5. A failed catalog read is non-fatal and the field stays free text.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import LLModelForm from '../components/llm/config/LLModelForm';

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('../hooks/useLlmProviders', () => ({
    useLlmProviders: () => ({ providers: [], loading: false }),
}));

const mockListProviderCatalog = vi.fn();
const mockListLLMCatalog = vi.fn();

const _stableGatewayClient = {
    listProviderCatalog: mockListProviderCatalog,
    listLLMCatalog: mockListLLMCatalog,
    getProviderIconUrl: () => 'https://x/icon',
    uploadProviderIcon: vi.fn(),
};

vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: _stableGatewayClient, groupTree: [] }),
}));

const PROVIDERS = [
    { id: 'provcat-openai', key: 'openai', display_name: 'OpenAI', category: 'chat', icon_url: null },
    { id: 'provcat-groq', key: 'groq', display_name: 'Groq', category: 'chat', icon_url: null },
];

// Invented ids on purpose: the test must not depend on which models any
// provider happens to serve this month.
const MODELS: Record<string, string[]> = {
    openai: ['catalog-chat-a', 'catalog-chat-b', 'catalog-embed-a'],
    groq: ['vendor/catalog-chat-c'],
};

function catalogFor(providerKey: string | undefined) {
    const names = MODELS[providerKey ?? ''] ?? [];
    return {
        catalog: names.map((model_name, i) => ({
            id: `llmcat-${providerKey}-${i}`,
            provider_key: providerKey,
            model_name,
            display_name: model_name,
            mode: 'chat',
            recommended: false,
            deprecated: false,
            source: 'seed',
        })),
        total: names.length,
    };
}

function suggestions(): string[] {
    return Array.from(
        document.querySelectorAll<HTMLOptionElement>('datalist#model-suggestions option'),
    ).map((o) => o.value);
}

function modelInput(): HTMLInputElement {
    const el = document.getElementById('model_name');
    if (!(el instanceof HTMLInputElement)) throw new Error('model_name input not rendered');
    return el;
}

function renderForm(variant?: 'instance' | 'catalog') {
    render(
        <LLModelForm
            model={null}
            variant={variant}
            onSave={vi.fn(() => Promise.resolve())}
            onClose={vi.fn()}
        />,
    );
}

describe('LLModelForm — model suggestions come from the catalog', () => {
    beforeEach(() => {
        mockListProviderCatalog.mockReset();
        mockListProviderCatalog.mockResolvedValue({ providers: PROVIDERS, total: PROVIDERS.length });
        mockListLLMCatalog.mockReset();
        mockListLLMCatalog.mockImplementation((params?: { provider_key?: string }) =>
            Promise.resolve(catalogFor(params?.provider_key)),
        );
    });

    it('suggests the catalog entries of the chosen provider', async () => {
        renderForm();

        await waitFor(() => expect(suggestions()).toEqual(MODELS.openai));
        expect(mockListLLMCatalog).toHaveBeenCalledWith({ provider_key: 'openai', limit: 500 });
        // The input is wired to that datalist.
        expect(modelInput()).toHaveAttribute('list', 'model-suggestions');
    });

    it('replaces the suggestions when the provider changes', async () => {
        renderForm();
        await waitFor(() => expect(suggestions()).toEqual(MODELS.openai));
        await waitFor(() => expect(screen.getByRole('option', { name: 'Groq' })).toBeInTheDocument());

        fireEvent.change(screen.getByRole('option', { name: 'Groq' }).closest('select')!, {
            target: { value: 'groq' },
        });

        await waitFor(() => expect(suggestions()).toEqual(MODELS.groq));
        // Not the last call: the form also asks the catalog, per provider and
        // mode, whether such a model needs its own API URL.
        expect(mockListLLMCatalog).toHaveBeenCalledWith({ provider_key: 'groq', limit: 500 });
    });

    it('compiles no ids into the form: an empty catalog suggests nothing', async () => {
        mockListLLMCatalog.mockResolvedValue({ catalog: [], total: 0 });
        renderForm();

        await waitFor(() => expect(mockListLLMCatalog).toHaveBeenCalled());
        await waitFor(() => expect(mockListProviderCatalog).toHaveBeenCalled());
        expect(suggestions()).toEqual([]);
    });

    it('does not suggest existing catalog ids when writing a catalog entry', async () => {
        renderForm('catalog');

        // The provider list has loaded, so the form's effects have run.
        await waitFor(() => expect(screen.getByRole('option', { name: 'Groq' })).toBeInTheDocument());
        expect(mockListLLMCatalog).not.toHaveBeenCalled();
        expect(suggestions()).toEqual([]);
    });

    it('keeps the field free text when the catalog cannot be read', async () => {
        mockListLLMCatalog.mockRejectedValue(new Error('catalog 500'));
        renderForm();

        await waitFor(() => expect(mockListLLMCatalog).toHaveBeenCalled());
        expect(suggestions()).toEqual([]);

        const input = modelInput();
        fireEvent.change(input, { target: { value: 'my-own/fine-tune-7' } });
        expect(input).toHaveValue('my-own/fine-tune-7');
    });
});

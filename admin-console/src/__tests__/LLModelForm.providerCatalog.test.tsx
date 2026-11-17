/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLModelForm — provider dropdown sources the GLOBAL provider catalog.
 *
 * The model form's provider <select> options must come from
 * `gatewayClient.listProviderCatalog()` (the full browse list) — NOT the
 * per-tenant *configured* `useLlmProviders()` list — so any provider is
 * pickable when manually adding a model. The backend auto-imports the
 * chosen provider into `llm_providers` on save.
 *
 * Coverage:
 *   1. ADD flow: every catalog provider appears as an option (incl. ones
 *      NOT in the configured-providers list), grouped by category→tier.
 *   2. ADD flow: the icon preview renders the slug brand mark (no upload
 *      control — there's no configured provider id to upload against).
 *   3. EDIT flow with a configured provider: the upload control shows.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import LLModelForm from '../components/llm/config/LLModelForm';
import { buildLLModelConfig } from '../test/llm/llmModelConfig';

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

// Configured providers (per-tenant). Deliberately a SMALL subset so we can
// prove the dropdown isn't sourced from here.
vi.mock('../hooks/useLlmProviders', () => ({
    useLlmProviders: () => ({
        providers: [
            {
                id: 'p-openai',
                key: 'openai',
                display_name: 'OpenAI',
                category: 'chat',
                has_icon: true,
                enabled: true,
            },
        ],
        loading: false,
    }),
}));

const mockListProviderCatalog = vi.fn();

const _stableGatewayClient = {
    listProviderCatalog: mockListProviderCatalog,
    getProviderIconUrl: () => 'https://x/icon',
    uploadProviderIcon: vi.fn(),
};

vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: _stableGatewayClient, groupTree: [] }),
}));

const CATALOG = [
    { id: 'provcat-openai', key: 'openai', display_name: 'OpenAI', category: 'chat', icon_url: null },
    { id: 'provcat-anthropic', key: 'anthropic', display_name: 'Anthropic', category: 'chat', icon_url: null },
    // NOT in the configured list — proves we source the catalog.
    { id: 'provcat-voyage', key: 'voyage', display_name: 'Voyage AI', category: 'embeddings', icon_url: null },
    { id: 'provcat-ollama', key: 'ollama', display_name: 'Ollama', category: 'self_hosted', icon_url: null },
];

const baseModel = buildLLModelConfig({ id: 'llm-existing', name: 'Existing Model' });

function renderForm(model: Parameters<typeof LLModelForm>[0]['model'] | null = null) {
    render(
        <LLModelForm model={model} onSave={vi.fn(async () => undefined)} onClose={vi.fn()} />,
    );
}

describe('LLModelForm — provider dropdown sources the catalog', () => {
    beforeEach(() => {
        mockListProviderCatalog.mockReset();
        mockListProviderCatalog.mockResolvedValue({ providers: CATALOG, total: CATALOG.length });
    });

    it('lists every catalog provider as an option (ADD flow)', async () => {
        renderForm(null);

        await waitFor(() => expect(mockListProviderCatalog).toHaveBeenCalled());

        // Catalog providers appear as options — including ones NOT in the
        // configured-providers list (voyage / ollama / anthropic).
        await waitFor(() => {
            expect(
                screen.getByRole('option', { name: 'Voyage AI' }),
            ).toBeInTheDocument();
        });
        expect(screen.getByRole('option', { name: 'Anthropic' })).toBeInTheDocument();
        expect(screen.getByRole('option', { name: 'Ollama' })).toBeInTheDocument();
        expect(screen.getByRole('option', { name: 'OpenAI' })).toBeInTheDocument();

        // No "(disabled)" suffix logic — catalog has no per-tenant enabled state.
        expect(screen.queryByText(/\(disabled\)/)).toBeNull();
    });

    it('shows the slug brand mark (not the upload control) in the ADD flow', async () => {
        renderForm(null);
        await waitFor(() => expect(mockListProviderCatalog).toHaveBeenCalled());

        // The ADD flow renders ProviderLogo + an explanatory note; there is
        // no "Upload icon" / "Replace icon" button.
        await waitFor(() => {
            expect(
                screen.getByText(/imported automatically when the model is saved/i),
            ).toBeInTheDocument();
        });
        expect(screen.queryByText(/Upload icon/i)).toBeNull();
        expect(screen.queryByText(/Replace icon/i)).toBeNull();
    });

    it('shows the upload control in the EDIT flow for a configured provider', async () => {
        renderForm(baseModel);
        await waitFor(() => expect(mockListProviderCatalog).toHaveBeenCalled());

        // openai is in the configured-providers list (has_icon=true) → upload
        // control with "Replace icon".
        await waitFor(() => {
            expect(screen.getByText(/Replace icon/i)).toBeInTheDocument();
        });
        // The ADD-flow auto-import note must NOT appear here.
        expect(
            screen.queryByText(/imported automatically when the model is saved/i),
        ).toBeNull();
    });
});

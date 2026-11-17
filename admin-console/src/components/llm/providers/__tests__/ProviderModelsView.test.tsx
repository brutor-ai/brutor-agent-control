/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for ProviderModelsView's per-row actions menu.
 *
 * What this file guards against: a menu rendered `position: absolute`
 * inside the table. The table wrapper carries `overflow-hidden` so its
 * rows clip to the container's rounded corners — which also clips such
 * a menu. On a provider with many models the container happens to be
 * tall enough to contain the menu, so the clip is invisible; on a
 * provider with one or two models the menu has nowhere to render and
 * never appears.
 *
 * What is worth defending:
 *
 *   - the menu escapes the clipping container entirely (portalled to
 *     document.body, positioned fixed) — asserted structurally, not by
 *     reading styles, because that is the actual failure mode;
 *   - it opens for a single-model provider, which is the case that broke;
 *   - it still carries every action, so portalling did not lose entries.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const listLLMs = vi.fn();
const updateLLM = vi.fn();
const copyLLM = vi.fn();
const deleteLLM = vi.fn();

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({
        gatewayClient: { listLLMs, updateLLM, copyLLM, deleteLLM },
    }),
}));

vi.mock('react-hot-toast', () => ({
    default: { success: vi.fn(), error: vi.fn() },
}));

import ProviderModelsView from '../ProviderModelsView';

const PROVIDER = {
    id: 'prov-hf',
    key: 'huggingface',
    display_name: 'HuggingFace Inference Endpoints',
    enabled: true,
} as never;

/**
 * One model — the shape that leaves a clipped menu with nowhere to go.
 * `listLLMs` returns a keyed object under `models`, not an array.
 */
const ONE_MODEL = {
    models: {
        'llm-1': {
            id: 'llm-1',
            name: 'Qwen3.8-27B-abliterated (HuggingFace)',
            model_name: 'wangzhang/Qwen3.8-27B-abliterated',
            provider: 'huggingface',
            mode: 'chat',
            enabled: true,
            description: '26.9B parameters | apache-2.0',
        },
    },
};

const renderView = () =>
    render(
        <ProviderModelsView
            provider={PROVIDER}
            onBack={() => {}}
            onEditProvider={() => {}}
        />,
    );

describe('ProviderModelsView — row actions menu', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        listLLMs.mockResolvedValue(ONE_MODEL);
    });

    it('opens the actions menu for a provider with a single model', async () => {
        renderView();
        await screen.findByText('Qwen3.8-27B-abliterated (HuggingFace)');

        fireEvent.click(screen.getByTitle('Actions'));

        const menu = await screen.findByTestId('model-row-actions-menu');
        expect(menu).toBeTruthy();
    });

    it('renders the menu outside the clipping table container', async () => {
        const { container } = renderView();
        await screen.findByText('Qwen3.8-27B-abliterated (HuggingFace)');

        fireEvent.click(screen.getByTitle('Actions'));
        const menu = await screen.findByTestId('model-row-actions-menu');

        // The bug in one assertion: if the menu lives inside the component
        // subtree it sits under `overflow-hidden` and gets clipped. It must
        // be portalled out to the body instead.
        expect(container.contains(menu)).toBe(false);
        expect(document.body.contains(menu)).toBe(true);
        expect(menu.closest('table')).toBeNull();
    });

    it('keeps every action after portalling', async () => {
        renderView();
        await screen.findByText('Qwen3.8-27B-abliterated (HuggingFace)');

        fireEvent.click(screen.getByTitle('Actions'));
        const menu = await screen.findByTestId('model-row-actions-menu');

        const labels = Array.from(menu.querySelectorAll('[role="menuitem"]')).map(
            (b) => b.textContent?.trim(),
        );
        expect(labels).toEqual(['Edit', 'Disable', 'Copy', 'Delete']);
    });

    it('closes when the backdrop is clicked', async () => {
        renderView();
        await screen.findByText('Qwen3.8-27B-abliterated (HuggingFace)');

        fireEvent.click(screen.getByTitle('Actions'));
        await screen.findByTestId('model-row-actions-menu');

        // The backdrop is the portalled sibling that covers the viewport.
        const backdrop = document.querySelector('.fixed.inset-0');
        expect(backdrop).toBeTruthy();
        fireEvent.click(backdrop as Element);

        await waitFor(() =>
            expect(screen.queryByTestId('model-row-actions-menu')).toBeNull(),
        );
    });
});

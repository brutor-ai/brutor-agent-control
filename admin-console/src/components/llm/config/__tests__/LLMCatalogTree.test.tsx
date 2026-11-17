/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The global-catalogue browser: providers as collapsible groups, one "Add" per
 * entry that materialises a configured model instance.
 *
 * Two behaviours carry the weight:
 *
 *   - filtering auto-expands the matching groups. Without that the operator
 *     types a model name, every group stays collapsed, and the screen reads as
 *     "no such model" while the match sits one click away;
 *   - an entry that is already configured shows "Added" and no button. The
 *     match is on `provider_key:model_name`, not on the catalogue row id, so
 *     the same model reached through a different catalogue row is still
 *     recognised — importing it twice would create a duplicate instance.
 */

import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// The provider logo reaches for auth context to resolve hashed assets; this
// tree's behaviour has nothing to do with which glyph it draws.
vi.mock('../providerLogos', () => ({
    ProviderLogo: ({ provider }: { provider: string }) => (
        <span data-testid={`logo-${provider}`} />
    ),
}));

import LLMCatalogTree from '../LLMCatalogTree';
import type { LLMCatalogEntry } from '../../../../lib/llmTypes';

const entry = (over: Partial<LLMCatalogEntry> = {}) =>
    ({
        id: 'cat-1',
        provider_key: 'anthropic',
        model_name: 'claude-opus-5',
        display_name: 'Claude Opus 5',
        mode: 'chat',
        recommended: false,
        ...over,
    }) as LLMCatalogEntry;

const ENTRIES = [
    entry(),
    entry({
        id: 'cat-2',
        model_name: 'claude-haiku-5',
        display_name: 'Claude Haiku 5',
        recommended: true,
    }),
    entry({
        id: 'cat-3',
        provider_key: 'openai',
        model_name: 'gpt-6',
        display_name: 'GPT-6',
        mode: 'chat',
    }),
];

function setup(
    props: Partial<React.ComponentProps<typeof LLMCatalogTree>> = {}
) {
    const onImport = vi.fn().mockResolvedValue(undefined);
    render(
        <LLMCatalogTree
            entries={ENTRIES}
            configuredKeys={new Set()}
            onImport={onImport}
            {...props}
        />
    );
    return { onImport };
}

const filterBox = () =>
    screen.getByPlaceholderText('Filter the catalog by name or model id…');

describe('LLMCatalogTree — grouping', () => {
    it('groups by provider and counts each group', () => {
        setup();
        expect(screen.getByText('Anthropic')).toBeInTheDocument();
        expect(screen.getByText('OpenAI')).toBeInTheDocument();
        expect(screen.getByText('2')).toBeInTheDocument();
    });

    it('starts collapsed so a large catalogue is navigable', () => {
        setup();
        expect(screen.queryByText('Claude Opus 5')).not.toBeInTheDocument();
    });

    it('opens and closes one provider at a time', () => {
        setup();
        fireEvent.click(screen.getByText('Anthropic'));
        expect(screen.getByText('Claude Opus 5')).toBeInTheDocument();
        expect(screen.queryByText('GPT-6')).not.toBeInTheDocument();

        fireEvent.click(screen.getByText('Anthropic'));
        expect(screen.queryByText('Claude Opus 5')).not.toBeInTheDocument();
    });

    it('expands and collapses everything at once', () => {
        setup();
        fireEvent.click(screen.getByText('Expand All'));
        expect(screen.getByText('Claude Opus 5')).toBeInTheDocument();
        expect(screen.getByText('GPT-6')).toBeInTheDocument();

        fireEvent.click(screen.getByText('Collapse All'));
        expect(screen.queryByText('GPT-6')).not.toBeInTheDocument();
    });

    it('sorts entries within a group by display name', () => {
        setup();
        fireEvent.click(screen.getByText('Expand All'));
        const names = screen
            .getAllByText(/^Claude /)
            .map((n) => n.textContent);
        expect(names).toEqual(['Claude Haiku 5', 'Claude Opus 5']);
    });

    it('files an entry with no provider under a custom group', () => {
        setup({
            entries: [entry({ id: 'c', provider_key: '', display_name: 'Local Llama' })],
        });
        fireEvent.click(screen.getByText('Expand All'));
        expect(screen.getByText('Local Llama')).toBeInTheDocument();
    });
});

describe('LLMCatalogTree — filtering', () => {
    it('reveals matches without the operator having to expand anything', () => {
        // Otherwise a filtered-but-collapsed tree reads as "not in the catalog".
        setup();
        fireEvent.change(filterBox(), { target: { value: 'haiku' } });

        expect(screen.getByText('Claude Haiku 5')).toBeInTheDocument();
        expect(screen.queryByText('Claude Opus 5')).not.toBeInTheDocument();
        expect(screen.queryByText('OpenAI')).not.toBeInTheDocument();
    });

    it('matches on the model id as well as the display name', () => {
        setup();
        fireEvent.change(filterBox(), { target: { value: 'gpt-6' } });
        expect(screen.getByText('GPT-6')).toBeInTheDocument();
    });

    it('ignores case', () => {
        setup();
        fireEvent.change(filterBox(), { target: { value: 'OPUS' } });
        expect(screen.getByText('Claude Opus 5')).toBeInTheDocument();
    });

    it('says nothing matched rather than showing an empty tree', () => {
        setup();
        fireEvent.change(filterBox(), { target: { value: 'gemini' } });
        expect(screen.getByText('No catalog models match.')).toBeInTheDocument();
    });

    it('restores the tree when the filter is cleared', () => {
        setup();
        fireEvent.change(filterBox(), { target: { value: 'haiku' } });
        fireEvent.change(filterBox(), { target: { value: '' } });

        expect(screen.getByText('OpenAI')).toBeInTheDocument();
        expect(screen.queryByText('Claude Haiku 5')).not.toBeInTheDocument();
    });
});

describe('LLMCatalogTree — importing', () => {
    it('hands the whole catalogue entry to the importer', async () => {
        const { onImport } = setup();
        fireEvent.click(screen.getByText('Expand All'));
        fireEvent.click(screen.getAllByRole('button', { name: /Add/ })[0]);

        await waitFor(() =>
            expect(onImport).toHaveBeenCalledWith(
                expect.objectContaining({ model_name: 'claude-haiku-5' })
            )
        );
    });

    it('marks an already-configured model as Added and offers no button', () => {
        setup({ configuredKeys: new Set(['anthropic:claude-opus-5']) });
        fireEvent.click(screen.getByText('Expand All'));

        expect(screen.getByText('Added')).toBeInTheDocument();
        // Two entries remain importable; the third is already in.
        expect(screen.getAllByRole('button', { name: /Add$/ })).toHaveLength(2);
    });

    it('keys the Added check on provider and model, not the catalogue row id', () => {
        // The same model can appear under more than one catalogue row.
        setup({
            entries: [entry({ id: 'a-different-row' })],
            configuredKeys: new Set(['anthropic:claude-opus-5']),
        });
        fireEvent.click(screen.getByText('Expand All'));
        expect(screen.getByText('Added')).toBeInTheDocument();
    });

    it('disables only the row being imported', async () => {
        let release: () => void = () => {};
        const onImport = vi.fn(
            () => new Promise<void>((res) => { release = res; })
        );
        setup({ onImport });
        fireEvent.click(screen.getByText('Expand All'));

        const buttons = screen.getAllByRole('button', { name: /Add$/ });
        fireEvent.click(buttons[0]);

        expect(await screen.findByText('Adding…')).toBeInTheDocument();
        expect(screen.getAllByRole('button', { name: /Add$/ })).toHaveLength(2);

        release();
        await waitFor(() => expect(screen.queryByText('Adding…')).not.toBeInTheDocument());
    });

    it('re-enables the row when the import fails', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const onImport = vi.fn().mockRejectedValue(new Error('quota exceeded'));
        setup({ onImport });
        fireEvent.click(screen.getByText('Expand All'));
        fireEvent.click(screen.getAllByRole('button', { name: /Add$/ })[0]);

        await waitFor(() => expect(onImport).toHaveBeenCalled());
        // Three Add buttons again — nothing is stuck spinning.
        await waitFor(() =>
            expect(screen.getAllByRole('button', { name: /Add$/ })).toHaveLength(3)
        );
        vi.restoreAllMocks();
    });

    it('flags recommended entries and shows each entry mode', () => {
        setup();
        fireEvent.click(screen.getByText('Expand All'));
        expect(screen.getByText('Recommended')).toBeInTheDocument();
        expect(screen.getAllByText('chat')).toHaveLength(3);
    });
});

describe('LLMCatalogTree — loading', () => {
    it('shows a spinner and no tree while the catalogue is in flight', () => {
        setup({ loading: true });
        expect(screen.queryByText('Anthropic')).not.toBeInTheDocument();
        expect(
            screen.queryByPlaceholderText('Filter the catalog by name or model id…')
        ).not.toBeInTheDocument();
    });
});

describe('LLMCatalogTree — an entry that needs an API URL', () => {
    it('says so on the entry the control plane marks, and on no other', () => {
        setup({
            entries: [
                entry({ id: 'az', provider_key: 'azure', model_name: 'gpt-4o', display_name: 'Azure GPT-4o', requires_api_url: true }),
                entry({ requires_api_url: false }),
            ],
        });
        fireEvent.click(screen.getByText('Expand All'));
        const marks = screen.getAllByText('Needs API URL');
        expect(marks).toHaveLength(1);
        expect(marks[0].closest('div')?.textContent).toContain('Azure GPT-4o');
    });
});


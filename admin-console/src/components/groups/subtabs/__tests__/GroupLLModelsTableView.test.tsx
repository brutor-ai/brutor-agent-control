/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The models bound to a group.
 *
 * Two distinctions carry weight here and both are easy to lose:
 *
 *   1. DIRECT vs INHERITED. An inherited binding is owned by an ancestor, so
 *      this screen must not offer to remove it or to flip its portal
 *      visibility — the write belongs to the group that made the binding.
 *      Inherited rows still show the visibility as a read-only fact.
 *   2. `requires_tier` vs `unhealthy`. The catalogue is right and the model
 *      is fine; the configured key simply has no access to that tier. Read
 *      as "unhealthy" an operator goes hunting for an outage that isn't
 *      there, so it gets its own amber state and, where the backend names
 *      the tier, says which one.
 *
 * The Portal column is not the table's own: it is added by the caller (its
 * tests are in GroupModelPortalVisibilityCell.test.tsx).
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

vi.mock('@/components/llm/config/providerLogos', () => ({
    ProviderLogo: ({ provider }: { provider: string }) => (
        <span data-testid={`provider-logo-${provider}`} />
    ),
}));

import GroupLLModelsTableView from '../GroupLLModelsTableView';
import type { LLModelInfo } from '../../../../lib/groupTypes';

function model(overrides: Partial<LLModelInfo> = {}): LLModelInfo {
    return {
        id: 'm-1',
        name: 'Claude Opus',
        description: 'Frontier reasoning model',
        provider: 'anthropic',
        model_name: 'claude-opus-5',
        is_inherited: false,
        health_status: { status: 'healthy' },
        ...overrides,
    } as LLModelInfo;
}

const getModelProvider = vi.fn((_name: string, key?: string) => ({
    provider: key === 'anthropic' ? 'Anthropic' : 'Meta',
    color: '#000',
    bgColor: '#fff',
}));

function renderTable(
    models: LLModelInfo[],
    props: Partial<React.ComponentProps<typeof GroupLLModelsTableView>> = {},
) {
    const onRemove = vi.fn();
    const view = render(
        <GroupLLModelsTableView
            models={models}
            onRemove={onRemove}
            removingId={null}
            getModelProvider={getModelProvider}
            {...props}
        />,
    );
    return { onRemove, ...view };
}

const rowFor = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

describe('GroupLLModelsTableView — reading a row', () => {
    it('shows the display name, the binding id and the provider model identifier', () => {
        renderTable([model()]);
        const row = rowFor('Claude Opus');
        expect(within(row).getByText('m-1')).toBeInTheDocument();
        expect(within(row).getByText('claude-opus-5')).toBeInTheDocument();
        expect(within(row).getByText('Anthropic')).toBeInTheDocument();
    });

    it('a model with no description says so rather than leaving a gap', () => {
        renderTable([model({ description: undefined })]);
        expect(within(rowFor('Claude Opus')).getByText('No description')).toBeInTheDocument();
    });

    it('draws the logo from the real serving platform, not the display label', () => {
        // A Llama served by Ollama shows the Ollama glyph even though the
        // badge reads "Meta".
        renderTable([model({ id: 'm-2', name: 'Llama 4', provider: 'ollama' })]);
        expect(screen.getByTestId('provider-logo-ollama')).toBeInTheDocument();
    });

    it('separates direct bindings from inherited ones', () => {
        renderTable([model(), model({ id: 'm-2', name: 'GPT-5', is_inherited: true })]);
        expect(within(rowFor('Claude Opus')).getByText('Direct')).toBeInTheDocument();
        expect(within(rowFor('GPT-5')).getByText('Inherited')).toBeInTheDocument();
    });

    it('counts what it is showing, singular and plural', () => {
        const { rerender } = renderTable([model()]);
        expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 1 model');
        rerender(
            <GroupLLModelsTableView
                models={[model(), model({ id: 'm-2', name: 'GPT-5' })]}
                onRemove={vi.fn()}
                removingId={null}
                getModelProvider={getModelProvider}
            />,
        );
        expect(screen.getByText(/Showing/)).toHaveTextContent('Showing 2 models');
    });
});

describe('GroupLLModelsTableView — health states', () => {
    it.each([
        ['healthy', 'Healthy'],
        ['unhealthy', 'Unhealthy'],
        ['degraded', 'Degraded'],
    ])('%s reads as "%s"', (status, label) => {
        renderTable([model({ health_status: { status } as LLModelInfo['health_status'] })]);
        expect(within(rowFor('Claude Opus')).getByText(label)).toBeInTheDocument();
    });

    it('an unrecognised status reads as Unknown rather than as healthy', () => {
        renderTable([
            model({
                health_status: {
                    // @ts-expect-error A status the type does not have: what the row is given when the server names one this console does not know.
                    status: 'banana',
                    consecutive_failures: 0,
                },
            }),
        ]);
        expect(within(rowFor('Claude Opus')).getByText('Unknown')).toBeInTheDocument();
    });

    it('an eligibility gate names the tier it needs — not an outage', () => {
        renderTable([
            model({
                health_status: { status: 'requires_tier', required_tier: 'Pro' } as LLModelInfo['health_status'],
            }),
        ]);
        const row = rowFor('Claude Opus');
        expect(within(row).getByText('Requires Pro')).toBeInTheDocument();
        expect(within(row).queryByText('Unhealthy')).toBeNull();
    });

    it('falls back to a generic tier gate when the backend names no tier', () => {
        renderTable([
            model({ health_status: { status: 'requires_tier' } as LLModelInfo['health_status'] }),
        ]);
        expect(within(rowFor('Claude Opus')).getByText('Requires Tier')).toBeInTheDocument();
    });
});

describe('GroupLLModelsTableView — columns added by the caller', () => {
    const headers = () => screen.getAllByRole('columnheader').map((h) => h.textContent?.trim());

    it('with none, the columns are the table\'s own', () => {
        renderTable([model()]);
        expect(headers()).toEqual(['Provider', 'Name', 'Description', 'Model identifier', 'Inherited', 'Health', 'Actions']);
    });

    it('are placed between Health and Actions, in the order given, one cell per row', () => {
        renderTable([model(), model({ id: 'm-2', name: 'GPT-5' })], {
            extraColumns: [
                { id: 'one', header: 'One', headerTitle: 'the first', render: (m) => <span>one for {m.id}</span> },
                { id: 'two', header: 'Two', render: (m) => <span>two for {m.id}</span> },
            ],
        });
        expect(headers()).toEqual([
            'Provider', 'Name', 'Description', 'Model identifier', 'Inherited', 'Health', 'One', 'Two', 'Actions',
        ]);
        expect(screen.getByRole('columnheader', { name: 'One' })).toHaveAttribute('title', 'the first');
        expect(within(rowFor('Claude Opus')).getByText('one for m-1')).toBeInTheDocument();
        expect(within(rowFor('Claude Opus')).getByText('two for m-1')).toBeInTheDocument();
        expect(within(rowFor('GPT-5')).getByText('one for m-2')).toBeInTheDocument();
        const cells = Array.from(rowFor('GPT-5').querySelectorAll('td')).map((td) => td.textContent);
        expect(cells.slice(6, 8)).toEqual(['one for m-2', 'two for m-2']);
        expect(cells).toHaveLength(9);
    });
});

describe('GroupLLModelsTableView — actions', () => {
    it('removal is offered on a direct binding only', () => {
        renderTable([model(), model({ id: 'm-2', name: 'GPT-5', is_inherited: true })]);
        expect(within(rowFor('Claude Opus')).getByTitle('Remove model')).toBeInTheDocument();
        expect(within(rowFor('GPT-5')).queryByTitle('Remove model')).toBeNull();
    });

    it('removal reports the model id and name so the caller can confirm', () => {
        const { onRemove } = renderTable([model()]);
        fireEvent.click(within(rowFor('Claude Opus')).getByTitle('Remove model'));
        expect(onRemove).toHaveBeenCalledWith('m-1', 'Claude Opus');
    });

    it('a removal in flight disables that row’s button only', () => {
        renderTable([model(), model({ id: 'm-2', name: 'GPT-5' })], { removingId: 'm-1' });
        expect(within(rowFor('Claude Opus')).getByTitle('Remove model')).toBeDisabled();
        expect(within(rowFor('GPT-5')).getByTitle('Remove model')).toBeEnabled();
    });

    it('says whether governance and limits are already in force on the row', () => {
        renderTable([model({ has_governance: true, has_limits: false })]);
        const row = rowFor('Claude Opus');
        expect(within(row).getByTitle('Governance active — click to configure')).toBeInTheDocument();
        expect(within(row).getByTitle('Configure per-model limits')).toBeInTheDocument();
    });

    it('hands the whole model to the governance and limits dialogs', () => {
        const onConfigureGovernance = vi.fn();
        const onConfigureLimits = vi.fn();
        const m = model();
        renderTable([m], { onConfigureGovernance, onConfigureLimits });
        const row = rowFor('Claude Opus');

        fireEvent.click(within(row).getByTitle('Configure governance'));
        expect(onConfigureGovernance).toHaveBeenCalledWith(m);

        fireEvent.click(within(row).getByTitle('Configure per-model limits'));
        expect(onConfigureLimits).toHaveBeenCalledWith(m);
    });

    it('governance and limits stay reachable on an inherited row — they are read paths', () => {
        renderTable([model({ is_inherited: true })], { onConfigureGovernance: vi.fn() });
        expect(within(rowFor('Claude Opus')).getByTitle('Configure governance')).toBeInTheDocument();
    });
});

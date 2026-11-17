/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The group's AI Models tab.
 *
 * Pinned: the Direct/Inherited/All views, removal calls target this group and
 * the right model, a contributed column's cell is given the group and the model
 * and its two callbacks, a refusal is reported without claiming
 * success, and the provider badge prefers the serving platform over the model's
 * name (a Llama on Ollama is Ollama, not Meta). The table is stubbed to its
 * callbacks and the provider function it is handed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import type { LLModelInfo, ResourceGroup } from '../../../../lib/groupTypes';

const gateway = {
    fetchEffectiveLLMModels: vi.fn(),
    removeModelFromGroup: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn() };

vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../GroupLLModelsTableView', () => ({
    default: (p: {
        models: LLModelInfo[];
        onRemove: (id: string, name: string) => void;
        onConfigureGovernance: (m: LLModelInfo) => void;
        onConfigureLimits: (m: LLModelInfo) => void;
        extraColumns?: { id: string; header: string; render: (m: LLModelInfo) => React.ReactNode }[];
        getModelProvider: (name: string, key?: string) => { provider: string };
    }) => (
        <div>
            {p.models.map((m) => (
                <div key={m.id} data-testid={`row-${m.id}`}>
                    <span>{m.name}</span>
                    <span data-testid="provider">{p.getModelProvider(m.name, m.provider).provider}</span>
                    <button onClick={() => p.onRemove(m.id, m.name)}>remove</button>
                    <button onClick={() => p.onConfigureGovernance(m)}>governance</button>
                    <button onClick={() => p.onConfigureLimits(m)}>limits</button>
                    <span data-testid="columns">{(p.extraColumns ?? []).map((c) => c.header).join(',')}</span>
                    {(p.extraColumns ?? []).map((c) => (
                        <span key={c.id}>{c.render(m)}</span>
                    ))}
                </div>
            ))}
        </div>
    ),
}));
function dialog(id: string) {
    return {
        default: (p: { groupId: string; model: { id: string }; onSave: () => void; onClose: () => void }) => (
            <div data-testid={id}>
                {p.groupId}/{p.model.id}
                <button onClick={p.onSave}>{id}-save</button>
                <button onClick={p.onClose}>{id}-close</button>
            </div>
        ),
    };
}
vi.mock('../LLMGovernanceDialog', () => dialog('gov-dialog'));
vi.mock('../LLMModelLimitsDialog', () => dialog('limits-dialog'));

import GroupLLModelsTab from '../GroupLLModelsTab';
import { SlotProvider } from '../../../../app/SlotProvider';
import type { SlotRegistry } from '../../../../app/slots';

const GROUP = { id: 'rg-1', name: 'eng', display_name: 'Eng' } as ResourceGroup;
const onUpdate = vi.fn();
const model = (id: string, name: string, provider?: string) => ({ id, name, provider }) as unknown as LLModelInfo;

const GPT = model('m-gpt', 'gpt-5', 'openai');
const LLAMA = model('m-llama', 'llama-3.3-70b', 'ollama');

/** A column contributed to the tab's slot: says what it was given and offers its two callbacks. */
const PROBE_COLUMN: SlotRegistry = {
    'resource-group.model-columns': [
        {
            id: 'probe',
            header: 'Probe',
            Cell: ({ group, model, onBeforeChange, onChanged }) => (
                <span>
                    cell {group.id}/{model.id}
                    <button onClick={onBeforeChange}>before</button>
                    <button onClick={() => { onChanged().catch(() => {}); }}>changed</button>
                </span>
            ),
        },
    ],
};

async function open({ columns = false } = {}) {
    render(
        <SlotProvider slots={columns ? PROBE_COLUMN : {}}>
            <GroupLLModelsTab group={GROUP} onUpdate={onUpdate} />
        </SlotProvider>,
    );
    await screen.findByText('gpt-5');
    expect(gateway.fetchEffectiveLLMModels).toHaveBeenCalledWith('rg-1');
}
const row = (id: string) => within(screen.getByTestId(`row-${id}`));

describe('GroupLLModelsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        for (const fn of Object.values(gateway)) fn.mockReset();
        gateway.fetchEffectiveLLMModels.mockResolvedValue({
            direct_models: [GPT],
            inherited_models: [LLAMA],
            effective_models: [GPT, LLAMA],
            direct_count: 1,
            inherited_count: 1,
            effective_count: 2,
        });
        gateway.removeModelFromGroup.mockResolvedValue({});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('splits the views as the server did, and the search narrows by name or id', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Direct (1)' }));
        expect(screen.queryByText('llama-3.3-70b')).toBeNull();

        fireEvent.click(screen.getByRole('button', { name: 'Inherited (1)' }));
        expect(screen.queryByText('gpt-5')).toBeNull();
        expect(screen.getByText('Inherited Models')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'All (2)' }));
        const search = screen.getByPlaceholderText('Search models...');
        fireEvent.change(search, { target: { value: 'M-LLAMA' } });
        expect(screen.queryByText('gpt-5')).toBeNull();
        expect(screen.getByText('llama-3.3-70b')).toBeInTheDocument();

        fireEvent.change(search, { target: { value: 'nothing' } });
        expect(screen.getByText('No models found matching your search')).toBeInTheDocument();
    });

    it.each([
        ['Direct', 'No direct models assigned yet'],
        ['Inherited', 'No inherited models from parent groups'],
        ['All', 'No AI models available'],
    ])('the empty %s view says so', async (label, message) => {
        gateway.fetchEffectiveLLMModels.mockResolvedValue({
            direct_models: [],
            inherited_models: [],
            effective_models: [],
            direct_count: 0,
            inherited_count: 0,
            effective_count: 0,
        });
        render(<GroupLLModelsTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('No AI models available');
        fireEvent.click(screen.getByRole('button', { name: `${label} (0)` }));
        expect(screen.getByText(message)).toBeInTheDocument();
    });

    it('labels a model by who serves it, using the name only as a fallback', async () => {
        const cases: [string, string | undefined, string][] = [
            ['llama-3', 'ollama', 'Ollama'],
            ['anything', 'bedrock', 'Bedrock'],
            ['gpt-x', 'custom', 'OpenAI'],
            ['claude-x', undefined, 'Anthropic'],
            ['gemini-x', 'custom', 'Google'],
            ['llama-x', 'custom', 'Meta'],
            ['mistral-x', 'custom', 'Mistral'],
            ['mystery', 'made-up', 'Other'],
        ];
        const models = cases.map(([name, provider]) => model(name, name, provider));
        gateway.fetchEffectiveLLMModels.mockResolvedValue({ effective_models: models, effective_count: models.length });
        render(<GroupLLModelsTab group={GROUP} onUpdate={onUpdate} />);
        await screen.findByText('mystery');
        for (const [name, , expected] of cases) {
            expect(row(name).getByTestId('provider')).toHaveTextContent(expected);
        }
    });

    it('removes after typed confirmation, then reloads and tells the parent', async () => {
        await open();
        fireEvent.click(row('m-gpt').getByText('remove'));
        const input = await screen.findByPlaceholderText('Enter "gpt-5"');
        fireEvent.change(input, { target: { value: 'gpt-5' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        await waitFor(() => expect(gateway.removeModelFromGroup).toHaveBeenCalledWith('rg-1', 'm-gpt'));
        await waitFor(() =>
            expect(toast.success).toHaveBeenCalledWith('Model Removed', 'gpt-5 has been removed from Eng.'),
        );
        expect(onUpdate).toHaveBeenCalledTimes(1);
        expect(gateway.fetchEffectiveLLMModels).toHaveBeenCalledTimes(2);
        expect(screen.queryByPlaceholderText('Enter "gpt-5"')).toBeNull();
    });

    it('a refused removal stays open and shows the reason', async () => {
        gateway.removeModelFromGroup.mockRejectedValue({ detail: 'in use by a routing group' });
        await open();
        fireEvent.click(row('m-gpt').getByText('remove'));
        const input = await screen.findByPlaceholderText('Enter "gpt-5"');
        fireEvent.change(input, { target: { value: 'gpt-5' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(await screen.findByText('in use by a routing group')).toBeInTheDocument();
        expect(onUpdate).not.toHaveBeenCalled();
        expect(toast.success).not.toHaveBeenCalled();
    });

    it('gives a contributed column the group, the model and its two callbacks', async () => {
        await open({ columns: true });
        expect(row('m-gpt').getByTestId('columns')).toHaveTextContent('Probe');
        expect(row('m-gpt').getByText('cell rg-1/m-gpt')).toBeInTheDocument();
        expect(row('m-llama').getByText('cell rg-1/m-llama')).toBeInTheDocument();

        // onChanged reloads the models.
        fireEvent.click(row('m-gpt').getByText('changed'));
        await waitFor(() => expect(gateway.fetchEffectiveLLMModels).toHaveBeenCalledTimes(2));
    });

    it('clears its error when a contributed cell is about to write', async () => {
        await open({ columns: true });
        // A reload that fails leaves the tab's error showing over the rows it has.
        gateway.fetchEffectiveLLMModels.mockRejectedValueOnce(new Error('models service is down'));
        fireEvent.click(row('m-gpt').getByText('changed'));
        expect(await screen.findByText('models service is down')).toBeInTheDocument();
        fireEvent.click(row('m-gpt').getByText('before'));
        await waitFor(() => expect(screen.queryByText('models service is down')).toBeNull());
    });

    it('with nothing contributed the table gets no extra column', async () => {
        await open();
        expect(row('m-gpt').getByTestId('columns')).toHaveTextContent('');
        expect(row('m-gpt').queryByText('cell rg-1/m-gpt')).toBeNull();
    });

    it('opens the governance and limits dialogs for the chosen model, and reloads on save', async () => {
        await open();
        fireEvent.click(row('m-llama').getByText('governance'));
        expect(screen.getByTestId('gov-dialog')).toHaveTextContent('rg-1/m-llama');
        fireEvent.click(screen.getByText('gov-dialog-save'));
        await waitFor(() => expect(gateway.fetchEffectiveLLMModels).toHaveBeenCalledTimes(2));
        await screen.findByText('gov-dialog-close');
        fireEvent.click(screen.getByText('gov-dialog-close'));
        expect(screen.queryByTestId('gov-dialog')).toBeNull();

        fireEvent.click(row('m-gpt').getByText('limits'));
        expect(screen.getByTestId('limits-dialog')).toHaveTextContent('rg-1/m-gpt');
        fireEvent.click(screen.getByText('limits-dialog-close'));
        expect(screen.queryByTestId('limits-dialog')).toBeNull();
    });

    it('shows the two help panels and closes them', async () => {
        await open();
        fireEvent.click(screen.getByTitle('Learn about model inheritance'));
        expect(screen.getByText('What is Model Inheritance?')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Got it' }));
        expect(screen.queryByText('What is Model Inheritance?')).toBeNull();

        fireEvent.click(screen.getByTitle('Learn how to add models'));
        expect(screen.getByText('How to Add Models to a Resource Group')).toBeInTheDocument();
    });

    it('shows a load failure', async () => {
        gateway.fetchEffectiveLLMModels.mockRejectedValue(new Error('cp down'));
        render(<GroupLLModelsTab group={GROUP} onUpdate={onUpdate} />);
        expect(await screen.findByText('cp down')).toBeInTheDocument();
    });
});

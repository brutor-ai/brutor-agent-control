/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Routing groups list.
 *
 * Pinned against the real deployment contract: a deployment is keyed by
 * `model_id` and has no `id`, `name` or `is_routing`. Reading those gave the
 * complexity tier editor options with no value, so a tier saved the display name
 * as the model id and the server refused it. Also pinned: a tier map failure
 * must not read as a failed group save, because the group did save.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const { toastMock } = vi.hoisted(() => ({
    toastMock: { success: vi.fn(), error: vi.fn() },
}));
vi.mock('react-hot-toast', () => ({ default: toastMock }));

const client = {
    listRoutingGroups: vi.fn(),
    listDeploymentsByRoutingGroup: vi.fn(),
    getRoutingGroup: vi.fn(),
    getRoutingGroupTiers: vi.fn(),
    replaceRoutingGroupTiers: vi.fn(),
    createRoutingGroup: vi.fn(),
    updateRoutingGroup: vi.fn(),
    deleteRoutingGroup: vi.fn(),
};
const gateway = { gatewayClient: client };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

let formPayload: Record<string, unknown> = {};
const nextTiers = [{ tier: 'simple', llm_model_id: 'm1', position: 0 }];

vi.mock('../RoutingGroupForm', () => ({
    default: ({
        routingGroup,
        isEditing,
        onSubmit,
        onCancel,
        availableGroups,
        groupModels,
        initialTiers,
        onTiersChange,
    }: {
        routingGroup: { name: string } | null;
        isEditing: boolean;
        onSubmit: (data: unknown) => Promise<void>;
        onCancel: () => void;
        availableGroups: Array<{ id: string }>;
        groupModels: Array<{ id: string; name: string }>;
        initialTiers: unknown[];
        onTiersChange: (tiers: unknown[]) => void;
    }) => (
        <div>
            <p>form {isEditing ? `editing ${routingGroup?.name}` : 'creating'}</p>
            <p>others: {availableGroups.map(g => g.id).join(',')}</p>
            <p data-testid="models">{JSON.stringify(groupModels)}</p>
            <p data-testid="tiers">{JSON.stringify(initialTiers)}</p>
            <button onClick={() => onTiersChange(nextTiers)}>change tiers</button>
            <button onClick={() => { onSubmit(formPayload).catch(() => {}); }}>form save</button>
            <button onClick={onCancel}>form cancel</button>
        </div>
    ),
}));
vi.mock('../RoutingGroupHelpPanel', () => ({
    default: ({ onClose }: { onClose: () => void }) => <button onClick={onClose}>close guide</button>,
}));
vi.mock('../../config/providerLogos', () => ({
    ProviderLogo: ({ provider }: { provider: string }) => <span>logo {provider}</span>,
}));
vi.mock('../../../common/GenericDeleteConfirmationModal', () => ({
    default: ({
        isOpen,
        itemName,
        onConfirm,
        onClose,
        errorMessage,
        customWarning,
    }: {
        isOpen: boolean;
        itemName: string;
        onConfirm: () => void;
        onClose: () => void;
        errorMessage?: string | null;
        customWarning?: string;
    }) =>
        isOpen ? (
            <div>
                delete modal for {itemName}
                {customWarning && <p>{customWarning}</p>}
                {errorMessage && <p>{errorMessage}</p>}
                <button onClick={onConfirm}>confirm delete</button>
                <button onClick={onClose}>cancel delete</button>
            </div>
        ) : null,
}));

import RoutingGroupsSubTab from '../RoutingGroupsSubTab';

function listItem(overrides: Record<string, unknown> = {}) {
    return {
        id: 'g1',
        name: 'Pool One',
        routing_strategy: 'least-busy',
        enabled: true,
        num_models: 0,
        ...overrides,
    };
}

function deployment(overrides: Record<string, unknown> = {}) {
    return {
        model_id: 'm1',
        model_name: 'Model One',
        provider_model_name: 'gpt-x',
        provider: 'openai',
        enabled: true,
        health_status: 'healthy',
        routing_weight: 2,
        total_requests: 1500,
        error_rate: 0,
        ...overrides,
    };
}

function setGroups(...groups: Array<ReturnType<typeof listItem>>) {
    client.listRoutingGroups.mockResolvedValue({ routing_groups: groups });
}

function setDeployments(...deployments: Array<ReturnType<typeof deployment>>) {
    client.listDeploymentsByRoutingGroup.mockResolvedValue({ deployments });
}

function click(name: string) {
    fireEvent.click(screen.getByRole('button', { name }));
}

const onRefresh = vi.fn();
const onFormStateChange = vi.fn();

async function loadTab() {
    render(<RoutingGroupsSubTab onRefresh={onRefresh} onFormStateChange={onFormStateChange} />);
    await waitFor(() => expect(client.listRoutingGroups).toHaveBeenCalled());
    await waitFor(() => expect(document.querySelector('.animate-spin')).toBeNull());
}

async function openEdit() {
    await loadTab();
    fireEvent.click(screen.getByTitle('Edit routing group'));
    await screen.findByText('form editing Pool One');
}

describe('RoutingGroupsSubTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        for (const fn of Object.values(client)) fn.mockReset();
        setGroups();
        setDeployments();
        client.getRoutingGroup.mockResolvedValue({ id: 'g1', name: 'Pool One' });
        client.getRoutingGroupTiers.mockResolvedValue({ tiers: [] });
        client.replaceRoutingGroupTiers.mockResolvedValue({});
        formPayload = {};
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
        document.body.style.overflow = '';
    });

    describe('list', () => {
        it('asks for disabled groups too, and shows the empty state', async () => {
            await loadTab();
            expect(client.listRoutingGroups).toHaveBeenCalledWith({ enabled_only: false });
            expect(screen.getByText('No Routing Groups Yet')).toBeTruthy();
            expect(screen.getAllByRole('button', { name: 'Create Routing Group' })).toHaveLength(2);
        });

        it('loads members only for groups that have some, and shows their providers once', async () => {
            setGroups(listItem({ num_models: 3 }), listItem({ id: 'g2', name: 'Empty', num_models: 0 }));
            setDeployments(
                deployment(),
                deployment({ model_id: 'm2', model_name: 'Two' }),
                deployment({ model_id: 'v', provider: 'routing_group' })
            );
            await loadTab();
            expect(await screen.findByText('logo openai')).toBeTruthy();
            expect(client.listDeploymentsByRoutingGroup).toHaveBeenCalledTimes(1);
            expect(client.listDeploymentsByRoutingGroup).toHaveBeenCalledWith({
                routing_group_id: 'g1',
                enabled_only: false,
            });
            expect(screen.queryByText('logo routing_group')).toBeNull();
        });

        it('toasts when the list or the members fail', async () => {
            client.listRoutingGroups.mockRejectedValueOnce(new Error('down'));
            await loadTab();
            expect(toastMock.error).toHaveBeenCalledWith('Failed to load routing groups: down');
            cleanup();

            setGroups(listItem({ num_models: 1 }));
            client.listDeploymentsByRoutingGroup.mockRejectedValue(new Error('down'));
            await loadTab();
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to load models for routing group: down')
            );
        });

        it('filters by search, on name or id, and by enabled state', async () => {
            setGroups(
                listItem({ id: 'alpha-id', name: 'Pool One' }),
                listItem({ id: 'beta', name: 'Second', enabled: false })
            );
            await loadTab();
            const search = screen.getByPlaceholderText('Search routing groups...');

            fireEvent.change(search, { target: { value: 'ALPHA' } });
            expect(screen.getByText('Pool One')).toBeTruthy();
            expect(screen.queryByText('Second')).toBeNull();

            fireEvent.change(search, { target: { value: 'nothing' } });
            expect(screen.getByText('No Matching Groups')).toBeTruthy();
            expect(screen.queryByRole('button', { name: 'Create Routing Group' })).toBeTruthy();

            fireEvent.change(search, { target: { value: '' } });
            const filter = screen.getByRole('combobox');
            fireEvent.change(filter, { target: { value: 'disabled' } });
            expect(screen.queryByText('Pool One')).toBeNull();
            expect(screen.getByText('Second')).toBeTruthy();

            fireEvent.change(filter, { target: { value: 'enabled' } });
            expect(screen.getByText('Pool One')).toBeTruthy();
            expect(screen.queryByText('Second')).toBeNull();
        });

        it('shows the mode, defaulting to chat, with its endpoint as the title', async () => {
            setGroups(listItem(), listItem({ id: 'g2', mode: 'rerank' }), listItem({ id: 'g3', mode: 'mystery' }));
            await loadTab();
            expect(screen.getByText('chat').getAttribute('title')).toBe(
                '/v1/chat/completions — multi-turn conversations'
            );
            expect(screen.getByText('rerank').getAttribute('title')).toBe(
                '/v1/rerank — re-rank documents by relevance'
            );
            expect(screen.getByText('mystery').getAttribute('title')).toBe('Mode: mystery');
        });

        it('badges fallbacks and limits, and no alerting, which is not in this release', async () => {
            setGroups(
                listItem({
                    has_fallbacks: true,
                    fallback_count: 2,
                    ...({ alerting_enabled: true } as object),
                    rpm_limit: 100,
                    tpm_limit: 5000,
                })
            );
            await loadTab();
            expect(screen.getByTitle('2 fallback group(s)')).toBeTruthy();
            expect(screen.queryByTitle('Alerting enabled')).toBeNull();
            expect(screen.getByTitle(`100 RPM, ${(5000).toLocaleString()} TPM`)).toBeTruthy();
        });

        it('expands to configuration and members, and collapses again', async () => {
            setGroups(
                listItem({
                    num_models: 4,
                    num_retries: 2,
                    cooldown_time: 9,
                    max_parallel_requests: 7,
                    rpm_limit: 11,
                    tpm_limit: 1000,
                    has_fallbacks: true,
                    fallback_count: 4,
                    fallback_models: ['fb-general'],
                    context_window_fallbacks: ['fb-window'],
                    content_policy_fallbacks: ['fb-policy'],
                    default_fallbacks: ['fb-default'],
                })
            );
            setDeployments(
                deployment(),
                deployment({ model_id: 'm2', model_name: 'Degraded One', health_status: 'degraded' }),
                deployment({ model_id: 'm3', model_name: 'Sick One', health_status: 'unhealthy' }),
                deployment({ model_id: 'm4', model_name: 'Off One', enabled: false }),
                deployment({ model_id: 'm5', model_name: 'Odd One', health_status: 'weird' }),
                deployment({ model_id: 'v', model_name: 'Virtual', provider: 'routing_group' })
            );
            await loadTab();
            await screen.findByText('logo openai');
            fireEvent.click(screen.getByText('Pool One'));

            expect(await screen.findByText('Model One')).toBeTruthy();
            // Retries and cooldown; the group's timeout and allowed failures
            // are not the gateway's and are not shown.
            expect(screen.getByText('Retries').nextElementSibling?.textContent).toBe('2x');
            expect(screen.getByText('Cooldown').nextElementSibling?.textContent).toBe('9s');
            expect(screen.queryByText(/Timeout/)).toBeNull();
            expect(screen.queryByText(/Fails/)).toBeNull();
            expect(screen.getByText('Fallbacks (4)')).toBeTruthy();
            for (const id of ['fb-general', 'fb-window', 'fb-policy', 'fb-default']) {
                expect(screen.getByText(id)).toBeTruthy();
            }
            expect(screen.getByText('Sick One')).toBeTruthy();
            expect(screen.getAllByText('w:2')).toHaveLength(5);
            expect(screen.queryByText('Virtual')).toBeNull();

            fireEvent.click(screen.getByText('Pool One'));
            expect(screen.queryByText('Model One')).toBeNull();
        });

        it('says so when an expanded group has no models', async () => {
            setGroups(listItem());
            await loadTab();
            fireEvent.click(screen.getByText('Pool One'));
            expect(await screen.findByText('No models assigned')).toBeTruthy();
            expect(screen.getByText('Retries').nextElementSibling?.textContent).toBe('3x');
            expect(screen.getByText('Cooldown').nextElementSibling?.textContent).toBe('60s');
        });

        it('swaps the banner for the guide and pauses auto-refresh meanwhile', async () => {
            await loadTab();
            expect(screen.getByText('Routing Groups')).toBeTruthy();
            onFormStateChange.mockClear();

            click('Guide');
            expect(screen.queryByText('Routing Groups')).toBeNull();
            await waitFor(() => expect(onFormStateChange).toHaveBeenLastCalledWith(true));

            click('close guide');
            expect(screen.getByText('Routing Groups')).toBeTruthy();
            await waitFor(() => expect(onFormStateChange).toHaveBeenLastCalledWith(false));
        });
    });

    it('resumes the parent auto-refresh when it unmounts with the guide open', async () => {
        await loadTab();
        click('Guide');
        await waitFor(() => expect(onFormStateChange).toHaveBeenLastCalledWith(true));
        cleanup();
        expect(onFormStateChange).toHaveBeenLastCalledWith(false);
    });

    describe('create', () => {
        it('creates, closes, reloads and tells the parent', async () => {
            setGroups(listItem());
            client.createRoutingGroup.mockResolvedValue({ id: 'g-new' });
            await loadTab();
            click('Create Routing Group');
            expect(screen.getByRole('heading', { name: 'Create Routing Group' })).toBeTruthy();
            expect(screen.getByText('others: g1')).toBeTruthy();
            expect(document.body.style.overflow).toBe('hidden');

            formPayload = { id: 'g-new', name: 'New', mode: 'chat', routing_strategy: 'least-busy' };
            click('form save');

            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('Routing group created successfully')
            );
            expect(client.createRoutingGroup).toHaveBeenCalledWith(formPayload);
            expect(client.replaceRoutingGroupTiers).not.toHaveBeenCalled();
            await waitFor(() => expect(screen.queryByText('form creating')).toBeNull());
            await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
            expect(client.listRoutingGroups).toHaveBeenCalledTimes(2);
            expect(document.body.style.overflow).toBe('unset');
        });

        it('saves the tier map against the new id for complexity routing', async () => {
            client.createRoutingGroup.mockResolvedValue({ id: 'g-new' });
            await loadTab();
            fireEvent.click(screen.getAllByRole('button', { name: 'Create Routing Group' })[0]);
            click('change tiers');
            formPayload = { id: 'g-new', name: 'New', mode: 'chat', routing_strategy: 'complexity-based-routing' };
            click('form save');

            await waitFor(() =>
                expect(client.replaceRoutingGroupTiers).toHaveBeenCalledWith('g-new', nextTiers)
            );
        });

        it('stays open and reports a failed create', async () => {
            client.createRoutingGroup.mockRejectedValue(new Error('id taken'));
            await loadTab();
            fireEvent.click(screen.getAllByRole('button', { name: 'Create Routing Group' })[0]);
            formPayload = { id: 'g1', name: 'New', mode: 'chat' };
            click('form save');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to create routing group: id taken')
            );
            expect(screen.getByText('form creating')).toBeTruthy();
            expect(onRefresh).not.toHaveBeenCalled();
        });

        it.each([
            ['form cancel', () => click('form cancel')],
            ['the Escape key', () => fireEvent.keyDown(document, { key: 'Escape' })],
        ])('closes on %s', async (_name, close) => {
            await loadTab();
            fireEvent.click(screen.getAllByRole('button', { name: 'Create Routing Group' })[0]);
            expect(screen.getByText('form creating')).toBeTruthy();
            close();
            await waitFor(() => expect(screen.queryByText('form creating')).toBeNull());
            expect(client.createRoutingGroup).not.toHaveBeenCalled();
        });
    });

    describe('edit', () => {
        beforeEach(() => setGroups(listItem()));

        it('gives the tier editor real member ids, without the virtual routing model', async () => {
            setDeployments(
                deployment(),
                deployment({ model_id: 'm2', model_name: '' }),
                deployment({ model_id: 'virtual', model_name: 'Virtual', provider: 'routing_group' })
            );
            await openEdit();
            expect(client.getRoutingGroup).toHaveBeenCalledWith('g1');
            expect(JSON.parse(screen.getByTestId('models').textContent ?? '')).toEqual([
                { id: 'm1', name: 'Model One' },
                { id: 'm2', name: 'm2' },
            ]);
        });

        it('hands over the existing tier map, trimmed to what the editor needs', async () => {
            client.getRoutingGroupTiers.mockResolvedValue({
                tiers: [{ tier: 'complex', llm_model_id: 'm1', position: 0, model_name: 'extra' }],
            });
            await openEdit();
            expect(JSON.parse(screen.getByTestId('tiers').textContent ?? '')).toEqual([
                { tier: 'complex', llm_model_id: 'm1', position: 0 },
            ]);
        });

        it('still opens when members and tiers cannot be loaded, and says that they were not', async () => {
            client.listDeploymentsByRoutingGroup.mockRejectedValue(new Error('down'));
            client.getRoutingGroupTiers.mockRejectedValue(new Error('The tiers of g1 could not be read'));
            await openEdit();
            expect(screen.getByTestId('models').textContent).toBe('[]');
            expect(screen.getByTestId('tiers').textContent).toBe('[]');
            // An empty list in the form is not "none stored": the user is told why it is empty.
            expect(toastMock.error).toHaveBeenCalledWith('Failed to load the models of the routing group: down');
            expect(toastMock.error).toHaveBeenCalledWith('Failed to load the tiers of the routing group: The tiers of g1 could not be read');
        });

        it('does not open when the group itself cannot be loaded', async () => {
            client.getRoutingGroup.mockRejectedValue(new Error('gone'));
            await loadTab();
            fireEvent.click(screen.getByTitle('Edit routing group'));
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to load routing group details: gone')
            );
            expect(screen.queryByText(/form editing/)).toBeNull();
        });

        it('updates, and only saves tiers for the strategy that reads them', async () => {
            client.updateRoutingGroup.mockResolvedValue({});
            await openEdit();
            formPayload = { name: 'Renamed', routing_strategy: 'least-busy' };
            click('form save');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('Routing group updated successfully')
            );
            expect(client.updateRoutingGroup).toHaveBeenCalledWith('g1', formPayload);
            expect(client.createRoutingGroup).not.toHaveBeenCalled();
            expect(client.replaceRoutingGroupTiers).not.toHaveBeenCalled();
            await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
        });

        it('saves the edited tier map for complexity routing', async () => {
            client.updateRoutingGroup.mockResolvedValue({});
            await openEdit();
            click('change tiers');
            formPayload = { id: 'g-new', name: 'New', mode: 'chat', routing_strategy: 'complexity-based-routing' };
            click('form save');
            await waitFor(() =>
                expect(client.replaceRoutingGroupTiers).toHaveBeenCalledWith('g1', nextTiers)
            );
        });

        it('does not call a tier map failure a failed save', async () => {
            client.updateRoutingGroup.mockResolvedValue({});
            client.replaceRoutingGroupTiers.mockRejectedValue(new Error('not a member'));
            await openEdit();
            formPayload = { id: 'g-new', name: 'New', mode: 'chat', routing_strategy: 'complexity-based-routing' };
            click('form save');

            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Group saved, but the tier map did not: not a member')
            );
            expect(toastMock.success).toHaveBeenCalledWith('Routing group updated successfully');
            await waitFor(() => expect(screen.queryByText('form editing Pool One')).toBeNull());
        });

        it('says in plain words that a cloud model the gateway would skip cannot be a fallback', async () => {
            const { thrownByClient } = await import('../../../../test/serverAnswers');
            client.updateRoutingGroup.mockRejectedValue(
                await thrownByClient(422, {
                    detail:
                        "fallback_models: unsupported_provider_api: model 'anthropic.claude-sonnet-4' (provider 'bedrock') cannot be a fallback: the gateway serves it on Amazon Bedrock only when it is the model a request names, never as a candidate it forwards to, so it would skip it on every request. As a member or a fallback it forwards to Amazon Nova, Mistral and DeepSeek chat models on Bedrock (chat) and to the Amazon Titan Text and Cohere Embed v3 models on Bedrock (embedding), and to no other cloud model.",
                }),
            );
            await openEdit();
            click('form save');
            await waitFor(() => expect(toastMock.error).toHaveBeenCalled());
            const calls = toastMock.error.mock.calls as unknown[][];
            const said = String(calls[calls.length - 1][0]);
            expect(said).toMatch(/^Failed to update routing group: Model "anthropic.claude-sonnet-4" cannot be a fallback\. The gateway serves it on Amazon Bedrock only when a request names it/);
            expect(said).not.toContain('unsupported_provider_api');
            expect(screen.getByText('form editing Pool One')).toBeTruthy();
        });

        it('stays open and reports a failed update', async () => {
            client.updateRoutingGroup.mockRejectedValue(new Error('conflict'));
            await openEdit();
            click('form save');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to update routing group: conflict')
            );
            expect(screen.getByText('form editing Pool One')).toBeTruthy();
        });
    });

    describe('delete', () => {
        beforeEach(() => setGroups(listItem({ num_models: 2 })));

        it('warns about members, then deletes and reports how many were disconnected', async () => {
            client.deleteRoutingGroup.mockResolvedValue({ models_disconnected: 2 });
            await loadTab();
            onFormStateChange.mockClear();
            fireEvent.click(screen.getByTitle('Delete routing group'));

            expect(screen.getByText('delete modal for Pool One')).toBeTruthy();
            expect(screen.getByText(/has 2 model\(s\) assigned/)).toBeTruthy();
            expect(onFormStateChange).toHaveBeenLastCalledWith(true);
            expect(client.deleteRoutingGroup).not.toHaveBeenCalled();

            click('confirm delete');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('Routing group deleted — 2 model(s) disconnected')
            );
            expect(client.deleteRoutingGroup).toHaveBeenCalledWith('g1');
            expect(screen.queryByText('delete modal for Pool One')).toBeNull();
            expect(onFormStateChange).toHaveBeenLastCalledWith(false);
            await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
        });

        it('reports a plain delete when nothing was attached', async () => {
            setGroups(listItem());
            client.deleteRoutingGroup.mockResolvedValue({ models_disconnected: 0 });
            await loadTab();
            fireEvent.click(screen.getByTitle('Delete routing group'));
            expect(screen.queryByText(/model\(s\) assigned/)).toBeNull();
            click('confirm delete');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('Routing group deleted successfully')
            );
        });

        it('keeps the modal open with the server error', async () => {
            client.deleteRoutingGroup.mockRejectedValue(new Error('referenced as a fallback'));
            await loadTab();
            fireEvent.click(screen.getByTitle('Delete routing group'));
            click('confirm delete');
            expect(await screen.findByText('referenced as a fallback')).toBeTruthy();
            expect(toastMock.success).not.toHaveBeenCalled();
            expect(onRefresh).not.toHaveBeenCalled();
        });

        it('cancels and resumes auto-refresh', async () => {
            await loadTab();
            fireEvent.click(screen.getByTitle('Delete routing group'));
            click('cancel delete');
            expect(screen.queryByText('delete modal for Pool One')).toBeNull();
            expect(onFormStateChange).toHaveBeenLastCalledWith(false);
            expect(client.deleteRoutingGroup).not.toHaveBeenCalled();
        });
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLM models tab: the list, its sort, and create/update/delete/copy/toggle.
 *
 * Two more things are pinned. The post-save health check knows the mode of a
 * model just created: run from a timer whose closure holds the stale `models`,
 * it would probe on the 30 s default, which aborts image and video probes
 * mid-call. And sorting by size in ascending order does not float models of
 * unknown size to the top.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const stubs = vi.hoisted(() => () => import('../../../../test/llm/llmModelTab'));
vi.mock('react-hot-toast', async () => (await stubs()).toastModule());
vi.mock('../../../../hooks/useGateway', async () => (await stubs()).useGatewayModule());
vi.mock('../../../../hooks/useAIModelHealthCheck', async () => (await stubs()).useAIModelHealthCheckModule());
vi.mock('../LLModelCard', async () => (await stubs()).itemModule('card'));
vi.mock('../LLModelListItem', async () => (await stubs()).itemModule('row'));
vi.mock('../LLModelForm', async () => (await stubs()).formModule());
vi.mock('../LLMCatalogTree', async () => (await stubs()).catalogTreeModule());
vi.mock('../LLMSearchModal', async () => (await stubs()).searchModalModule());
vi.mock('../HealthCheckHelpModal', async () => (await stubs()).closeOnlyModalModule('health help close'));
vi.mock('../LLMProxyEndpointModal', async () => (await stubs()).proxyModalModule());
vi.mock('../../../common/GenericDeleteConfirmationModal', async () => (await stubs()).deleteModalModule());

import LLModelTab from '../LLModelTab';
import { WithBuiltinSlots } from '../../../../test/builtinSlots';
import { SlotProvider } from '../../../../app/SlotProvider';
import { thrownByClient } from '../../../../test/serverAnswers';
import { explainDispatchRefusal } from '../../../../lib/dispatchRefusal';
import { GATEWAY_MESSAGES, proxyReturned, type GatewayMessageName } from '../../../../lib/__tests__/fixtures/dispatchRefusals';
import type { ModelImportSlotItem } from '../../../../app/slots';
import {
    click,
    clickAddNew,
    client,
    formStub,
    health,
    healthOptions,
    llm,
    resetLLModelTab,
    setModels,
    toastMock,
} from '../../../../test/llm/llmModelTab';

vi.setConfig({ testTimeout: 20_000 });

function names(): string[] {
    return screen.queryAllByTestId('item-name').map(n => n.textContent ?? '');
}

/** Rendered the way the application does: with the built-in slot items. */
async function loadTab(props: { initialFocusModelId?: string | null } = {}) {
    render(<LLModelTab {...props} />, { wrapper: WithBuiltinSlots });
    await screen.findByText('Enabled Models');
}

/** The post-save check is delayed 2 s by design; waiting for it keeps it out of the next test. */
async function postSaveCheckDone() {
    await waitFor(() => expect(client.testLLMConnection).toHaveBeenCalled(), { timeout: 4000 });
}

/**
 * What the API client throws when the control plane answers with `detail`:
 * 422 for a list of validation errors, 400 for anything else. The real
 * client is given the answer; nothing about the error's shape is made up.
 */
function rejection(detail: unknown) {
    return thrownByClient(Array.isArray(detail) ? 422 : 400, { detail });
}

describe('LLModelTab', () => {
    beforeEach(resetLLModelTab);

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('list', () => {
        it('splits enabled and imported models and counts them', async () => {
            setModels(
                llm('a'),
                llm('b', { provider: 'anthropic' }),
                llm('imp', { enabled: false, source: 'import' })
            );
            await loadTab();
            expect(names()).toEqual(['A', 'B', 'IMP']);
            expect(screen.getByText('2 of 3 active across 2 providers')).toBeTruthy();
            expect(screen.getByText('1 discovered via Traffic Data Import')).toBeTruthy();
        });

        // "Observed (imported) models" is Community UI over
        // Community data, not an Enterprise piece behind a slot. These hold
        // what that decision rests on: it needs no slot item, and without
        // rows of source 'import' (only the Enterprise traffic import writes
        // them) it is not there.
        describe('the observed models section', () => {
            const SECTION = 'Observed (imported) models';

            it('is drawn without any slot item, and enabling a model there is the Community update', async () => {
                setModels(llm('a'), llm('imp', { enabled: false, source: 'import' }));
                client.updateLLM.mockResolvedValue({});
                // No SlotProvider: every slot is empty, as in a Community-only build.
                render(<LLModelTab />);
                await screen.findByText('Enabled Models');
                expect(screen.getByText(SECTION)).toBeTruthy();
                expect(screen.getByText('1 discovered via Traffic Data Import')).toBeTruthy();
                expect(names()).toEqual(['A', 'IMP']);
                // The import sources are the slot's; none here.
                expect(screen.queryByRole('button', { name: /hugging\s*face/i })).toBeNull();

                click('enable IMP');
                await waitFor(() => expect(client.updateLLM).toHaveBeenCalledWith('imp', { enabled: true }));
            });

            it('is absent when no model was imported, with or without slot items; the rest of the tab works', async () => {
                // A disabled model that was added by hand is not an observed one.
                setModels(llm('a'), llm('off', { enabled: false, source: 'manual' }), llm('plain', { enabled: false }));
                const { unmount } = render(<LLModelTab />);
                await screen.findByText('Enabled Models');
                expect(screen.queryByText(SECTION)).toBeNull();
                expect(screen.queryByText(/discovered via Traffic Data Import/)).toBeNull();
                // They are listed as disabled models.
                expect(names()).toEqual(['A', 'OFF', 'PLAIN']);
                expect(screen.getByText('1 of 3 active across 1 provider')).toBeTruthy();
                clickAddNew();
                expect(screen.getByText('form for a new model')).toBeTruthy();
                unmount();

                await loadTab();
                expect(screen.queryByText(SECTION)).toBeNull();
                expect(names()).toEqual(['A', 'OFF', 'PLAIN']);
            });

            it('an imported model that was enabled is listed with the enabled ones, not as observed', async () => {
                setModels(llm('imp', { enabled: true, source: 'import' }));
                await loadTab();
                expect(names()).toEqual(['IMP']);
                expect(screen.queryByText(SECTION)).toBeNull();
            });
        });

        it('shows the empty state, which offers both ways in', async () => {
            await loadTab();
            expect(screen.getByText('No Enabled Models')).toBeTruthy();
            click('Browse Catalog');
            expect(screen.getByText('Browse Model Catalog')).toBeTruthy();
        });

        it('toasts when the list cannot be loaded', async () => {
            client.listLLMs.mockRejectedValue(new Error('boom'));
            await loadTab();
            expect(toastMock.error).toHaveBeenCalledWith('Failed to load LLM models: boom');
        });

        it('switches between card and row layouts', async () => {
            setModels(llm('a'));
            await loadTab();
            expect(screen.getByTestId('model-item').getAttribute('data-kind')).toBe('card');
            fireEvent.click(screen.getByTitle('List view'));
            expect(screen.getByTestId('model-item').getAttribute('data-kind')).toBe('row');
            fireEvent.click(screen.getByTitle('Card view'));
            expect(screen.getByTestId('model-item').getAttribute('data-kind')).toBe('card');
        });

        it('reloads on Refresh', async () => {
            await loadTab();
            click('Refresh');
            await waitFor(() => expect(client.listLLMs).toHaveBeenCalledTimes(2));
            expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy();
        });

        it('marks the deep-linked model as focused', async () => {
            setModels(llm('a'), llm('b'));
            await loadTab({ initialFocusModelId: 'b' });
            expect(await screen.findByText('focused B')).toBeTruthy();
            expect(screen.queryByText('focused A')).toBeNull();
        });

        it('ignores a deep link to a model that no longer exists', async () => {
            setModels(llm('a'));
            await loadTab({ initialFocusModelId: 'gone' });
            expect(screen.queryByText(/^focused/)).toBeNull();
            expect(names()).toEqual(['A']);
        });

        describe('the disabled models section', () => {
            const SECTION = 'Disabled models';

            it('lists a model that is switched off, so that it can be given what it lacks, enabled or deleted', async () => {
                setModels(llm('a'), llm('off', { enabled: false, source: 'manual' }), llm('plain', { enabled: false }));
                client.updateLLM.mockResolvedValue({});
                await loadTab();
                expect(screen.getByText(SECTION)).toBeTruthy();
                expect(screen.getByText('2 switched off')).toBeTruthy();
                expect(names()).toEqual(['A', 'OFF', 'PLAIN']);

                click('enable OFF');
                await waitFor(() => expect(client.updateLLM).toHaveBeenCalledWith('off', { enabled: true }));
                click('edit PLAIN');
                expect(screen.getByText('form for PLAIN')).toBeTruthy();
            });

            it('is shown when no model is enabled at all', async () => {
                setModels(llm('off', { enabled: false }));
                await loadTab();
                expect(screen.getByText('No Enabled Models')).toBeTruthy();
                expect(screen.getByText(SECTION)).toBeTruthy();
                expect(names()).toEqual(['OFF']);
            });

            it('is absent when every model is enabled, and leaves observed models to their own section', async () => {
                setModels(llm('a'), llm('imp', { enabled: false, source: 'import' }));
                await loadTab();
                expect(screen.queryByText(SECTION)).toBeNull();
                expect(names()).toEqual(['A', 'IMP']);
            });
        });
        it.todo(
            'BUG: LLModelCard/LLModelListItem get no onNavigateToRoutingGroup from this tab, so the "Go to Routing Group" control on a routing model does nothing'
        );
    });

    describe('sort', () => {
        function sortBy(value: string) {
            fireEvent.change(screen.getByRole('combobox'), { target: { value } });
        }

        beforeEach(() =>
            setModels(
                llm('m', {
                    name: 'Mid',
                    provider: 'zeta',
                    parameters: 8,
                    updated_at: '2026-02-01T00:00:00Z',
                    performance_metrics: { total_requests: 5, total_tokens_consumed: 900 },
                }),
                llm('a', {
                    name: 'alpha',
                    provider: 'beta',
                    updated_at: '2026-03-01T00:00:00Z',
                    performance_metrics: { total_requests: 9, total_tokens_consumed: 100 },
                }),
                llm('z', {
                    name: 'Zed',
                    provider: 'alpha',
                    parameters: 70,
                    updated_at: '2026-01-01T00:00:00Z',
                    performance_metrics: { total_requests: 1, total_tokens_consumed: 500 },
                })
            )
        );

        it('defaults to name, ignoring case, and reverses', async () => {
            await loadTab();
            expect(names()).toEqual(['alpha', 'Mid', 'Zed']);
            fireEvent.click(screen.getByLabelText('Toggle sort direction (currently asc)'));
            expect(names()).toEqual(['Zed', 'Mid', 'alpha']);
        });

        it.each([
            ['provider', ['Zed', 'alpha', 'Mid']],
            ['requests', ['Zed', 'Mid', 'alpha']],
            ['tokens', ['alpha', 'Zed', 'Mid']],
            ['updated', ['Zed', 'Mid', 'alpha']],
        ])('sorts by %s', async (key, expected) => {
            await loadTab();
            sortBy(key);
            expect(names()).toEqual(expected);
        });

        it('keeps unknown size last in both directions', async () => {
            await loadTab();
            sortBy('size');
            expect(names()).toEqual(['Mid', 'Zed', 'alpha']);
            fireEvent.click(screen.getByLabelText('Toggle sort direction (currently asc)'));
            expect(names()).toEqual(['Zed', 'Mid', 'alpha']);
        });

        it('remembers the choice across mounts', async () => {
            await loadTab();
            sortBy('requests');
            cleanup();
            await loadTab();
            expect(screen.getByRole<HTMLSelectElement>('combobox').value).toBe('requests');
            expect(names()).toEqual(['Zed', 'Mid', 'alpha']);
        });
    });

    describe('create', () => {
        const valid = { name: 'New', provider: 'openai', model_name: 'gpt', api_url_auto_discovered: true };

        async function openForm() {
            await loadTab();
            clickAddNew();
            expect(screen.getByText('form for a new model')).toBeTruthy();
        }

        // A refusal is thrown to the form, not toasted: a save that
        // returns is a save that worked, and the form would say "Model
        // created" and close with nothing created. The form is told, and stays.
        it('refuses a form missing required fields: the form is told, and nothing is created', async () => {
            await openForm();
            formStub.payload = { name: 'New' };
            click('form save');
            expect(await screen.findByText('form error: Name, provider and model name are required. Nothing was created.')).toBeTruthy();
            expect(client.createLLM).not.toHaveBeenCalled();
            expect(toastMock.success).not.toHaveBeenCalled();
            expect(screen.getByText('form for a new model')).toBeTruthy();
        });

        it('requires a URL unless auto-discovered: the form is told, and nothing is created', async () => {
            await openForm();
            formStub.payload = { name: 'New', provider: 'x', model_name: 'y', api_url_auto_discovered: false };
            click('form save');
            expect(
                await screen.findByText('form error: API URL is required when Auto-discover API URL is off. Nothing was created.'),
            ).toBeTruthy();
            expect(client.createLLM).not.toHaveBeenCalled();
            expect(toastMock.success).not.toHaveBeenCalled();
            expect(screen.getByText('form for a new model')).toBeTruthy();
        });

        it('strips read-only fields, defaults enabled, then closes and reloads', async () => {
            client.createLLM.mockResolvedValue({ id: 'new-1', mode: 'chat' });
            await openForm();
            formStub.payload = {
                ...valid,
                id: 'stale',
                health_status: {},
                performance_metrics: {},
                created_at: 'x',
                updated_at: 'y',
                groups: [],
                group_ids: ['g1'],
            };
            click('form save');

            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('LLM Model "New" created successfully')
            );
            expect(client.createLLM).toHaveBeenCalledWith({ ...valid, group_ids: ['g1'], enabled: true });
            expect(screen.queryByText('form for a new model')).toBeNull();
            await waitFor(() => expect(client.listLLMs).toHaveBeenCalledTimes(2));
            await postSaveCheckDone();
        });

        it('writes nothing of what the form held to the browser console', async () => {
            // The form holds the provider's credentials.
            const log = vi.spyOn(console, 'log').mockImplementation(() => {});
            client.createLLM.mockResolvedValue({ id: 'new-1', mode: 'chat' });
            await openForm();
            formStub.payload = { ...valid, api_key: 'sk-secret-value' };
            click('form save');
            await waitFor(() => expect(client.createLLM).toHaveBeenCalled());
            await postSaveCheckDone();
            expect(log).not.toHaveBeenCalled();
            log.mockRestore();
        });

        it('keeps an explicit enabled=false', async () => {
            client.createLLM.mockResolvedValue({});
            await openForm();
            formStub.payload = { ...valid, enabled: false };
            click('form save');
            await waitFor(() => expect(client.createLLM).toHaveBeenCalled());
            expect(client.createLLM).toHaveBeenCalledWith({ ...valid, enabled: false });
        });

        it('probes a new model with its real mode, so slow modes get the long timeout', async () => {
            client.createLLM.mockResolvedValue({ id: 'img-1', mode: 'image_generation' });
            await openForm();
            formStub.payload = { ...valid, mode: 'image_generation' };
            click('form save');

            await postSaveCheckDone();
            expect(client.testLLMConnection).toHaveBeenCalledWith('img-1', 'image_generation');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('Health check passed: New (12ms)', { id: 'toast-id' })
            );
        });

        it('hands a validation error list back to the form as readable lines', async () => {
            client.createLLM.mockRejectedValue(
                await rejection([
                    { loc: ['body', 'name'], msg: 'too short', input: 'x', type: 'string_too_short' },
                    { msg: 'bad' },
                ])
            );
            await openForm();
            formStub.payload = valid;
            click('form save');
            const error = await screen.findByText(/form error:/);
            expect(error.textContent).toBe('form error: name: too short\nfield: bad');
            expect(toastMock.success).not.toHaveBeenCalled();
        });

        it('hands the form the validation errors themselves, without what was typed', async () => {
            // A 422 repeats the `input` that failed. The form gets where and
            // what; the input stays out of anything it could print.
            client.createLLM.mockRejectedValue(
                await rejection([
                    { loc: ['body', 'api_key'], msg: 'too long', input: 'typed-api-key-value', type: 'string_too_long' },
                ])
            );
            await openForm();
            formStub.payload = valid;
            click('form save');
            await screen.findByText(/form error:/);
            const caught = formStub.refused;
            expect(caught).toBeInstanceOf(Error);
            expect((caught as { validationErrors?: unknown }).validationErrors).toEqual([
                { loc: ['body', 'api_key'], msg: 'too long' },
            ]);
            expect(JSON.stringify(caught, Object.getOwnPropertyNames(caught as object))).not.toContain('typed-api-key-value');
        });

        it('hands a string detail, or the plain message, back to the form', async () => {
            client.createLLM.mockRejectedValueOnce(await rejection('Name already taken'));
            await openForm();
            formStub.payload = valid;
            click('form save');
            expect(await screen.findByText('form error: Name already taken')).toBeTruthy();

            client.createLLM.mockRejectedValueOnce(new Error('network down'));
            click('form save');
            expect(await screen.findByText('form error: network down')).toBeTruthy();
        });
    });

    describe('update', () => {
        it('sends the updates, closes, reloads and probes with the edited mode', async () => {
            setModels(llm('a', { mode: 'chat' }));
            client.updateLLM.mockResolvedValue({});
            await loadTab();
            click('edit A');
            expect(screen.getByText('form for A')).toBeTruthy();
            formStub.payload = { name: 'Renamed', mode: 'video_generation' };
            click('form save');

            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('LLM Model "Renamed" updated successfully')
            );
            expect(client.updateLLM).toHaveBeenCalledWith('a', formStub.payload);
            expect(screen.queryByText('form for A')).toBeNull();
            await postSaveCheckDone();
            expect(client.testLLMConnection).toHaveBeenCalledWith('a', 'video_generation');
        });

        it('falls back to the model mode and reports a failed probe', async () => {
            setModels(llm('a', { mode: 'embedding' }));
            client.updateLLM.mockResolvedValue({});
            client.testLLMConnection.mockResolvedValue({ status: 'unhealthy', error_message: 'bad key' });
            await loadTab();
            click('edit A');
            formStub.payload = { description: 'd' };
            click('form save');
            await postSaveCheckDone();
            expect(client.testLLMConnection).toHaveBeenCalledWith('a', 'embedding');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Health check failed: A — bad key', {
                    id: 'toast-id',
                    duration: 7000,
                })
            );
        });

        it('says what to fix when the check after a save is refused by the gateway', async () => {
            const raw = proxyReturned(GATEWAY_MESSAGES.provider_api_url_missing);
            setModels(llm('a'));
            client.updateLLM.mockResolvedValue({});
            client.testLLMConnection.mockResolvedValue({ status: 'unhealthy', error_message: raw });
            await loadTab();
            click('edit A');
            formStub.payload = {};
            click('form save');
            await postSaveCheckDone();
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith(
                    `Health check failed: A — ${explainDispatchRefusal(raw)}`,
                    { id: 'toast-id', duration: 7000 },
                )
            );
        });

        it('reports a probe that throws', async () => {
            setModels(llm('a'));
            client.updateLLM.mockResolvedValue({});
            client.testLLMConnection.mockRejectedValue(new Error('timeout'));
            await loadTab();
            click('edit A');
            formStub.payload = {};
            click('form save');
            await postSaveCheckDone();
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Health check failed: A — timeout', {
                    id: 'toast-id',
                    duration: 7000,
                })
            );
        });

        it('leaves the form open with the server error', async () => {
            setModels(llm('a'));
            client.updateLLM.mockRejectedValueOnce(await rejection([{ loc: ['body', 'timeout'], msg: 'too big' }]));
            await loadTab();
            click('edit A');
            formStub.payload = {};
            click('form save');
            expect(await screen.findByText('form error: timeout: too big')).toBeTruthy();
            expect(screen.getByText('form for A')).toBeTruthy();

            client.updateLLM.mockRejectedValueOnce(await rejection('Read only'));
            click('form save');
            expect(await screen.findByText('form error: Read only')).toBeTruthy();

            client.updateLLM.mockRejectedValueOnce(new Error('down'));
            click('form save');
            expect(await screen.findByText('form error: down')).toBeTruthy();
        });
    });

    describe('delete', () => {
        it('deletes only after confirming, then clears the focus and reloads', async () => {
            setModels(llm('a'));
            client.deleteLLM.mockResolvedValue({});
            await loadTab({ initialFocusModelId: 'a' });
            await screen.findByText('focused A');
            click('delete A');
            expect(screen.getByText('delete modal for A')).toBeTruthy();
            expect(client.deleteLLM).not.toHaveBeenCalled();

            setModels();
            click('confirm delete');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('LLM Model "A" deleted successfully')
            );
            expect(client.deleteLLM).toHaveBeenCalledWith('a');
            await waitFor(() => expect(names()).toEqual([]));
            expect(screen.queryByText('delete modal for A')).toBeNull();
        });

        it('keeps the modal open with the error, and cancel leaves the model alone', async () => {
            setModels(llm('a'));
            client.deleteLLM.mockRejectedValue(new Error('in use by routing group'));
            await loadTab();
            click('delete A');
            click('confirm delete');
            expect(await screen.findByText('in use by routing group')).toBeTruthy();
            expect(toastMock.success).not.toHaveBeenCalled();

            click('cancel delete');
            expect(screen.queryByText('delete modal for A')).toBeNull();
            expect(names()).toEqual(['A']);
        });
    });

    describe('row actions', () => {
        beforeEach(() => setModels(llm('a', { mode: 'audio_speech' })));

        it('tests the connection with the model mode', async () => {
            await loadTab();
            click('test A');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('Connection successful! Latency: 12ms')
            );
            expect(client.testLLMConnection).toHaveBeenCalledWith('a', 'audio_speech');
            await waitFor(() => expect(client.listLLMs).toHaveBeenCalledTimes(2));
        });

        it('reports an unhealthy result and a test that throws', async () => {
            await loadTab();
            client.testLLMConnection.mockResolvedValueOnce({ status: 'unhealthy' });
            click('test A');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Connection failed: Unknown error', { duration: 5000 })
            );

            client.testLLMConnection.mockRejectedValueOnce(new Error('dns'));
            click('test A');
            await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('Connection test failed: dns'));
        });

        it.each(Object.keys(GATEWAY_MESSAGES) as GatewayMessageName[])(
            'says what to fix when the gateway refuses to dispatch the model (%s)',
            async (name) => {
                const raw = proxyReturned(GATEWAY_MESSAGES[name]);
                const said = explainDispatchRefusal(raw);
                expect(said).not.toBeNull();
                await loadTab();
                client.testLLMConnection.mockResolvedValueOnce({ status: 'unhealthy', error_message: raw });
                click('test A');
                await waitFor(() =>
                    expect(toastMock.error).toHaveBeenCalledWith(`Connection failed: ${said}`, { duration: 5000 })
                );
            },
        );

        it('says what to fix when the test itself is answered with the refusal', async () => {
            await loadTab();
            client.testLLMConnection.mockRejectedValueOnce(new Error(GATEWAY_MESSAGES.provider_credential_missing));
            click('test A');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith(
                    `Connection test failed: ${explainDispatchRefusal(GATEWAY_MESSAGES.provider_credential_missing)}`
                )
            );
        });

        it('toggles with an enabled-only payload and reloads', async () => {
            client.updateLLM.mockResolvedValue({});
            await loadTab();
            click('disable A');
            expect(await screen.findByText('Disabling model…')).toBeTruthy();
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('LLM Model "A" disabled successfully')
            );
            expect(client.updateLLM).toHaveBeenCalledWith('a', { enabled: false });
            await waitFor(() => expect(screen.queryByText('Disabling model…')).toBeNull());

            click('enable A');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('LLM Model "A" enabled successfully')
            );
            expect(client.updateLLM).toHaveBeenLastCalledWith('a', { enabled: true });
        });

        it('reports a failed toggle', async () => {
            client.updateLLM.mockRejectedValue(new Error('locked'));
            await loadTab();
            click('disable A');
            await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('Failed to update model: locked'));
            expect(toastMock.success).not.toHaveBeenCalled();
        });

        it('says in plain words when enabling is refused by the license limit or by the tenant quota', async () => {
            // What the control plane answers since enabling counts as one more model.
            client.updateLLM.mockRejectedValueOnce(
                await thrownByClient(403, {
                    detail: { error: 'licence_limit_reached', limit: 'max_llm_models', maximum: 5, current: 5, message: 'server words (max_llm_models)' },
                }),
            );
            await loadTab();
            click('enable A');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith(
                    'Failed to update model: The license allows 5 enabled models per tenant, and there are 5. Nothing was changed. Remove or disable one, or contact Brutor AI to raise the limit of your license.',
                ),
            );

            client.updateLLM.mockRejectedValueOnce(
                await thrownByClient(429, {
                    detail: { error: 'tenant_quota_reached', limit: 'max_llm_models', maximum: 3, current: 3, message: 'server words' },
                }),
            );
            click('enable A');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenLastCalledWith(
                    "Failed to update model: This tenant's quota allows 3 enabled models, and there are 3. Nothing was changed. Remove or disable one, or ask a system administrator to raise the tenant's quota.",
                ),
            );
            expect(toastMock.success).not.toHaveBeenCalled();
        });

        it('copies server side and names the result', async () => {
            client.copyLLM.mockResolvedValue({ model: { name: 'A (copy)' } });
            await loadTab();
            click('copy A');
            await waitFor(() =>
                expect(toastMock.success).toHaveBeenCalledWith('Model copied as "A (copy)"')
            );
            expect(client.copyLLM).toHaveBeenCalledWith('a');
            await waitFor(() => expect(client.listLLMs).toHaveBeenCalledTimes(2));
        });

        it('reports each shape of copy failure', async () => {
            await loadTab();
            client.copyLLM.mockRejectedValueOnce(
                await rejection([{ loc: ['body', 'name'], msg: 'dup', input: 'typed-name', type: 'value_error' }])
            );
            click('copy A');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to copy model: name: dup')
            );

            client.copyLLM.mockRejectedValueOnce(await rejection('Quota reached'));
            await waitFor(() => expect(screen.queryByText('Copying model…')).toBeNull());
            click('copy A');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to copy model: Quota reached')
            );

            client.copyLLM.mockRejectedValueOnce(new Error('down'));
            await waitFor(() => expect(screen.queryByText('Copying model…')).toBeNull());
            click('copy A');
            await waitFor(() => expect(toastMock.error).toHaveBeenCalledWith('Failed to copy model: down'));
        });
    });

    describe('catalog', () => {
        it('pages through the whole catalog and marks what is configured', async () => {
            setModels(llm('a'));
            const fullPage = Array.from({ length: 500 }, (_, i) => ({ id: `c${i}`, name: `c${i}` }));
            client.listLLMCatalog
                .mockResolvedValueOnce({ catalog: fullPage, total: 501 })
                .mockResolvedValueOnce({ catalog: [{ id: 'last', name: 'last' }], total: 501 });
            await loadTab();
            click('Browse');

            expect(await screen.findByText('501 models available — add one to configure it as an instance')).toBeTruthy();
            expect(client.listLLMCatalog.mock.calls).toEqual([
                [{ limit: 500, offset: 0 }],
                [{ limit: 500, offset: 500 }],
            ]);
            expect(screen.getByText('configured: openai:a-name')).toBeTruthy();
        });

        it('loads only once across open and close', async () => {
            client.listLLMCatalog.mockResolvedValue({ catalog: [{ id: 'c1', name: 'c1' }], total: 1 });
            await loadTab();
            click('Browse');
            await screen.findByRole('button', { name: 'tree import c1' });
            click('Close');
            click('Browse');
            await screen.findByRole('button', { name: 'tree import c1' });
            expect(client.listLLMCatalog).toHaveBeenCalledTimes(1);
        });

        it('shows a load failure in the modal, and retries', async () => {
            client.listLLMCatalog.mockRejectedValueOnce(new Error('catalog 500'));
            await loadTab();
            click('Browse');
            expect(await screen.findByText('Could not load the model catalog')).toBeTruthy();
            expect(screen.getByText('catalog 500')).toBeTruthy();
            expect(toastMock.error).toHaveBeenCalledWith('Failed to load model catalog: catalog 500');

            client.listLLMCatalog.mockResolvedValueOnce({ catalog: [{ id: 'c1', name: 'c1' }], total: 1 });
            click('Retry');
            expect(await screen.findByRole('button', { name: 'tree import c1' })).toBeTruthy();
            expect(screen.queryByText('Could not load the model catalog')).toBeNull();
        });

        const DEFAULT_GROUP_NOTE = "Available to the Default group. To restrict who can use it, edit the model's Access Control tab";

        it('imports from the tree, then offers to edit the new model', async () => {
            client.listLLMCatalog.mockResolvedValue({ catalog: [{ id: 'c1', name: 'c1' }], total: 1 });
            client.importLLMCatalogModel.mockResolvedValue({ id: 'imported', name: 'Imported' });
            await loadTab();
            click('Browse');
            setModels(llm('imported', { name: 'Imported' }));
            fireEvent.click(await screen.findByRole('button', { name: 'tree import c1' }));

            expect(await screen.findByText('Model Ready')).toBeTruthy();
            expect(client.importLLMCatalogModel).toHaveBeenCalledWith('c1');
            expect(toastMock.success).toHaveBeenCalledWith('Model added');
            // An imported model is bound to the Default group: nothing to assign.
            const ready = screen.getByTestId('model-added-notice');
            expect(ready).toHaveTextContent(DEFAULT_GROUP_NOTE);
            expect(ready).not.toHaveTextContent('Assign to');
            await waitFor(() => expect(names()).toEqual(['Imported']));

            click('Edit Model');
            expect(screen.getByText('form for Imported')).toBeTruthy();
            expect(screen.queryByText('Browse Model Catalog')).toBeNull();
            expect(screen.queryByText('Model Ready')).toBeNull();
        });

        it('says before the import that a model on Amazon Bedrock or Vertex AI is added disabled', async () => {
            client.listLLMCatalog.mockResolvedValue({ catalog: [{ id: 'c1', name: 'c1' }], total: 1 });
            await loadTab();
            click('Browse');
            expect(await screen.findByTestId('catalog-cloud-note')).toHaveTextContent(
                'A model on Amazon Bedrock or Vertex AI is added disabled. It has no credential yet: edit it, enter its credential, then enable it.',
            );
        });

        it.each([
            ['bedrock', 'Amazon Bedrock', 'AWS access key ID, secret access key and AWS region'],
            ['vertex_ai', 'Vertex AI', 'service account JSON key, GCP project ID and GCP location'],
        ])('says a %s model was added disabled, and what it needs before it can be enabled', async (provider, cloud, needs) => {
            client.listLLMCatalog.mockResolvedValue({ catalog: [{ id: 'c1', name: 'c1' }], total: 1 });
            client.importLLMCatalogModel.mockResolvedValue({ id: 'imported', name: 'Imported', enabled: false, provider });
            await loadTab();
            click('Browse');
            setModels(llm('imported', { name: 'Imported', enabled: false, provider }));
            fireEvent.click(await screen.findByRole('button', { name: 'tree import c1' }));

            const notice = await screen.findByTestId('model-added-notice');
            expect(notice).toHaveTextContent('Model added, disabled');
            expect(notice).toHaveTextContent(`Imported was added disabled: a model on ${cloud} has no credential until you enter one.`);
            expect(notice).toHaveTextContent(`Enter the ${needs} in the API tab`);
            expect(notice).toHaveTextContent('Enable the model');
            expect(notice).toHaveTextContent(DEFAULT_GROUP_NOTE);
            expect(notice).not.toHaveTextContent('Assign to');
            expect(notice).not.toHaveTextContent('API Key');
            expect(screen.queryByText('Model Ready')).toBeNull();
            expect(toastMock.success).toHaveBeenCalledWith('Model added, disabled');
            // It is in the list, where it can be edited.
            await waitFor(() => expect(names()).toEqual(['Imported']));
        });

        it('imports from search, closes it, and dismisses the notice with OK', async () => {
            client.importLLMCatalogModel.mockResolvedValue({ id: 'n', name: 'N' });
            await loadTab();
            click('Search');
            click('search import');
            expect(await screen.findByText('Model Ready')).toBeTruthy();
            expect(client.importLLMCatalogModel).toHaveBeenCalledWith('cat-9');
            expect(screen.queryByText('search modal')).toBeNull();

            click('OK');
            expect(screen.queryByText('Model Ready')).toBeNull();
        });

        it('reports a failed import and rethrows so the caller can reset', async () => {
            client.importLLMCatalogModel.mockRejectedValue(new Error('already exists'));
            await loadTab();
            click('Search');
            click('search import');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith('Failed to add model: already exists')
            );
            expect(screen.getByText('search modal')).toBeTruthy();
            expect(screen.queryByText('Model Ready')).toBeNull();
        });

        it('says in plain words when the control plane refuses an entry the gateway cannot serve', async () => {
            client.importLLMCatalogModel.mockRejectedValue(
                await thrownByClient(422, {
                    detail:
                        "unsupported_provider_api: the gateway does not implement the Amazon Bedrock API for model 'ai21.j2-ultra' (provider 'bedrock'), so it would refuse every request for it. On Bedrock it serves Anthropic Claude models and the Amazon Nova, Mistral and DeepSeek chat models as chat models, and the Amazon Titan Text and Cohere Embed v3 embedding models as embedding models.",
                }),
            );
            await loadTab();
            click('Search');
            click('search import');
            await waitFor(() =>
                expect(toastMock.error).toHaveBeenCalledWith(
                    'Failed to add model: The gateway cannot call this model on Amazon Bedrock. There it serves Anthropic Claude, Amazon Nova, Mistral and DeepSeek chat models, and Amazon Titan Text and Cohere Embed v3 embedding models. Nothing was saved. Choose one of those models.',
                ),
            );
        });

        describe('an entry that needs an API URL', () => {
            const AZURE = { id: 'c1', name: 'c1', display_name: 'Azure GPT', provider_key: 'azure', model_name: 'gpt-4o', mode: 'chat', requires_api_url: true };
            // The control plane's refusal, as it words it.
            const NO_DESTINATION =
                "api_url is required: model 'gpt-4o' is of provider 'azure', for which the gateway knows no base URL for a chat " +
                'model, so it would refuse every request for it (provider_api_url_missing). Set api_url to the address of your own endpoint.';

            async function startImport(entry: Record<string, unknown> = AZURE) {
                client.listLLMCatalog.mockResolvedValue({ catalog: [entry], total: 1 });
                await loadTab();
                click('Browse');
                fireEvent.click(await screen.findByRole('button', { name: 'tree import c1' }));
            }

            it('is asked for before anything is sent, and sent with the import', async () => {
                client.importLLMCatalogModel.mockResolvedValue({ id: 'imported', name: 'Azure GPT' });
                await startImport();

                const url = await screen.findByLabelText(/^API URL/);
                expect(screen.getByText(/Azure GPT needs an API URL/)).toBeTruthy();
                expect(screen.getByText(/the gateway knows no address for a chat model of provider "azure"/)).toBeTruthy();
                expect(client.importLLMCatalogModel).not.toHaveBeenCalled();

                // Nothing typed: nothing sent.
                click('Add model');
                expect(await screen.findByText('API URL is required.')).toBeTruthy();
                expect(client.importLLMCatalogModel).not.toHaveBeenCalled();

                fireEvent.change(url, { target: { value: ' https://unit.openai.azure.com ' } });
                click('Add model');
                expect(await screen.findByText('Model Ready')).toBeTruthy();
                expect(client.importLLMCatalogModel).toHaveBeenCalledTimes(1);
                expect(client.importLLMCatalogModel).toHaveBeenCalledWith('c1', { api_url: 'https://unit.openai.azure.com' });
                expect(screen.queryByLabelText(/^API URL/)).toBeNull();
                expect(toastMock.success).toHaveBeenCalledWith('Model added');
            });

            it('is not asked for of an entry that needs none', async () => {
                client.importLLMCatalogModel.mockResolvedValue({ id: 'imported', name: 'Claude' });
                await startImport({ ...AZURE, provider_key: 'anthropic', requires_api_url: false });
                expect(await screen.findByText('Model Ready')).toBeTruthy();
                expect(client.importLLMCatalogModel).toHaveBeenCalledWith('c1');
                expect(screen.queryByLabelText(/^API URL/)).toBeNull();
            });

            it('imports nothing when the question is cancelled', async () => {
                await startImport();
                await screen.findByLabelText(/^API URL/);
                click('Cancel');
                await waitFor(() => expect(screen.queryByLabelText(/^API URL/)).toBeNull());
                expect(client.importLLMCatalogModel).not.toHaveBeenCalled();
                expect(screen.queryByText('Model Ready')).toBeNull();
                expect(toastMock.error).not.toHaveBeenCalled();
            });

            it('shows the control plane\'s refusal by the field and stays open', async () => {
                client.importLLMCatalogModel.mockRejectedValueOnce(await thrownByClient(422, { detail: NO_DESTINATION }));
                await startImport();
                const url = await screen.findByLabelText(/^API URL/);
                fireEvent.change(url, { target: { value: 'https://unit.openai.azure.com' } });
                click('Add model');
                await waitFor(() => expect(url.parentElement?.textContent).toContain(NO_DESTINATION));
                expect(screen.queryByText('Model Ready')).toBeNull();

                client.importLLMCatalogModel.mockResolvedValue({ id: 'imported', name: 'Azure GPT' });
                click('Add model');
                expect(await screen.findByText('Model Ready')).toBeTruthy();
            });

            it('shows any other failure in the question too', async () => {
                client.importLLMCatalogModel.mockRejectedValueOnce(new Error('already exists'));
                await startImport();
                fireEvent.change(await screen.findByLabelText(/^API URL/), { target: { value: 'https://unit.openai.azure.com' } });
                click('Add model');
                expect(await screen.findByText('Failed to add model: already exists')).toBeTruthy();
                expect(screen.getByLabelText(/^API URL/)).toBeTruthy();
            });

            it('is asked for when the control plane refuses an entry the list did not mark', async () => {
                client.importLLMCatalogModel.mockRejectedValueOnce(await thrownByClient(422, { detail: NO_DESTINATION }));
                await startImport({ ...AZURE, requires_api_url: false });
                const url = await screen.findByLabelText(/^API URL/);
                expect(url.parentElement?.textContent).toContain(NO_DESTINATION);
                expect(client.importLLMCatalogModel).toHaveBeenCalledWith('c1');

                client.importLLMCatalogModel.mockResolvedValue({ id: 'imported', name: 'Azure GPT' });
                fireEvent.change(url, { target: { value: 'https://unit.openai.azure.com' } });
                click('Add model');
                expect(await screen.findByText('Model Ready')).toBeTruthy();
                expect(client.importLLMCatalogModel).toHaveBeenLastCalledWith('c1', { api_url: 'https://unit.openai.azure.com' });
            });
        });

        it('with nothing contributed, offers Browse and Search only', async () => {
            render(<LLModelTab />);
            await screen.findByText('Enabled Models');
            expect(screen.queryByRole('button', { name: 'HuggingFace' })).toBeNull();
            const bar = screen.getByRole('button', { name: 'Search' }).parentElement!.parentElement!.parentElement!;
            expect(Array.from(bar.querySelectorAll('button')).map(b => b.textContent)).toEqual(['Browse', 'Search']);
            clickAddNew();
            expect(screen.getByText('form for a new model')).toBeTruthy();
        });
    });

    describe('a contributed import source', () => {
        /** A source: a button in the action bar and a dialog that picks a model named after the source. */
        const fakeSource = (id: string): ModelImportSlotItem => ({
            id,
            Button: ({ onOpen }) => <button onClick={onOpen}>{`find at ${id}`}</button>,
            Dialog: ({ isOpen, onClose, onCreateModel }) => (
                <div data-testid={`dialog-${id}`} data-open={String(isOpen)}>
                    {isOpen && (
                        <>
                            <button onClick={() => onCreateModel({ name: `Picked at ${id}` })}>{`pick at ${id}`}</button>
                            <button onClick={onClose}>{`close ${id}`}</button>
                        </>
                    )}
                </div>
            ),
        });
        const isOpen = (id: string) => screen.getByTestId(`dialog-${id}`).getAttribute('data-open');

        async function loadWithSources(...ids: string[]) {
            render(
                <SlotProvider slots={{ 'llm-models.import': ids.map(fakeSource) }}>
                    <LLModelTab />
                </SlotProvider>,
            );
            await screen.findByText('Enabled Models');
        }

        it('places the buttons in order after Browse and Search, and keeps every dialog mounted, closed', async () => {
            await loadWithSources('one', 'two');
            const bar = screen.getByRole('button', { name: 'find at one' }).parentElement!;
            expect(Array.from(bar.querySelectorAll('button')).map(b => b.textContent)).toEqual([
                'Browse', 'Search', 'find at one', 'find at two',
            ]);
            expect(isOpen('one')).toBe('false');
            expect(isOpen('two')).toBe('false');
        });

        it('opens only the dialog whose button was pressed, and closes it when the dialog asks', async () => {
            await loadWithSources('one', 'two');
            click('find at two');
            expect(isOpen('two')).toBe('true');
            expect(isOpen('one')).toBe('false');
            click('close two');
            expect(isOpen('two')).toBe('false');
            // Closed without a pick: no create form.
            expect(screen.queryByText(/^form for/)).toBeNull();
        });

        it('turns a picked model into a prefilled create form, and forgets the pick when the form closes', async () => {
            await loadWithSources('one');
            click('find at one');
            click('pick at one');
            expect(screen.getByText('form for Picked at one')).toBeTruthy();
            expect(isOpen('one')).toBe('false');
            click('form close');
            expect(screen.queryByText(/^form for/)).toBeNull();
            clickAddNew();
            expect(screen.getByText('form for a new model')).toBeTruthy();
        });

        it('pauses health checks while a dialog is open and through the create form it leads to', async () => {
            await loadWithSources('one');
            expect(healthOptions.paused).toBe(false);
            click('find at one');
            expect(healthOptions.paused).toBe(true);
            // Closing the dialog without a pick resumes them.
            click('close one');
            expect(healthOptions.paused).toBe(false);
            click('find at one');
            expect(healthOptions.paused).toBe(true);
            click('pick at one');
            // The create form the pick leads to keeps them paused; closing it resumes.
            expect(healthOptions.paused).toBe(true);
            click('form close');
            expect(healthOptions.paused).toBe(false);
        });
    });

    describe('health check bar', () => {
        it('uses the configured interval and shows when checking is paused or off', async () => {
            health.healthCheckConfig = { ai_settings: { interval_seconds: 300 } };
            const first = render(<LLModelTab />);
            expect(await screen.findByText(/Health check every 300s/)).toBeTruthy();
            first.unmount();

            health.isPaused = true;
            const second = render(<LLModelTab />);
            expect(await screen.findByText('Health check paused')).toBeTruthy();
            second.unmount();

            health.isPaused = false;
            health.isEnabled = false;
            render(<LLModelTab />);
            expect(await screen.findByText('Health check disabled')).toBeTruthy();
        });

        it('runs a check on demand and reloads', async () => {
            await loadTab();
            click('Check Now');
            await waitFor(() => expect(health.runHealthCheck).toHaveBeenCalledTimes(1));
            await waitFor(() => expect(client.listLLMs).toHaveBeenCalledTimes(2));
        });

        it('disables the button while a check runs', async () => {
            health.isChecking = true;
            await loadTab();
            expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Checking...' }).disabled).toBe(true);
        });

        it('reloads after an automatic check completes', async () => {
            health.lastCheckTime = new Date('2026-11-01T10:00:00Z');
            await loadTab();
            await waitFor(() => expect(client.listLLMs).toHaveBeenCalledTimes(2));
            expect(screen.getByText(/Last check:/)).toBeTruthy();
        });

        // The gateway does not act on health-check settings in this release: none are offered.
        it('offers no health-check settings', async () => {
            await loadTab();
            expect(screen.queryByTitle('Configure health check settings')).toBeNull();
        });

        it('opens the help and proxy modals', async () => {
            await loadTab();
            click('How it works');
            click('health help close');
            expect(screen.queryByRole('button', { name: 'health help close' })).toBeNull();
            click('Proxy API Access');
            click('proxy modal http://proxy.test');
            expect(screen.queryByRole('button', { name: 'proxy modal http://proxy.test' })).toBeNull();
        });
    });
});

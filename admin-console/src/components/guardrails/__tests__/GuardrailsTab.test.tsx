/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Guardrails list.
 *
 * The defect worth pinning is the quick toggle. The update endpoint treats
 * `builtin` as a full replacement (`_set_if_changed` writes whatever the body
 * parsed to, including Pydantic's defaults), so a toggle that sends a partial
 * `builtin` does not just flip `enabled`: it switches secrets detection off,
 * resets the detector providers and wipes per-check surfaces, with a success
 * toast. The toggle must round-trip everything the config has.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const gateway = {
    listGuardrailConfigs: vi.fn(),
    updateGuardrailConfigById: vi.fn(),
    deleteGuardrailConfig: vi.fn(),
    fetchGuardrailProviders: vi.fn(),
};
const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() };

vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));
vi.mock('../BreakGlassPanel', () => ({ default: () => <div>break-glass panel</div> }));
vi.mock('../GuardrailTestPanel', () => ({
    default: ({ initialConfigId, onClose }: { initialConfigId?: string; onClose: () => void }) => (
        <div>
            test panel for {initialConfigId ?? 'all'}
            <button onClick={onClose}>close test panel</button>
        </div>
    ),
}));
vi.mock('../GuardrailForm', () => ({
    default: ({
        config,
        onSave,
        onClose,
    }: {
        config: { name: string } | null;
        onSave: () => void;
        onClose: () => void;
    }) => (
        <div>
            form for {config?.name ?? 'a new config'}
            <button onClick={onSave}>form save</button>
            <button onClick={onClose}>form close</button>
        </div>
    ),
}));

import GuardrailsTab from '../GuardrailsTab';
import type { GuardrailConfigResponse } from '../../../lib/guardrailTypes';

const SURFACES = { chat_input: true, mcp_input: true, skill_output: true, a2a_inbound: true, a2a_outbound: true };

/** A config with a non-default value in every field the toggle has to preserve. */
const STRICT = {
    id: 'gr-1',
    tenant_id: 't-1',
    name: 'Strict',
    description: 'Blocks everything risky',
    is_default: true,
    priority: 10,
    streaming_output_mode: 'sync',
    group_ids: [],
    groups: [],
    enabled: true,
    surfaces: SURFACES,
    builtin: {
        pii: {
            enabled: true,
            action: 'block',
            provider: 'presidio',
            categories: ['email'],
            threshold: 0.6,
            surfaces: ['chat_input'],
            per_surface: { mcp_input: { action: 'redact' } },
        },
        prompt_injection: {
            enabled: true,
            action: 'block',
            sensitivity: 0.9,
            provider: 'lakera_guard',
            surfaces: ['chat_input'],
            per_surface: { mcp_input: { action: 'warn' }, skill_input: { action: 'warn' } },
        },
        jailbreak: {
            enabled: true,
            action: 'warn',
            provider: 'lakera_guard',
            surfaces: ['chat_input'],
            per_surface: null,
        },
        toxic_content: {
            enabled: true,
            action: 'warn',
            threshold: 0.4,
            provider: 'openai_moderation',
            category_thresholds: { hate: 0.2 },
            surfaces: ['chat_output'],
            per_surface: null,
        },
        banned_words: ['secret'],
        banned_patterns: ['\\d{4}', 'x+'],
        banned_words_match_mode: 'word',
        banned_words_surfaces: ['chat_input'],
        banned_patterns_surfaces: ['mcp_input'],
        banned_words_per_surface: { mcp_input: { words: ['a'] } },
        banned_patterns_per_surface: { mcp_input: { patterns: ['a'] }, skill_input: { patterns: ['b'] } },
        secrets: {
            enabled: true,
            action: 'block',
            categories: ['jwt'],
            surfaces: ['mcp_input'],
            threshold: 0.3,
            per_surface: { chat_input: { action: 'warn' } },
        },
    },
    providers: [
        {
            provider: 'lakera_guard',
            enabled: true,
            endpoint: 'https://lakera.test',
            config: { project: 'p' },
        },
        { provider: 'presidio', enabled: false },
    ],
    max_check_timeout_ms: 750,
    fail_open: true,
    fail_open_overrides: { pii: false },
    log_all_checks: true,
} as unknown as GuardrailConfigResponse;

const BARE = {
    ...STRICT,
    id: 'gr-2',
    name: 'Bare',
    description: undefined,
    is_default: false,
    enabled: false,
    surfaces: {},
    builtin: {
        pii: { enabled: false, action: 'allow', surfaces: [] },
        prompt_injection: { enabled: false, action: 'allow', surfaces: [] },
        jailbreak: { enabled: false, action: 'allow', surfaces: [] },
        toxic_content: { enabled: false, action: 'allow', surfaces: [] },
        banned_words: [],
        banned_patterns: [],
        banned_words_surfaces: [],
        banned_patterns_surfaces: [],
    },
    providers: [],
} as unknown as GuardrailConfigResponse;

const row = (name: string) => screen.getByText(name).closest('tr') as HTMLElement;

async function renderLoaded(configs: GuardrailConfigResponse[] = [STRICT, BARE]) {
    gateway.listGuardrailConfigs.mockResolvedValue({ configs, total: configs.length });
    render(<GuardrailsTab />);
    await screen.findByRole('heading', { name: 'Guardrails' });
}

describe('GuardrailsTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        gateway.fetchGuardrailProviders.mockResolvedValue({ providers: [] });
        gateway.updateGuardrailConfigById.mockResolvedValue({});
        gateway.deleteGuardrailConfig.mockResolvedValue({});
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    describe('quick toggle', () => {
        const sentBody = () => gateway.updateGuardrailConfigById.mock.calls[0][1] as Record<string, unknown>;

        it('flips only enabled, and sends the config it was given', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Disable'));

            await waitFor(() => expect(gateway.updateGuardrailConfigById).toHaveBeenCalledTimes(1));
            expect(gateway.updateGuardrailConfigById.mock.calls[0][0]).toBe('gr-1');
            expect(sentBody()).toMatchObject({
                enabled: false,
                surfaces: SURFACES,
                max_check_timeout_ms: 750,
                fail_open: true,
                log_all_checks: true,
            });
        });

        it('keeps secrets detection on: the endpoint would otherwise default it to off', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Disable'));

            await waitFor(() => expect(gateway.updateGuardrailConfigById).toHaveBeenCalledTimes(1));
            expect(sentBody().builtin).toMatchObject({
                secrets_enabled: true,
                secrets_action: 'block',
                secrets_categories: ['jwt'],
                secrets_surfaces: ['mcp_input'],
                secrets_threshold: 0.3,
            });
        });

        it('keeps every check\'s provider, action and tuning', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Disable'));

            await waitFor(() => expect(gateway.updateGuardrailConfigById).toHaveBeenCalledTimes(1));
            expect(sentBody().builtin).toMatchObject({
                pii_enabled: true,
                pii_action: 'block',
                pii_provider: 'presidio',
                pii_categories: ['email'],
                pii_threshold: 0.6,
                prompt_injection_enabled: true,
                prompt_injection_action: 'block',
                prompt_injection_sensitivity: 0.9,
                prompt_injection_provider: 'lakera_guard',
                jailbreak_enabled: true,
                jailbreak_action: 'warn',
                jailbreak_provider: 'lakera_guard',
                toxic_content_enabled: true,
                toxic_content_action: 'warn',
                toxic_content_threshold: 0.4,
                toxic_content_provider: 'openai_moderation',
                toxic_content_category_thresholds: { hate: 0.2 },
                banned_words: ['secret'],
                banned_patterns: ['\\d{4}', 'x+'],
                banned_words_match_mode: 'word',
            });
        });

        it('keeps per-check surfaces and per-surface overrides', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Disable'));

            await waitFor(() => expect(gateway.updateGuardrailConfigById).toHaveBeenCalledTimes(1));
            expect(sentBody().builtin).toMatchObject({
                pii_surfaces: ['chat_input'],
                prompt_injection_surfaces: ['chat_input'],
                jailbreak_surfaces: ['chat_input'],
                toxic_content_surfaces: ['chat_output'],
                banned_words_surfaces: ['chat_input'],
                banned_patterns_surfaces: ['mcp_input'],
                pii_per_surface: { mcp_input: { action: 'redact' } },
                prompt_injection_per_surface: { mcp_input: { action: 'warn' }, skill_input: { action: 'warn' } },
                banned_words_per_surface: { mcp_input: { words: ['a'] } },
                banned_patterns_per_surface: { mcp_input: { patterns: ['a'] }, skill_input: { patterns: ['b'] } },
                secrets_per_surface: { chat_input: { action: 'warn' } },
            });
        });

        it('keeps providers and per-check fail-open overrides', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Disable'));

            await waitFor(() => expect(gateway.updateGuardrailConfigById).toHaveBeenCalledTimes(1));
            expect(sentBody().fail_open_overrides).toEqual({ pii: false });
            // A provider as the contract names it.
            expect(sentBody().providers).toEqual([
                { provider: 'lakera_guard', enabled: true, endpoint: 'https://lakera.test', config: { project: 'p' } },
                { provider: 'presidio', enabled: false, endpoint: undefined, config: undefined },
            ]);
        });

        it('shows the new state and confirms, in both directions', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Disable'));
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith('Guardrails Disabled', '"Strict" has been disabled.'),
            );
            expect(within(row('Strict')).getByText('Inactive')).toBeInTheDocument();
            expect(screen.getByText('2 configurations, 0 active')).toBeInTheDocument();

            fireEvent.click(within(row('Bare')).getByTitle('Enable'));
            await waitFor(() =>
                expect(toast.success).toHaveBeenCalledWith('Guardrails Enabled', '"Bare" is now active.'),
            );
            expect(within(row('Bare')).getByText('Active')).toBeInTheDocument();
        });

        it('leaves the row alone and reports when the update fails', async () => {
            gateway.updateGuardrailConfigById.mockRejectedValue(new Error('forbidden'));
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Disable'));

            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Toggle Failed', 'forbidden'));
            expect(within(row('Strict')).getByText('Active')).toBeInTheDocument();
            expect(within(row('Strict')).getByTitle('Disable')).toBeEnabled();
        });
    });

    describe('delete', () => {
        const openDelete = () => fireEvent.click(within(row('Strict')).getByTitle('Delete configuration'));

        it('demands the name, deletes by id, and removes the row', async () => {
            await renderLoaded();
            openDelete();

            const confirm = screen.getByRole('button', { name: 'Delete Guardrail Config' });
            expect(confirm).toBeDisabled();
            fireEvent.change(screen.getByPlaceholderText('Enter "Strict"'), { target: { value: 'wrong' } });
            expect(confirm).toBeDisabled();

            fireEvent.change(screen.getByPlaceholderText('Enter "Strict"'), { target: { value: 'Strict' } });
            fireEvent.click(confirm);

            await waitFor(() => expect(gateway.deleteGuardrailConfig).toHaveBeenCalledWith('gr-1'));
            await waitFor(() => expect(screen.queryByText('Strict')).not.toBeInTheDocument());
            expect(toast.success).toHaveBeenCalledWith('Config Deleted', '"Strict" has been deleted.');
        });

        it('keeps the row and shows why when the delete is refused', async () => {
            gateway.deleteGuardrailConfig.mockRejectedValue(new Error('still bound to 3 groups'));
            await renderLoaded();
            openDelete();
            fireEvent.change(screen.getByPlaceholderText('Enter "Strict"'), { target: { value: 'Strict' } });
            fireEvent.click(screen.getByRole('button', { name: 'Delete Guardrail Config' }));

            expect(await screen.findByText('still bound to 3 groups')).toBeInTheDocument();
            expect(screen.getByText('Delete failed')).toBeInTheDocument();
            expect(toast.error).toHaveBeenCalledWith('Delete Failed', 'still bound to 3 groups');
            expect(screen.getAllByText('Strict').some((el) => el.closest('tr'))).toBe(true);
        });

        it('deletes nothing when cancelled', async () => {
            await renderLoaded();
            openDelete();

            fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

            expect(screen.queryByRole('button', { name: 'Delete Guardrail Config' })).not.toBeInTheDocument();
            expect(gateway.deleteGuardrailConfig).not.toHaveBeenCalled();
        });
    });

    describe('loading', () => {
        it('summarises what each config checks and where', async () => {
            await renderLoaded();

            const strict = within(row('Strict'));
            expect(strict.getByText('Default')).toBeInTheDocument();
            expect(strict.getByText('Blocks everything risky')).toBeInTheDocument();
            expect(strict.getByText('PII → Block (+1 surface)')).toBeInTheDocument();
            expect(strict.getByText('Injection → Block (+2 surfaces)')).toBeInTheDocument();
            expect(strict.getByText('Jailbreak → Warn')).toBeInTheDocument();
            expect(strict.getByText('Toxic → Warn')).toBeInTheDocument();
            expect(strict.getByText('Secrets → Block (+1 surface)')).toBeInTheDocument();
            expect(strict.getByText('1 word (+1 surface)')).toBeInTheDocument();
            expect(strict.getByText('2 patterns (+2 surfaces)')).toBeInTheDocument();
            expect(strict.getByText('1 provider')).toBeInTheDocument();
            for (const s of ['LLM', 'MCP', 'Skill', 'A2A in', 'A2A out']) {
                expect(strict.getByText(s)).toBeInTheDocument();
            }

            const bare = within(row('Bare'));
            expect(bare.getAllByText('None')).toHaveLength(2);
            expect(screen.getByText('2 configurations, 1 active')).toBeInTheDocument();
        });

        it('counts per-surface-only banned lists as active configuration', async () => {
            const overridesOnly = {
                ...BARE,
                builtin: {
                    ...BARE.builtin,
                    banned_words_per_surface: { mcp_input: { words: ['a'] } },
                    banned_patterns_per_surface: { mcp_input: { patterns: ['b'] } },
                },
            } as unknown as GuardrailConfigResponse;
            await renderLoaded([overridesOnly]);

            const bare = within(row('Bare'));
            expect(bare.getByText('words (+1 surface)')).toBeInTheDocument();
            expect(bare.getByText('patterns (+1 surface)')).toBeInTheDocument();
            expect(screen.getByText('1 configuration, 0 active')).toBeInTheDocument();
        });

        it('shows the error and retries when the first load fails', async () => {
            gateway.listGuardrailConfigs.mockRejectedValueOnce(new Error('db down'));
            render(<GuardrailsTab />);

            expect(await screen.findByText('Failed to Load Configurations')).toBeInTheDocument();
            expect(screen.getByText('db down')).toBeInTheDocument();
            expect(toast.error).toHaveBeenCalledWith('Failed to Load', 'db down');

            gateway.listGuardrailConfigs.mockResolvedValue({ configs: [STRICT], total: 1 });
            fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

            expect(await screen.findByText('Strict')).toBeInTheDocument();
        });

        it('fails the load when only the providers cannot be read', async () => {
            gateway.listGuardrailConfigs.mockResolvedValue({ configs: [STRICT], total: 1 });
            gateway.fetchGuardrailProviders.mockRejectedValue(new Error('providers down'));
            render(<GuardrailsTab />);

            expect(await screen.findByText('providers down')).toBeInTheDocument();
        });

        it('keeps the list on screen when a refresh fails', async () => {
            await renderLoaded();

            gateway.listGuardrailConfigs.mockRejectedValue(new Error('blip'));
            fireEvent.click(screen.getByTitle('Refresh'));

            await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Failed to Load', 'blip'));
            expect(await screen.findByText('Strict')).toBeInTheDocument();
        });

        it('invites creating the first config when there are none', async () => {
            await renderLoaded([]);

            expect(screen.getByText('No Guardrail Configurations')).toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', { name: 'Create First Config' }));
            expect(screen.getByText('form for a new config')).toBeInTheDocument();
        });

        it('puts break-glass state above the list', async () => {
            await renderLoaded();
            const panel = screen.getByText('break-glass panel');
            const heading = screen.getByRole('heading', { name: 'Guardrails' });

            expect(panel.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        });
    });

    describe('search', () => {
        const many = ['Alpha', 'Beta', 'Gamma', 'Delta'].map(
            (name, i) => ({ ...BARE, id: `gr-${i}`, name, description: `about ${name}x` }) as GuardrailConfigResponse,
        );

        it('is offered only once the list is long enough', async () => {
            await renderLoaded(many.slice(0, 3));
            expect(screen.queryByPlaceholderText(/Search guardrail/)).not.toBeInTheDocument();
        });

        it('matches name or description, and says when nothing matches', async () => {
            await renderLoaded(many);
            const box = screen.getByPlaceholderText(/Search guardrail/);

            fireEvent.change(box, { target: { value: 'BETA' } });
            expect(screen.getByText('Beta')).toBeInTheDocument();
            expect(screen.queryByText('Alpha')).not.toBeInTheDocument();

            fireEvent.change(box, { target: { value: 'gammax' } });
            expect(screen.getByText('Gamma')).toBeInTheDocument();
            expect(screen.queryByText('Beta')).not.toBeInTheDocument();

            fireEvent.change(box, { target: { value: 'nothing-like-this' } });
            expect(screen.getByText('No matches')).toBeInTheDocument();
            expect(screen.queryByText('No Guardrail Configurations')).not.toBeInTheDocument();

            fireEvent.click(within(box.parentElement as HTMLElement).getByRole('button'));
            expect(screen.getByText('Alpha')).toBeInTheDocument();
        });
    });

    describe('forms and the test panel', () => {
        it('opens the form for a new config and reloads after a save', async () => {
            await renderLoaded();
            expect(gateway.listGuardrailConfigs).toHaveBeenCalledTimes(1);

            fireEvent.click(screen.getByRole('button', { name: 'New Config' }));
            fireEvent.click(screen.getByRole('button', { name: 'form save' }));

            await waitFor(() => expect(gateway.listGuardrailConfigs).toHaveBeenCalledTimes(2));
            await screen.findByRole('heading', { name: 'Guardrails' });
            expect(screen.queryByText('form for a new config')).not.toBeInTheDocument();
        });

        it('opens the chosen config for editing, and closes without reloading', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Bare')).getByTitle('Edit configuration'));
            expect(screen.getByText('form for Bare')).toBeInTheDocument();

            fireEvent.click(screen.getByRole('button', { name: 'form close' }));
            expect(screen.queryByText('form for Bare')).not.toBeInTheDocument();
            expect(gateway.listGuardrailConfigs).toHaveBeenCalledTimes(1);
        });

        it('tests a specific config, or any content', async () => {
            await renderLoaded();

            fireEvent.click(within(row('Strict')).getByTitle('Test this configuration'));
            expect(screen.getByText('test panel for gr-1')).toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', { name: 'close test panel' }));

            fireEvent.click(screen.getByRole('button', { name: 'Test Content' }));
            expect(screen.getByText('test panel for all')).toBeInTheDocument();
        });
    });
});

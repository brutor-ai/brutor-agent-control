/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Guardrail test dialog.
 *
 * Pinned: the exact test request (trimmed content, chosen surface, direction and
 * config), the direction picker being live only for surfaces that need it, and the
 * verdict banner matching the server's answer. A dialog that reads "Content Passed"
 * when zero configs ran would be false assurance, so that case says so instead.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { getActionConfig } from '../../../lib/guardrailTypes';

const gateway = { testGuardrails: vi.fn(), listGuardrailConfigs: vi.fn() };
const toast = { success: vi.fn(), error: vi.fn() };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../common/Toast', () => ({ useToast: () => toast }));

import GuardrailTestPanel from '../GuardrailTestPanel';

const onClose = vi.fn();
const CONFIGS = [
    { id: 'c1', name: 'Strict', is_default: true, enabled: true },
    { id: 'c2', name: 'Off one', is_default: false, enabled: false },
];
const PASSED = { blocked: false, action: 'allow', violations: [], duration_ms: 12.34, configs_evaluated: 2 };

const typeContent = (v: string) =>
    fireEvent.change(screen.getByPlaceholderText('Enter or paste content to check against guardrails...'), {
        target: { value: v },
    });
const run = () => fireEvent.click(screen.getByRole('button', { name: 'Run Guardrail Test' }));
const open = async (initialConfigId?: string) => {
    render(<GuardrailTestPanel onClose={onClose} initialConfigId={initialConfigId} />);
    await waitFor(() => expect(screen.queryByText('Loading configurations...')).toBeNull());
};
/** The selects in order: configuration, surface, direction. */
const selects = () => screen.getAllByRole<HTMLSelectElement>('combobox');

describe('GuardrailTestPanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        gateway.testGuardrails.mockReset();
        gateway.listGuardrailConfigs.mockReset();
        gateway.listGuardrailConfigs.mockResolvedValue({ configs: CONFIGS });
        gateway.testGuardrails.mockResolvedValue(PASSED);
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('sends trimmed content on the default surface, with no config when auto-resolving', async () => {
        await open();
        expect(screen.getByRole('button', { name: 'Run Guardrail Test' })).toBeDisabled();
        typeContent('  hello  ');
        run();
        await waitFor(() =>
            expect(gateway.testGuardrails).toHaveBeenCalledWith({
                content: 'hello',
                surface: 'chat_input',
                direction: 'input',
                config_id: undefined,
            }),
        );
        expect(screen.getByText(/Auto-resolve only tests tenant defaults/)).toBeInTheDocument();
    });

    it('labels default and disabled configs, and starts on the requested one', async () => {
        await open('c2');
        expect(screen.getByRole('option', { name: 'Strict ★ Default' })).toBeInTheDocument();
        expect(screen.getByRole('option', { name: 'Off one (disabled)' })).toBeInTheDocument();
        typeContent('x');
        run();
        await waitFor(() => expect(gateway.testGuardrails).toHaveBeenCalled());
        expect(gateway.testGuardrails.mock.calls[0][0]).toMatchObject({ config_id: 'c2' });
    });

    it('auto-selects the only config there is', async () => {
        gateway.listGuardrailConfigs.mockResolvedValue({ configs: [CONFIGS[0]] });
        await open();
        expect(selects()[0].value).toBe('c1');
    });

    it('says when there are no configs', async () => {
        gateway.listGuardrailConfigs.mockResolvedValue({ configs: [] });
        await open();
        expect(screen.getByText('No guardrail configurations found. Create one first.')).toBeInTheDocument();
    });

    it.todo('BUG: a failed config list reads as "No guardrail configurations found. Create one first." (GuardrailTestPanel.tsx load catch only logs)');

    it('enables direction only for surfaces that need it, and sends the choice', async () => {
        await open();
        const [, surface, direction] = selects();
        expect(direction).toBeDisabled();
        fireEvent.change(surface, { target: { value: 'mcp' } });
        expect(direction).toBeEnabled();
        fireEvent.change(direction, { target: { value: 'output' } });
        fireEvent.change(selects()[0], { target: { value: 'c1' } });
        typeContent('x');
        run();
        await waitFor(() =>
            expect(gateway.testGuardrails).toHaveBeenCalledWith({
                content: 'x',
                surface: 'mcp',
                direction: 'output',
                config_id: 'c1',
            }),
        );

        fireEvent.change(surface, { target: { value: 'a2a_inbound' } });
        expect(direction).toBeDisabled();
    });

    it('an example fills the content and clears a stale verdict', async () => {
        await open();
        typeContent('x');
        run();
        await screen.findByText('Content Passed');
        fireEvent.click(screen.getByRole('button', { name: 'Clean input' }));
        expect(screen.queryByText('Content Passed')).toBeNull();
        expect(screen.getByRole<HTMLTextAreaElement>('textbox').value).toMatch(/capital of France/);
    });

    it('reports a pass with timing and the number of configs run', async () => {
        await open();
        typeContent('x');
        run();
        expect(await screen.findByText('Content Passed')).toBeInTheDocument();
        expect(screen.getByText('12.3ms')).toBeInTheDocument();
        expect(screen.getByText(/2 configs/)).toBeInTheDocument();
    });

    it('does not read as a clean pass when no config ran', async () => {
        gateway.testGuardrails.mockResolvedValue({ ...PASSED, configs_evaluated: 0 });
        await open();
        typeContent('x');
        run();
        expect(await screen.findByText('No Checks Ran')).toBeInTheDocument();
        expect(screen.queryByText('Content Passed')).toBeNull();
        expect(screen.getByText(/selected config has the chosen surface\/direction/)).toBeInTheDocument();
    });

    it('shows a block with each violation', async () => {
        gateway.testGuardrails.mockResolvedValue({
            blocked: true,
            action: 'block',
            duration_ms: 3,
            configs_evaluated: 1,
            config_name: 'Strict',
            violations: [
                { action: 'block', check: 'pii', config_name: 'Strict', detail: 'SSN found' },
                { action: 'warn', check: 'toxic', detail: 'rude' },
            ],
        });
        await open();
        typeContent('x');
        run();
        expect(await screen.findByText('Content Blocked')).toBeInTheDocument();
        expect(screen.getByText('2 Violations')).toBeInTheDocument();
        expect(screen.getByText('SSN found')).toBeInTheDocument();
        expect(screen.getByText('rude')).toBeInTheDocument();
        expect(screen.getAllByText(getActionConfig('block').label).length).toBeGreaterThan(0);
    });

    it('distinguishes violations that did not block', async () => {
        gateway.testGuardrails.mockResolvedValue({
            blocked: false,
            action: 'redact',
            duration_ms: 3,
            configs_evaluated: 1,
            violations: [{ action: 'redact', check: 'pii', detail: 'email' }],
        });
        await open();
        typeContent('x');
        run();
        expect(await screen.findByText('Violations Detected')).toBeInTheDocument();
        expect(screen.getByText('1 Violation')).toBeInTheDocument();
    });

    it.each([
        [new Error('engine down'), 'engine down', 'engine down'],
        [{ response: { data: { detail: 'bad surface' } } }, 'bad surface', 'bad surface'],
        [{}, 'Test failed', 'Test failed'],
    ])('reports a failed run: %#', async (rejection, shown, toasted) => {
        gateway.testGuardrails.mockRejectedValue(rejection);
        await open();
        typeContent('x');
        run();
        expect(await screen.findByText(shown)).toBeInTheDocument();
        expect(toast.error).toHaveBeenCalledWith('Test Failed', toasted);
        expect(screen.queryByText('Content Passed')).toBeNull();
    });

    it('renders a structured detail rather than [object Object]', async () => {
        gateway.testGuardrails.mockRejectedValue({ response: { data: { detail: [{ msg: 'nope' }] } } });
        await open();
        typeContent('x');
        run();
        expect(await screen.findByText('[{"msg":"nope"}]')).toBeInTheDocument();
    });

    it('closes from the footer', async () => {
        await open();
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});

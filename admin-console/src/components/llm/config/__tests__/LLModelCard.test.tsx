/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Model card.
 *
 * Pinned: the menu wires each action to the right callback, a routing model
 * renders as a link to its group rather than as a real model, and pricing stays
 * clean for free models. A `0` price must not reach JSX through `a && <div/>`,
 * which renders a stray "0" on the card of every local model.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../LLModelStatsModal', () => ({
    default: ({ onClose }: { onClose: () => void }) => (
        <div>
            stats modal
            <button onClick={onClose}>close stats</button>
        </div>
    ),
}));

// The real logo reads the gateway to resolve per-tenant branding; not under test here.
vi.mock('../providerLogos', () => ({ ProviderLogo: () => <span>logo</span> }));

// The providers of the tenant, as the list of providers answers them.
const providers = vi.hoisted(() => ({ list: [] as Array<{ key: string; enabled: boolean }> }));
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient: null }) }));
vi.mock('../../../../hooks/useLlmProviders', () => ({ useLlmProviders: () => ({ providers: providers.list, loading: false }) }));

import LLModelCard from '../LLModelCard';
import type { LLModelConfig } from '../../../../lib/llmTypes';
import { explainDispatchRefusal } from '../../../../lib/dispatchRefusal';
import { GATEWAY_MESSAGES, proxyReturned, type GatewayMessageName } from '../../../../lib/__tests__/fixtures/dispatchRefusals';

function model(overrides: Record<string, unknown> = {}): LLModelConfig {
    return {
        id: 'model-1',
        name: 'GPT Test',
        provider: 'openai',
        model_name: 'gpt-test',
        enabled: true,
        api_url: 'https://api.example.test/v1',
        performance_metrics: { total_requests: 0, failed_requests: 0, success_rate: 100, total_tokens_consumed: 0 },
        groups: [],
        ...overrides,
    } as unknown as LLModelConfig;
}

const callbacks = {
    onEdit: vi.fn(),
    onDelete: vi.fn(),
    onCopy: vi.fn(),
    onTestConnection: vi.fn(),
    onToggleEnabled: vi.fn(),
    onNavigateToRoutingGroup: vi.fn(),
};

function renderCard(m: LLModelConfig, extra: { isTesting?: boolean; isFocused?: boolean } = {}) {
    return render(<LLModelCard model={m} isTesting={false} {...callbacks} {...extra} />);
}

function openMenu(container: HTMLElement) {
    const trigger = container.querySelector('.relative.ml-2 > button');
    if (!trigger) throw new Error('menu trigger not found');
    fireEvent.click(trigger);
}

describe('LLModelCard', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(cleanup);

    it('shows the model identity, explicit API URL and deep-link anchor', () => {
        const { container } = renderCard(model({ description: 'Fast one', parameters: 8 }));
        expect(screen.getByText('GPT Test')).toBeTruthy();
        expect(screen.getByText('Fast one')).toBeTruthy();
        expect(screen.getByText('gpt-test')).toBeTruthy();
        expect(screen.getByText('8B')).toBeTruthy();
        expect(screen.getByText('https://api.example.test/v1')).toBeTruthy();
        // Mission Control scrolls to this attribute; renaming it breaks the deep link.
        expect(container.querySelector('[data-llm-model-id="model-1"]')).toBeTruthy();
    });

    it('labels a discovered API URL as auto detected', () => {
        renderCard(model({ api_url: undefined }));
        expect(screen.getByText('Auto Detected')).toBeTruthy();
    });

    it.each([
        ['Edit', 'onEdit'],
        ['Delete', 'onDelete'],
        ['Copy', 'onCopy'],
        ['Test Connection', 'onTestConnection'],
    ] as const)('menu item %s calls its callback and closes the menu', (label, callback) => {
        const { container } = renderCard(model());
        openMenu(container);
        fireEvent.click(screen.getByRole('button', { name: label }));
        expect(callbacks[callback]).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole('button', { name: label })).toBeNull();
    });

    it('asks to disable an enabled model and to enable a disabled one', () => {
        const first = renderCard(model());
        openMenu(first.container);
        fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
        expect(callbacks.onToggleEnabled).toHaveBeenCalledWith(false);
        first.unmount();

        const second = renderCard(model({ enabled: false }));
        openMenu(second.container);
        fireEvent.click(screen.getByRole('button', { name: 'Enable' }));
        expect(callbacks.onToggleEnabled).toHaveBeenLastCalledWith(true);
    });

    it('offers a HuggingFace link only for HuggingFace models', () => {
        const open = vi.spyOn(window, 'open').mockImplementation(() => null);
        const other = renderCard(model());
        openMenu(other.container);
        expect(screen.queryByRole('button', { name: 'HuggingFace Info' })).toBeNull();
        other.unmount();

        const hf = renderCard(model({ provider: 'huggingface', model_name: 'meta/llama' }));
        openMenu(hf.container);
        fireEvent.click(screen.getByRole('button', { name: 'HuggingFace Info' }));
        expect(open).toHaveBeenCalledWith('https://huggingface.co/meta/llama', '_blank');
        open.mockRestore();
    });

    it('opens statistics from the menu and from the metrics button', () => {
        const { container } = renderCard(
            model({
                performance_metrics: {
                    total_requests: 1200,
                    failed_requests: 1,
                    success_rate: 99.5,
                    avg_latency_ms: 123.4,
                    total_tokens_consumed: 10,
                },
            })
        );
        expect(screen.getByText('123')).toBeTruthy();
        expect(screen.getByText('99.5')).toBeTruthy();
        expect(screen.getByText((1200).toLocaleString())).toBeTruthy();

        fireEvent.click(screen.getByRole('button', { name: 'View Detailed Statistics' }));
        expect(screen.getByText('stats modal')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'close stats' }));
        expect(screen.queryByText('stats modal')).toBeNull();

        openMenu(container);
        fireEvent.click(screen.getByRole('button', { name: 'View Statistics' }));
        expect(screen.getByText('stats modal')).toBeTruthy();
    });

    it('hides the detailed statistics button when there are no requests', () => {
        renderCard(model());
        expect(screen.queryByRole('button', { name: 'View Detailed Statistics' })).toBeNull();
    });

    it('shows per-million and per-thousand pricing', () => {
        renderCard(model({ cost_per_input_token: 3, cost_per_output_token: 15 }));
        expect(screen.getByText('$3.00')).toBeTruthy();
        expect(screen.getByText('$15.00')).toBeTruthy();
        expect(screen.getByText('1,000,000 input tokens @ $0.0030/1k')).toBeTruthy();
        expect(screen.getByText('1,000,000 output tokens @ $0.0150/1k')).toBeTruthy();
    });

    it('renders no pricing and no stray zero for a free model', () => {
        // No metrics: otherwise a legitimate "Total 0" would be on the card too.
        const { container } = renderCard(
            model({ cost_per_input_token: 0, cost_per_output_token: 0, performance_metrics: {} })
        );
        expect(screen.queryByText('Estimated Cost (1M tokens)')).toBeNull();
        expect(container.textContent).not.toMatch(/\b0\b/);
    });

    it('shows only the paid side when input is free', () => {
        const { container } = renderCard(model({ cost_per_input_token: 0, cost_per_output_token: 2 }));
        expect(screen.getByText('$2.00')).toBeTruthy();
        expect(screen.queryByText('input')).toBeNull();
        expect(container.textContent).not.toMatch(/\(1M tokens\)0/);
    });

    it('surfaces the health error only while unhealthy', () => {
        const unhealthy = renderCard(
            model({ health_status: { status: 'unhealthy', error_message: 'bad key', consecutive_failures: 1 } })
        );
        expect(screen.getByText('Error Details')).toBeTruthy();
        expect(screen.getByText('bad key')).toBeTruthy();
        unhealthy.unmount();

        renderCard(model({ health_status: { status: 'healthy', error_message: 'old error' } }));
        expect(screen.queryByText('Error Details')).toBeNull();
    });

    it.each(Object.keys(GATEWAY_MESSAGES) as GatewayMessageName[])(
        'says what to fix for the gateway refusal %s, not the raw answer',
        (name) => {
            const raw = proxyReturned(GATEWAY_MESSAGES[name]);
            renderCard(model({ health_status: { status: 'unhealthy', error_message: raw, consecutive_failures: 1 } }));
            const said = explainDispatchRefusal(raw);
            expect(said).not.toBeNull();
            expect(screen.getByText(said ?? '')).toBeTruthy();
            expect(screen.queryByText(raw)).toBeNull();
        },
    );

    it('renders a routing model as a link to its group, not as a model', () => {
        renderCard(model({ mode: 'routing', model_name: 'group-42', parameters: 8 }));
        expect(screen.queryByText('group-42')).toBeNull();
        expect(screen.queryByText('8B')).toBeNull();
        expect(screen.getByText('Virtual Routing Model')).toBeTruthy();

        fireEvent.click(screen.getByTitle('Go to Routing Group'));
        expect(callbacks.onNavigateToRoutingGroup).toHaveBeenCalledWith('group-42');
        fireEvent.click(screen.getByRole('button', { name: 'View Routing Group →' }));
        expect(callbacks.onNavigateToRoutingGroup).toHaveBeenCalledTimes(2);
    });

    it('shows load-balancing membership for a member model', () => {
        renderCard(model({ routing_group_id: 'abcdefgh-1234', routing_weight: 2.5 }));
        expect(screen.getByText('Load Balanced')).toBeTruthy();
        expect(screen.getByText('Weight: 2.5')).toBeTruthy();
        expect(screen.getByText('abcdefgh...')).toBeTruthy();
    });

    it('shows the testing overlay and the focus ring when asked', () => {
        const { container } = renderCard(model(), { isTesting: true, isFocused: true });
        expect(screen.getByText('Testing connection...')).toBeTruthy();
        expect(container.querySelector('[data-llm-model-id]')?.className).toContain('ring-primary-500');
    });

    describe('a model the gateway does not serve', () => {
        afterEach(() => {
            providers.list = [];
        });

        it('is marked "Not served" from its retire date on, with why', () => {
            renderCard(model({ retire_date: '2020-01-01' }));
            const badge = screen.getByTestId('model-not-served');
            expect(badge).toHaveTextContent('Not served');
            expect(badge).toHaveAttribute(
                'title',
                'The gateway does not serve this model. Retire date: 2020-01-01. From its retire date on, a model takes no call.',
            );
        });

        it('is marked "Not served" when its provider is disabled', () => {
            providers.list = [{ key: 'openai', enabled: false }];
            renderCard(model());
            expect(screen.getByTestId('model-not-served')).toHaveAttribute(
                'title',
                'The gateway does not serve this model: its provider (openai) is disabled. A model of a disabled provider takes no call.',
            );
        });

        it('has no such mark when it is served', () => {
            providers.list = [{ key: 'openai', enabled: true }];
            renderCard(model({ retire_date: '2999-01-01' }));
            expect(screen.queryByTestId('model-not-served')).toBeNull();
        });
    });
});

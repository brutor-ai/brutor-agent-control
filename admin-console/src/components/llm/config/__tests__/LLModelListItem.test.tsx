/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Model list row.
 *
 * Pinned: the mode label (a multimodal model must not fall through to
 * Chat), plus the toggle direction and the menu wiring.
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

import LLModelListItem from '../LLModelListItem';
import type { LLModelConfig } from '../../../../lib/llmTypes';

function model(overrides: Record<string, unknown> = {}): LLModelConfig {
    return {
        id: 'model-1',
        name: 'GPT Test',
        provider: 'openai',
        model_name: 'gpt-test',
        enabled: true,
        performance_metrics: { total_requests: 0, failed_requests: 0, success_rate: 0, total_tokens_consumed: 0 },
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

function renderRow(m: LLModelConfig, extra: { isTesting?: boolean; isFocused?: boolean } = {}) {
    return render(<LLModelListItem model={m} isTesting={false} {...callbacks} {...extra} />);
}

function openMenu(container: HTMLElement) {
    const trigger = container.querySelector('div.relative > button');
    if (!trigger) throw new Error('menu trigger not found');
    fireEvent.click(trigger);
}

describe('LLModelListItem', () => {
    beforeEach(() => vi.clearAllMocks());
    afterEach(cleanup);

    it.each([
        ['embedding', 'Embedding'],
        ['completion', 'Completion'],
        ['responses', 'Responses'],
        ['image_generation', 'Image'],
        ['audio_speech', 'TTS'],
        ['audio_transcription', 'Transcribe'],
        ['video_generation', 'Video'],
        ['rerank', 'Rerank'],
        ['moderation', 'Moderation'],
        ['chat', 'Chat'],
        [undefined, 'Chat'],
    ])('labels mode %s as %s', (mode, label) => {
        renderRow(model({ mode }));
        expect(screen.getByText(label)).toBeTruthy();
    });

    it.each([
        [2_000_000, '2M'],
        [1_500_000, '1.5M'],
        [128_000, '128K'],
        [512, '512'],
    ])('formats a context window of %d as %s', (context_window, label) => {
        renderRow(model({ capabilities: { context_window } }));
        expect(screen.getByText(label)).toBeTruthy();
    });

    it('shows the size and metrics, colouring the success rate by threshold', () => {
        const { rerender } = renderRow(
            model({
                parameters: 70,
                health_status: { status: 'healthy', avg_latency_ms: 88.6 },
                performance_metrics: { total_requests: 42, success_rate: 96.25 },
            })
        );
        expect(screen.getByText('70B')).toBeTruthy();
        expect(screen.getByText('89ms')).toBeTruthy();
        expect(screen.getByText('42')).toBeTruthy();
        expect(screen.getByText('96.3%').className).toContain('text-green-600');

        rerender(
            <LLModelListItem
                model={model({ performance_metrics: { success_rate: 85 } })}
                isTesting={false}
                {...callbacks}
            />
        );
        expect(screen.getByText('85.0%').className).toContain('text-yellow-600');

        rerender(
            <LLModelListItem
                model={model({ performance_metrics: { success_rate: 50 } })}
                isTesting={false}
                {...callbacks}
            />
        );
        expect(screen.getByText('50.0%').className).toContain('text-red-600');
    });

    it('toggles in the opposite direction of the current state', () => {
        const first = renderRow(model());
        fireEvent.click(screen.getByTitle('Disable model'));
        expect(callbacks.onToggleEnabled).toHaveBeenCalledWith(false);
        first.unmount();

        renderRow(model({ enabled: false }));
        fireEvent.click(screen.getByTitle('Enable model'));
        expect(callbacks.onToggleEnabled).toHaveBeenLastCalledWith(true);
    });

    it.each([
        ['Edit', 'onEdit'],
        ['Delete', 'onDelete'],
        ['Copy', 'onCopy'],
        ['Test Connection', 'onTestConnection'],
    ] as const)('menu item %s calls its callback and closes the menu', (label, callback) => {
        const { container } = renderRow(model());
        openMenu(container);
        fireEvent.click(screen.getByRole('button', { name: label }));
        expect(callbacks[callback]).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole('button', { name: label })).toBeNull();
    });

    it('disables the test action while a test is running', () => {
        const { container } = renderRow(model(), { isTesting: true });
        openMenu(container);
        expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Testing...' }).disabled).toBe(true);
    });

    it('opens statistics from the menu and closes them again', () => {
        const { container } = renderRow(model());
        openMenu(container);
        fireEvent.click(screen.getByRole('button', { name: 'View Statistics' }));
        expect(screen.getByText('stats modal')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'close stats' }));
        expect(screen.queryByText('stats modal')).toBeNull();
    });

    it('opens the HuggingFace page for a HuggingFace model', () => {
        const open = vi.spyOn(window, 'open').mockImplementation(() => null);
        const { container } = renderRow(model({ tags: ['huggingface'], model_name: 'meta/llama' }));
        openMenu(container);
        fireEvent.click(screen.getByRole('button', { name: 'HuggingFace Info' }));
        expect(open).toHaveBeenCalledWith('https://huggingface.co/meta/llama', '_blank');
        open.mockRestore();
    });

    it('renders a routing model as a link to its group', () => {
        renderRow(model({ mode: 'routing', model_name: 'group-42', parameters: 8 }));
        expect(screen.getByText('Routing')).toBeTruthy();
        expect(screen.queryByText('8B')).toBeNull();
        fireEvent.click(screen.getByTitle('Go to Routing Group'));
        expect(callbacks.onNavigateToRoutingGroup).toHaveBeenCalledWith('group-42');
    });

    it('flags load-balanced membership and carries the focus ring and deep-link anchor', () => {
        const { container } = renderRow(model({ routing_group_id: 'g1' }), { isFocused: true });
        expect(screen.getByText('Load Balanced')).toBeTruthy();
        expect(container.querySelector('[data-llm-model-id="model-1"]')?.className).toContain('ring-primary-500');
    });

    describe('a model the gateway does not serve', () => {
        afterEach(() => {
            providers.list = [];
        });

        it('is marked "Not served" from its retire date on, with why', () => {
            renderRow(model({ retire_date: '2020-01-01' }));
            const badge = screen.getByTestId('model-not-served');
            expect(badge).toHaveTextContent('Not served');
            expect(badge).toHaveAttribute(
                'title',
                'The gateway does not serve this model. Retire date: 2020-01-01. From its retire date on, a model takes no call.',
            );
        });

        it('is marked "Not served" when its provider is disabled', () => {
            providers.list = [{ key: 'openai', enabled: false }];
            renderRow(model());
            expect(screen.getByTestId('model-not-served')).toHaveAttribute(
                'title',
                'The gateway does not serve this model: its provider (openai) is disabled. A model of a disabled provider takes no call.',
            );
        });

        it('has no such mark when it is served', () => {
            providers.list = [{ key: 'openai', enabled: true }];
            renderRow(model({ retire_date: '2999-01-01' }));
            expect(screen.queryByTestId('model-not-served')).toBeNull();
        });
    });
});

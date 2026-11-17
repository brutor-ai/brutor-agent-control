/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Model statistics modal.
 *
 * The numbers here are derived client side, so the arithmetic is what is pinned:
 * success and failure rates, the 40/60 token split and the cost estimate. The
 * estimate is not all-or-nothing: a model with a free side (input or
 * output priced 0) must not report $0 whatever it has consumed.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import LLModelStatsModal from '../LLModelStatsModal';
import { GATEWAY_MESSAGES, proxyReturned } from '../../../../lib/__tests__/fixtures/dispatchRefusals';
import type { LLModelConfig } from '../../../../lib/llmTypes';

function model(overrides: Record<string, unknown> = {}): LLModelConfig {
    return {
        id: 'model-1',
        name: 'GPT Test',
        provider: 'openai',
        model_name: 'gpt-test',
        enabled: true,
        performance_metrics: {
            total_requests: 200,
            failed_requests: 50,
            success_rate: 75,
            avg_latency_ms: 321.7,
            total_tokens_consumed: 1_000_000,
        },
        ...overrides,
    } as unknown as LLModelConfig;
}

describe('LLModelStatsModal', () => {
    afterEach(cleanup);

    it('derives request rates and the token split', () => {
        render(<LLModelStatsModal model={model()} onClose={vi.fn()} />);
        expect(screen.getByText('Model Statistics: GPT Test')).toBeTruthy();
        expect(screen.getByText('75.0% success rate')).toBeTruthy();
        expect(screen.getByText('25.0% failure rate')).toBeTruthy();
        expect(screen.getByText('150')).toBeTruthy();
        expect(screen.getByText('50').className).toContain('text-red-600');
        expect(screen.getByText((400_000).toLocaleString())).toBeTruthy();
        expect(screen.getByText((600_000).toLocaleString())).toBeTruthy();
        expect(screen.getByText('322')).toBeTruthy();
    });

    it('reports zero rates, not NaN, with no traffic', () => {
        render(
            <LLModelStatsModal
                model={model({ performance_metrics: { total_requests: 0, failed_requests: 0 } })}
                onClose={vi.fn()}
            />
        );
        expect(screen.getByText('0.0% success rate')).toBeTruthy();
        expect(screen.getByText('0.0% failure rate')).toBeTruthy();
    });

    it('estimates cost from both sides at per-million prices', () => {
        render(
            <LLModelStatsModal model={model({ cost_per_input_token: 10, cost_per_output_token: 20 })} onClose={vi.fn()} />
        );
        // 400k input at $10/M is $4; 600k output at $20/M is $12.
        expect(screen.getByText('$16.0000')).toBeTruthy();
        expect(screen.getByText('$0.0100/1K')).toBeTruthy();
        expect(screen.getByText('$0.0200/1K')).toBeTruthy();
    });

    it('still counts the paid side when the other side is free', () => {
        render(
            <LLModelStatsModal model={model({ cost_per_input_token: 0, cost_per_output_token: 20 })} onClose={vi.fn()} />
        );
        expect(screen.getByText('$12.0000')).toBeTruthy();
    });

    it('says what to fix for a gateway dispatch refusal, not the raw answer', () => {
        const raw = proxyReturned(GATEWAY_MESSAGES.provider_api_url_missing);
        render(
            <LLModelStatsModal
                model={model({ health_status: { status: 'unhealthy', consecutive_failures: 1, error_message: raw } })}
                onClose={vi.fn()}
            />
        );
        expect(screen.getByText(/set API URL/)).toBeTruthy();
        expect(screen.queryByText(raw)).toBeNull();
    });

    it('shows health detail, including failures and the probe type', () => {
        render(
            <LLModelStatsModal
                model={model({
                    health_status: {
                        status: 'unhealthy',
                        consecutive_failures: 3,
                        avg_latency_ms: 45.2,
                        error_message: 'upstream 500',
                        last_check: new Date(Date.now() - 2 * 3_600_000).toISOString(),
                        last_success: new Date(Date.now() - 3 * 86_400_000).toISOString(),
                        last_probe_type: 'catalog',
                    },
                })}
                onClose={vi.fn()}
            />
        );
        expect(screen.getByText('unhealthy').className).toContain('text-red-700');
        expect(screen.getByText('3').className).toContain('text-red-600');
        expect(screen.getByText('Response time: 45ms')).toBeTruthy();
        expect(screen.getByText('upstream 500')).toBeTruthy();
        expect(screen.getByText('2h ago')).toBeTruthy();
        expect(screen.getByText('3d ago')).toBeTruthy();
        expect(screen.getByText('catalog')).toBeTruthy();
    });

    it('says when a model was never checked, with recent checks relative', () => {
        const { unmount } = render(<LLModelStatsModal model={model()} onClose={vi.fn()} />);
        expect(screen.getAllByText('Never').length).toBe(2);
        expect(screen.getByText('unknown').className).toContain('text-yellow-700');
        unmount();

        render(
            <LLModelStatsModal
                model={model({
                    health_status: {
                        status: 'healthy',
                        last_check: new Date(Date.now() - 5 * 60_000).toISOString(),
                        last_success: new Date().toISOString(),
                    },
                })}
                onClose={vi.fn()}
            />
        );
        expect(screen.getByText('healthy').className).toContain('text-green-700');
        expect(screen.getByText('5m ago')).toBeTruthy();
        expect(screen.getByText('Just now')).toBeTruthy();
    });

    it('shows configuration values, with defaults for what is unset', () => {
        const { unmount } = render(
            <LLModelStatsModal
                model={model({
                    default_temperature: 0.2,
                    default_max_tokens: 4096,
                    timeout: 30,
                    supports_streaming: true,
                    supports_function_calling: false,
                })}
                onClose={vi.fn()}
            />
        );
        expect(screen.getByText('0.2')).toBeTruthy();
        expect(screen.getByText((4096).toLocaleString())).toBeTruthy();
        expect(screen.getByText('30s')).toBeTruthy();
        expect(screen.getByText('Yes')).toBeTruthy();
        expect(screen.getByText('No')).toBeTruthy();
        unmount();

        render(<LLModelStatsModal model={model()} onClose={vi.fn()} />);
        expect(screen.getAllByText('Default').length).toBe(3);
        expect(screen.getAllByText('Unknown').length).toBe(2);
    });

    it('closes through the header button', () => {
        const onClose = vi.fn();
        render(<LLModelStatsModal model={model()} onClose={onClose} />);
        const header = screen.getByText('Model Statistics: GPT Test').closest('div')?.parentElement;
        const close = header?.querySelector('button');
        if (!close) throw new Error('close button not found');
        fireEvent.click(close);
        expect(onClose).toHaveBeenCalledTimes(1);
    });
});

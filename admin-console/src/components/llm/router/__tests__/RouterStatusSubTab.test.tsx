/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Router status view.
 *
 * Pinned: Redis credentials are masked before display, test history is lazy per
 * group and is refetched when the parent refreshes, and the derived health and
 * warning states are the ones the numbers call for.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const client = { getRouterTestHistory: vi.fn() };
const gateway = { gatewayClient: client };
vi.mock('../../../../hooks/useGateway', () => ({ useGateway: () => gateway }));
vi.mock('../../config/providerLogos', () => ({ ProviderLogo: () => <span>logo</span> }));

import RouterStatusSubTab from '../RouterStatusSubTab';
import type { RouterStatusResponse } from '../../../../lib/llmTypes';

function groupStatus(overrides: Record<string, unknown> = {}) {
    return {
        id: 'g1',
        name: 'Pool One',
        routing_strategy: 'least-busy',
        total_models: 2,
        healthy_models: 2,
        total_requests: 0,
        error_rate: 0,
        models: [],
        ...overrides,
    };
}

function modelStatus(overrides: Record<string, unknown> = {}) {
    return {
        model_id: 'm1',
        model_name: 'Model One',
        provider: 'openai',
        health_status: 'healthy',
        routing_weight: 3,
        total_requests: 1000,
        avg_latency_ms: 87.6,
        error_rate: 0,
        ...overrides,
    };
}

function statusOf(overrides: Record<string, unknown> = {}): RouterStatusResponse {
    return {
        enabled: true,
        routing_strategy: 'usage-based-routing-v2',
        total_deployments: 5,
        healthy_deployments: 4,
        unhealthy_deployments: 1,
        unassigned_deployments: 0,
        total_routing_groups: 1,
        routing_groups: [],
        cache_enabled: false,
        cache_status: 'disconnected',
        router_cache: {
            cached_routers: 1,
            max_cached_routers: 10,
            cache_utilization: 0.1,
            hit_rate: 0.9,
            cache_hits: 90,
            cache_misses: 10,
            total_rebuilds: 5,
            router_ttl_seconds: 300,
        },
        fallback_enabled: false,
        health_check_enabled: false,
        total_requests: 1000,
        total_errors: 0,
        error_rate: 0,
        avg_latency_ms: 0,
        uptime_seconds: 1,
        timestamp: '2026-11-01T00:00:00Z',
        ...overrides,
    } as unknown as RouterStatusResponse;
}

/** The value paired with a label in the cache stats grid. */
function statValue(label: string): HTMLElement {
    const row = screen.getByText(label).closest('.flex.items-start');
    const value = row?.querySelector('span');
    if (!value) throw new Error(`No value for ${label}`);
    return value;
}

describe('RouterStatusSubTab', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        client.getRouterTestHistory.mockReset();
        client.getRouterTestHistory.mockResolvedValue({ items: [] });
    });
    afterEach(cleanup);

    it('asks for a refresh when there is no status', () => {
        render(<RouterStatusSubTab status={null} />);
        expect(screen.getByText('No status data available')).toBeTruthy();
    });

    it('explains an inactive router rather than showing empty numbers', () => {
        render(<RouterStatusSubTab status={statusOf({ enabled: false })} />);
        expect(screen.getByText('Router Not Active')).toBeTruthy();
        expect(screen.queryByText('Deployments')).toBeNull();
    });

    it('shows the headline numbers', () => {
        render(
            <RouterStatusSubTab
                status={statusOf({ total_errors: 12, error_rate: 1.2, avg_latency_ms: 240.4 })}
            />
        );
        expect(screen.getByText('Deployments').previousElementSibling?.textContent).toBe('5');
        expect(screen.getByText('4 healthy · 1 unhealthy')).toBeTruthy();
        expect(screen.getByText('1.2% error rate')).toBeTruthy();
        expect(screen.getByText('240ms')).toBeTruthy();
    });

    it('says when there are no errors and no latency yet', () => {
        render(<RouterStatusSubTab status={statusOf()} />);
        expect(screen.getByText('No errors')).toBeTruthy();
        expect(screen.getByText('—')).toBeTruthy();
    });

    it('calls out unassigned models, with the right plural', () => {
        const { unmount } = render(<RouterStatusSubTab status={statusOf({ unassigned_deployments: 1 })} />);
        expect(screen.getByText('1 model not assigned to any routing group')).toBeTruthy();
        unmount();
        render(<RouterStatusSubTab status={statusOf({ unassigned_deployments: 3 })} />);
        expect(screen.getByText('3 models not assigned to any routing group')).toBeTruthy();
    });

    describe('infrastructure', () => {
        it('never shows Redis credentials', () => {
            const { container } = render(
                <RouterStatusSubTab
                    status={statusOf({
                        cache_enabled: true,
                        redis_url: 'redis://admin:s3cret@redis.internal:6379/0',
                        fallback_enabled: true,
                        health_check_enabled: true,
                    })}
                />
            );
            expect(container.textContent).not.toContain('s3cret');
            expect(container.textContent).not.toContain('admin:');
            expect(screen.getByText('Connected · redis://***@redis.internal:6379/0')).toBeTruthy();
            expect(screen.getByText('Enabled')).toBeTruthy();
            expect(screen.getByText('Active')).toBeTruthy();
            expect(screen.getByText('Rate-Limit Optimized')).toBeTruthy();
        });

        it('reports what is off, and an unknown default strategy by its raw name', () => {
            render(<RouterStatusSubTab status={statusOf({ routing_strategy: 'custom-x' })} />);
            expect(screen.getByText('Not configured')).toBeTruthy();
            expect(screen.getByText('Disabled')).toBeTruthy();
            expect(screen.getByText('Inactive')).toBeTruthy();
            expect(screen.getByText('custom-x')).toBeTruthy();
        });

        it('shows the cache numbers, without warnings when it is healthy', () => {
            render(<RouterStatusSubTab status={statusOf({
                router_cache: {
                    cached_routers: 1, max_cached_routers: 10, cache_utilization: 0.1,
                    hit_rate: 0.9, cache_hits: 90, cache_misses: 0, total_rebuilds: 5, router_ttl_seconds: 300,
                },
            })} />);
            expect(screen.getByText('1 / 10 slots')).toBeTruthy();
            expect(statValue('Hit Rate').textContent).toBe('90.0%');
            expect(statValue('Hit Rate').className).not.toContain('text-yellow-600');
            expect(statValue('Cache Misses').className).not.toContain('text-yellow-600');
            expect(statValue('Total Rebuilds').className).not.toContain('text-yellow-600');
            expect(statValue('Router TTL').textContent).toBe('300s');
        });

        it('warns on a middling hit rate and many rebuilds, and shows long TTLs in hours', () => {
            render(<RouterStatusSubTab status={statusOf({
                router_cache: {
                    cached_routers: 9, max_cached_routers: 10, cache_utilization: 0.9,
                    hit_rate: 0.6, cache_hits: 10, cache_misses: 4, total_rebuilds: 9, router_ttl_seconds: 7200,
                },
            })} />);
            expect(statValue('Hit Rate').className).toContain('text-yellow-600');
            expect(statValue('Cache Misses').className).toContain('text-yellow-600');
            expect(statValue('Total Rebuilds').className).toContain('text-yellow-600');
            expect(statValue('Router TTL').textContent).toBe('2.0h');
        });
    });

    describe('routing groups', () => {
        it('summarises a group by its friendly strategy, health and error level', () => {
            render(
                <RouterStatusSubTab
                    status={statusOf({
                        routing_groups: [
                            groupStatus({
                                total_requests: 1234,
                                healthy_models: 1,
                                error_rate: 12,
                                has_fallbacks: true,
                                // Alerting is not in this release: a value would not be shown.
                                ...({ alerting_enabled: true } as object),
                            }),
                            groupStatus({ id: 'g2', name: 'Two', routing_strategy: 'odd', total_models: 1, healthy_models: 0, error_rate: 3 }),
                            groupStatus({ id: 'g3', name: 'Three', total_models: 0, healthy_models: 0, error_rate: 1 }),
                        ],
                    })}
                />
            );
            expect(screen.getAllByText('Queue-Aware')).toHaveLength(2);
            expect(screen.getByText('odd')).toBeTruthy();
            expect(screen.getByText('1 model')).toBeTruthy();
            expect(screen.getAllByText('2 models').length).toBe(1);
            expect(screen.getByText('50%')).toBeTruthy();
            // A group with no models must not divide by zero.
            expect(screen.getAllByText('0%').length).toBe(2);
            expect(screen.getByTitle('Fallbacks configured')).toBeTruthy();
            expect(screen.queryByTitle('Alerting on')).toBeNull();
            expect(screen.getByText('12% err').className).toContain('text-red-700');
            expect(screen.getByText('3% err').className).toContain('text-yellow-700');
            expect(screen.getByText('1% err').className).toContain('text-secondary');
        });

        it('expands to per-model detail and collapses again', async () => {
            render(
                <RouterStatusSubTab
                    status={statusOf({
                        routing_groups: [
                            groupStatus({
                                avg_latency_ms: 50.4,
                                total_requests: 5000,
                                models: [
                                    modelStatus(),
                                    modelStatus({ model_id: 'm2', model_name: 'Bad', health_status: 'unhealthy', error_rate: 15, avg_latency_ms: null }),
                                    modelStatus({ model_id: 'm3', model_name: 'Meh', health_status: 'degraded', error_rate: 5 }),
                                    modelStatus({ model_id: 'm4', model_name: 'Odd', health_status: 'nope' }),
                                ],
                            }),
                        ],
                    })}
                />
            );
            expect(screen.queryByText('Model One')).toBeNull();

            fireEvent.click(screen.getByText('Pool One'));
            expect(screen.getByText('Model One')).toBeTruthy();
            expect(screen.getByText('2/2')).toBeTruthy();
            expect(screen.getByText('50ms')).toBeTruthy();
            expect(screen.getByText('15%').className).toContain('text-red-600');
            expect(screen.getByText('5%').className).toContain('text-yellow-600');
            expect(screen.getAllByText('88ms').length).toBe(3);
            await screen.findByText(/No tests recorded yet/);

            fireEvent.click(screen.getByText('Pool One'));
            expect(screen.queryByText('Model One')).toBeNull();
        });

        it('says when a group has no models', async () => {
            render(<RouterStatusSubTab status={statusOf({ routing_groups: [groupStatus()] })} />);
            fireEvent.click(screen.getByText('Pool One'));
            expect(screen.getByText('No models assigned to this routing group')).toBeTruthy();
            expect(screen.getAllByText('—').length).toBeGreaterThan(1);
            await screen.findByText(/No tests recorded yet/);
        });
    });

    describe('test history', () => {
        const status = statusOf({ routing_groups: [groupStatus()] });

        it('loads per group on first expand only, and renders each entry', async () => {
            let resolve: (value: unknown) => void = () => {};
            client.getRouterTestHistory.mockReturnValue(new Promise(r => { resolve = r; }));
            render(<RouterStatusSubTab status={status} />);
            expect(client.getRouterTestHistory).not.toHaveBeenCalled();

            fireEvent.click(screen.getByText('Pool One'));
            expect(client.getRouterTestHistory).toHaveBeenCalledWith({ routing_group_id: 'g1', limit: 10 });
            expect(screen.getByText('Loading…')).toBeTruthy();

            resolve({
                items: [
                    {
                        timestamp: new Date(Date.now() - 30_000).toISOString(),
                        selected_model: 'gpt-x',
                        status_code: 200,
                        latency_ms: 44.4,
                        message: 'ok',
                    },
                    {
                        timestamp: new Date(Date.now() - 5 * 60_000).toISOString(),
                        error: 'boom',
                        status_code: 500,
                        message: 'upstream failed',
                    },
                    { timestamp: new Date(Date.now() - 2 * 3_600_000).toISOString(), message: 'no pick' },
                    { timestamp: '2026-01-02T00:00:00Z', selected_model: 'old', message: 'old one' },
                    { timestamp: 'not-a-date', selected_model: 'weird', message: 'bad date' },
                ],
            });
            expect(await screen.findByText('gpt-x')).toBeTruthy();
            expect(screen.getByText('200')).toBeTruthy();
            expect(screen.getByText('44ms')).toBeTruthy();
            expect(screen.getByText('30s ago')).toBeTruthy();
            expect(screen.getByText('error')).toBeTruthy();
            expect(screen.getByText('5m ago')).toBeTruthy();
            expect(screen.getByText('2h ago')).toBeTruthy();
            expect(screen.getByText('<unknown>')).toBeTruthy();
            expect(screen.getByText(new Date('2026-01-02T00:00:00Z').toLocaleDateString())).toBeTruthy();
            // An unparsable time stays readable rather than "Invalid Date".
            expect(screen.getByText('not-a-date')).toBeTruthy();

            fireEvent.click(screen.getByText('Pool One'));
            fireEvent.click(screen.getByText('Pool One'));
            expect(client.getRouterTestHistory).toHaveBeenCalledTimes(1);
        });

        it('a failed load offers Retry, which asks for the history of that group again', async () => {
            client.getRouterTestHistory.mockRejectedValueOnce(new Error('down'));
            render(<RouterStatusSubTab status={status} />);
            fireEvent.click(screen.getByText('Pool One'));
            await screen.findByRole('alert');
            client.getRouterTestHistory.mockResolvedValue({
                items: [{ timestamp: new Date().toISOString(), selected_model: 'gpt-retried', status_code: 200, latency_ms: 10, message: 'ok' }],
            });
            fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
            expect(await screen.findByText('gpt-retried')).toBeTruthy();
            expect(screen.queryByRole('alert')).toBeNull();
            expect(client.getRouterTestHistory).toHaveBeenCalledTimes(2);
        });

        it('a failed load is not "no tests recorded": the group says so, without retrying on every expand', async () => {
            client.getRouterTestHistory.mockRejectedValue(new Error('down'));
            render(<RouterStatusSubTab status={status} />);
            fireEvent.click(screen.getByText('Pool One'));
            expect((await screen.findByRole('alert')).textContent).toContain('Failed to load the test history: down');
            expect(screen.queryByText(/No tests recorded yet/)).toBeNull();

            fireEvent.click(screen.getByText('Pool One'));
            fireEvent.click(screen.getByText('Pool One'));
            expect(client.getRouterTestHistory).toHaveBeenCalledTimes(1);
        });

        it('refetches loaded groups when the parent refreshes, keeping the list on failure', async () => {
            client.getRouterTestHistory.mockResolvedValueOnce({
                items: [{ timestamp: 'x', selected_model: 'first', message: 'm' }],
            });
            const { rerender } = render(<RouterStatusSubTab status={status} />);
            fireEvent.click(screen.getByText('Pool One'));
            await screen.findByText('first');

            client.getRouterTestHistory.mockResolvedValueOnce({
                items: [{ timestamp: 'x', selected_model: 'second', message: 'm' }],
            });
            rerender(<RouterStatusSubTab status={{ ...status }} />);
            expect(await screen.findByText('second')).toBeTruthy();

            client.getRouterTestHistory.mockRejectedValueOnce(new Error('down'));
            rerender(<RouterStatusSubTab status={{ ...status }} />);
            await waitFor(() => expect(client.getRouterTestHistory).toHaveBeenCalledTimes(3));
            expect(screen.getByText('second')).toBeTruthy();
        });
    });
});

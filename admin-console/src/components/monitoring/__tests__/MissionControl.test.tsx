/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control — the main `loadAllData` fan-out.
 *
 * Two properties matter here.
 *
 * The fan-out is a pair of `Promise.allSettled` batches whose results are
 * mapped field by field. `allSettled` never rejects, so a partial outage is
 * invisible unless the mapping is asserted: if one rejected sub-fetch made
 * the whole update fall through, an operator with a sick metrics endpoint
 * would lose every KPI on the page — including the healthy ones telling them
 * traffic is still flowing. The dashboard must degrade, not go dark.
 *
 * Second, the fan-out runs on a 30s timer and can outlast it at 90 days /
 * All Time, so responses arrive out of order. Only the newest load may write
 * state; otherwise the KPIs silently revert to a period the header no longer
 * shows.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor, cleanup } from '@testing-library/react';

const fetchServers = vi.fn();
const fetchRealTimeMetrics = vi.fn();
const fetchPerformanceMetrics = vi.fn();
const fetchDashboardMetrics = vi.fn();
const fetchAllUsageAlerts = vi.fn();

/**
 * Mission Control touches dozens of client methods across its tabs. Only the
 * main fan-out is under test, so everything else auto-stubs to a resolved
 * null rather than being listed one by one — a missing name would otherwise
 * throw from an unrelated tab effect and read as a failure of this test.
 */
const autoStub = (overrides: Record<string, unknown>): Record<string, unknown> =>
    new Proxy(overrides, {
        get(target, prop: string) {
            if (prop in target) return target[prop];
            return vi.fn().mockResolvedValue(null);
        },
        has: () => true,
    });

const stubClient = autoStub({});

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () =>
        autoStub({
            fetchServers,
            fetchRealTimeMetrics,
            fetchPerformanceMetrics,
            fetchDashboardMetrics,
            fetchAllUsageAlerts,
            isAuthenticated: true,
            gatewayClient: stubClient,
        }),
}));
const toastError = vi.fn();
vi.mock('../../common/Toast', () => ({
    useToast: () => ({ success: vi.fn(), error: toastError, warning: vi.fn(), info: vi.fn() }),
}));
vi.mock('react-hot-toast', () => ({ default: { error: (...a: unknown[]): unknown => toastError(...a), success: vi.fn() } }));
vi.mock('../../common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn().mockResolvedValue(true),
}));

import MissionControl from '../MissionControl';

vi.setConfig({ testTimeout: 30_000 });

const dashboard = (over: Record<string, unknown> = {}) => ({
    dashboard: {
        requests_last_hour: 41,
        requests_last_day: 9182,
        active_connections: 7,
        error_rate: 0.5,
        avg_response_time_ms: 120,
        ...over,
    },
});

// The default (Traffic) tab is fed by the performance member of the fan-out.
const performance = (serverName = 'srv-alpha') => ({
    performance_metrics: {
        top_mcp_servers: [
            { server_id: 'srv_1', server_name: serverName, request_count: 42 },
        ],
    },
});

describe('MissionControl loadAllData', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        localStorage.clear();
        fetchServers.mockResolvedValue([]);
        fetchRealTimeMetrics.mockResolvedValue({ real_time_metrics: {} });
        fetchPerformanceMetrics.mockResolvedValue(performance());
        fetchDashboardMetrics.mockResolvedValue(dashboard());
        fetchAllUsageAlerts.mockResolvedValue({ alerts: [] });
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('maps a healthy fan-out onto the dashboard', async () => {
        render(<MissionControl />);
        expect(await screen.findByText('srv-alpha')).toBeInTheDocument();
        expect(screen.queryByText('No MCP server activity')).not.toBeInTheDocument();
    });

    it('keeps the members that succeeded when others in the batch reject', async () => {
        // Half the fan-out is down; the performance member is fine.
        fetchServers.mockRejectedValue(new Error('servers unavailable'));
        fetchDashboardMetrics.mockRejectedValue(new Error('dashboard unavailable'));
        fetchRealTimeMetrics.mockRejectedValue(new Error('realtime unavailable'));
        fetchAllUsageAlerts.mockRejectedValue(new Error('alerts unavailable'));

        render(<MissionControl />);

        // The healthy member still paints. A mapping that bailed on the first
        // rejection would leave the whole page on its empty state, telling the
        // operator there is no traffic when there is.
        expect(await screen.findByText('srv-alpha')).toBeInTheDocument();
        // And a partial outage is not escalated into a full-screen error.
        expect(screen.queryByText(/Failed to load dashboard data/i)).not.toBeInTheDocument();
    });

    it('a read that fails says so in the tab that holds its cards, and the others still paint', async () => {
        fetchRealTimeMetrics.mockRejectedValue(new Error('realtime unavailable'));

        render(<MissionControl />);

        expect(await screen.findByText('srv-alpha')).toBeInTheDocument();
        const alerts = await screen.findAllByRole('alert');
        expect(alerts.map((a) => a.textContent)).toEqual([
            expect.stringContaining('Failed to load the live metrics: realtime unavailable'),
        ]);
        expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
        expect(toastError).not.toHaveBeenCalled();
    });

    it('a refresh that fails keeps the cards and marks them as stale; no toast per poll', async () => {
        localStorage.setItem('mission-control-auto-refresh', '1');
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
        try {
            render(<MissionControl />);
            expect(await screen.findByText('srv-alpha')).toBeInTheDocument();
            expect(screen.queryByRole('status')).not.toBeInTheDocument();

            fetchPerformanceMetrics.mockRejectedValue(new Error('timeout'));
            await act(async () => {
                await vi.advanceTimersByTimeAsync(30_000);
            });
            const mark = await screen.findByRole('status');
            expect(mark).toHaveTextContent(/Could not refresh; showing what was loaded at \d\d:\d\d/);
            expect(mark).toHaveTextContent('Failed to load the performance metrics: timeout');
            expect(screen.getByText('srv-alpha')).toBeInTheDocument();

            await act(async () => {
                await vi.advanceTimersByTimeAsync(30_000);
            });
            expect(screen.getAllByRole('status')).toHaveLength(1);
            expect(toastError).not.toHaveBeenCalled();

            fetchPerformanceMetrics.mockResolvedValue(performance());
            await act(async () => {
                await vi.advanceTimersByTimeAsync(30_000);
            });
            await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
        } finally {
            vi.useRealTimers();
        }
    });

    it('drops a superseded load so a slow response cannot repaint the KPIs', async () => {
        // Hold the first load open, let a second one answer, then release the
        // first: its (older) numbers must never reach the screen.
        let releaseFirst: () => void = () => {};
        fetchPerformanceMetrics.mockImplementation(
            () => new Promise((resolve) => {
                releaseFirst = () => resolve(performance('srv-stale'));
            }),
        );

        const { unmount } = render(<MissionControl />);
        await waitFor(() => expect(fetchPerformanceMetrics).toHaveBeenCalled());

        // Unmount retires the in-flight load by bumping the sequence.
        unmount();
        releaseFirst();
        await new Promise((r) => setTimeout(r, 50));

        // The retired load wrote nothing — nothing from it reaches the screen,
        // and no "setState on an unmounted component" warning is emitted.
        expect(screen.queryByText('srv-stale')).not.toBeInTheDocument();
    });
});

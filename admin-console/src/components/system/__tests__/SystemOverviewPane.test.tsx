/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * System overview health grid.
 *
 * Pinned: cards land in the right section (and an unknown kind falls back to
 * infrastructure rather than vanishing), cards deep-link only when they can, a
 * failed first load offers Retry but a failed poll does not blank a grid already
 * showing, and polling stops while the tab is hidden.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';

const gatewayClient = { getSystemOverview: vi.fn() };
const gateway = { gatewayClient };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => gateway }));

import SystemOverviewPane from '../SystemOverviewPane';

const card = (kind: string, id: string, extra: object = {}) => ({
    kind,
    id,
    label: `${kind}-${id}`,
    status: 'healthy',
    detail: null,
    latency_ms: null,
    target_route: null,
    ...extra,
});

const RESPONSE = {
    generated_at: '2026-11-05T10:00:00Z',
    counters: { healthy: 4, degraded: 1, down: 2, unknown: 3, disabled: 5 },
    maintenance: { mode: 'off', since: null },
    cards: [
        card('database', 'pg', { latency_ms: 12, detail: 'primary' }),
        card('redis', 'r'),
        card('qdrant', 'q', { status: 'down' }),
        card('llm_upstream', 'm1', { status: 'degraded', target_route: 'ai-models' }),
        card('mcp_server', 's1', { status: 'disabled' }),
        card('skill_runner', 'future', { status: 'unknown' }),
    ],
};

const sectionOf = (title: string) => screen.getByRole('heading', { name: title }).closest('section') as HTMLElement;
const onTabChange = vi.fn();
const open = async () => {
    render(<SystemOverviewPane onTabChange={onTabChange} />);
    await screen.findByTestId('system-overview-counters');
};

describe('SystemOverviewPane', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        gatewayClient.getSystemOverview.mockReset();
        gatewayClient.getSystemOverview.mockResolvedValue(RESPONSE);
    });

    afterEach(() => {
        cleanup();
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('shows each counter against its own label', async () => {
        await open();
        const counters = within(screen.getByTestId('system-overview-counters'));
        for (const [label, value] of [['Healthy', '4'], ['Degraded', '1'], ['Down', '2'], ['Unknown', '3'], ['Disabled', '5']]) {
            expect(counters.getByText(label).parentElement).toHaveTextContent(value);
        }
    });

    it('sorts cards into sections, with unknown kinds kept under infrastructure', async () => {
        await open();
        const infra = within(sectionOf('Infrastructure'));
        expect(infra.getByText('database-pg')).toBeInTheDocument();
        expect(infra.getByText('redis-r')).toBeInTheDocument();
        expect(infra.getByText('qdrant-q')).toBeInTheDocument();
        expect(infra.getByText('skill_runner-future')).toBeInTheDocument();
        expect(within(sectionOf('LLM Upstreams')).getByText('llm_upstream-m1')).toBeInTheDocument();
        expect(within(sectionOf('MCP Servers')).getByText('mcp_server-s1')).toBeInTheDocument();
        expect(infra.getByText('12 ms')).toBeInTheDocument();
        expect(infra.getByText('primary')).toBeInTheDocument();
    });

    it('explains an empty section instead of showing a blank box', async () => {
        gatewayClient.getSystemOverview.mockResolvedValue({ ...RESPONSE, cards: [] });
        await open();
        expect(screen.getByTestId('system-overview-group-empty-llm-upstreams')).toHaveTextContent(
            'No LLM models enabled for this tenant.',
        );
        expect(screen.getByTestId('system-overview-group-empty-mcp-servers')).toHaveTextContent(
            'No MCP servers registered for this tenant.',
        );
        expect(screen.getByTestId('system-overview-group-empty-infrastructure')).toBeInTheDocument();
    });

    it('a card with a route deep-links; one without is inert', async () => {
        await open();
        fireEvent.click(screen.getByTestId('system-overview-card-llm_upstream-m1'));
        expect(onTabChange).toHaveBeenCalledWith('ai-models');
        expect(screen.getByTestId('system-overview-card-database-pg')).toBeDisabled();
    });

    it('without a navigation handler even a routed card is inert', async () => {
        render(<SystemOverviewPane />);
        await screen.findByTestId('system-overview-counters');
        expect(screen.getByTestId('system-overview-card-llm_upstream-m1')).toBeDisabled();
    });

    it('announces maintenance only when it is on', async () => {
        await open();
        expect(screen.queryByTestId('system-overview-maintenance-banner')).toBeNull();
        cleanup();

        gatewayClient.getSystemOverview.mockResolvedValue({
            ...RESPONSE,
            maintenance: { mode: 'read_only', since: '2026-11-05T09:00:00Z' },
        });
        await open();
        const banner = screen.getByTestId('system-overview-maintenance-banner');
        expect(banner).toHaveTextContent('Maintenance: read_only');
        expect(banner).toHaveTextContent('since');
    });

    it('a failed first load offers Retry, which recovers', async () => {
        gatewayClient.getSystemOverview.mockRejectedValueOnce(new Error('cp down'));
        render(<SystemOverviewPane />);
        expect(await screen.findByTestId('system-overview-error')).toHaveTextContent('cp down');
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        expect(await screen.findByTestId('system-overview-counters')).toBeInTheDocument();
        expect(screen.queryByTestId('system-overview-error')).toBeNull();
    });

    it('Refresh fetches again and a failed refresh does not blank the grid', async () => {
        await open();
        gatewayClient.getSystemOverview.mockRejectedValueOnce(new Error('blip'));
        fireEvent.click(screen.getByTestId('system-overview-refresh'));
        await waitFor(() => expect(gatewayClient.getSystemOverview).toHaveBeenCalledTimes(2));
        expect(screen.getByTestId('system-overview-counters')).toBeInTheDocument();
        expect(screen.queryByTestId('system-overview-error')).toBeNull();
    });

    it('polls every 15s while visible, skips while hidden, and stops on unmount', async () => {
        vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
        const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
        const { unmount } = render(<SystemOverviewPane />);
        await screen.findByTestId('system-overview-counters');
        expect(gatewayClient.getSystemOverview).toHaveBeenCalledTimes(1);

        vi.advanceTimersByTime(15_000);
        await waitFor(() => expect(gatewayClient.getSystemOverview).toHaveBeenCalledTimes(2));

        visibility.mockReturnValue('hidden');
        vi.advanceTimersByTime(45_000);
        expect(gatewayClient.getSystemOverview).toHaveBeenCalledTimes(2);

        visibility.mockReturnValue('visible');
        unmount();
        vi.advanceTimersByTime(45_000);
        expect(gatewayClient.getSystemOverview).toHaveBeenCalledTimes(2);
    });
});

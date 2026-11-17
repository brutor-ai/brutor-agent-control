/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Delegation Graphs list: each run's graph links to the AI
 * Systems involved (the root and every system that served a segment), so an
 * operator can go from "this chain misbehaved" to the system's Composition.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const listA2ADelegationGraphs = vi.fn();
const getA2ADelegationStats = vi.fn();
const stubClient = { listA2ADelegationGraphs, getA2ADelegationStats };

vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ gatewayClient: stubClient }),
}));
vi.mock('../A2ADelegationGraphDetail', () => ({
    default: ({ rootTaskId, onBack }: { rootTaskId: string; onBack: () => void }) => (
        <div data-testid="graph-detail" data-root={rootTaskId}>
            <button onClick={onBack}>back to graphs</button>
        </div>
    ),
}));

import A2ADelegationGraphsTab from '../A2ADelegationGraphsTab';

const graph = (over: Record<string, unknown> = {}) => ({
    root_task_id: 'task-root-1',
    tenant_id: 't1',
    started_at: '2026-09-26T10:00:00Z',
    ended_at: '2026-09-26T10:00:05Z',
    duration_ms: 5000,
    total_steps: 3,
    max_depth: 2,
    inbound_steps: 1,
    outbound_steps: 2,
    success_steps: 3,
    failure_steps: 0,
    overall_success: true,
    root_card_name: 'fraud-screener-card',
    root_actor_agent_name: 'Scoring Agent',
    systems: [
        { id: 'sys-fraud', name: 'Fraud Screener', role: 'segment' },
        { id: 'sys-demo', name: 'Brutor Demo System', role: 'root' },
    ],
    ...over,
});

const setActiveTab = vi.fn();

beforeEach(() => {
    vi.clearAllMocks();
    getA2ADelegationStats.mockResolvedValue(null);
    listA2ADelegationGraphs.mockResolvedValue({
        graphs: [graph(), graph({ root_task_id: 'task-root-2', systems: [] })],
        total: 2,
        limit: 50,
        offset: 0,
    });
});

describe('A2ADelegationGraphsTab — AI System links', () => {
    it('lists the systems involved, root first, each linking to its Composition tab', async () => {
        render(<A2ADelegationGraphsTab setActiveTab={setActiveTab} />);
        const systems = await screen.findByTestId('graph-systems');
        const buttons = Array.from(systems.querySelectorAll('button'));
        expect(buttons.map((b) => b.getAttribute('data-testid'))).toEqual([
            'graph-system-sys-demo',
            'graph-system-sys-fraud',
        ]);
        expect(buttons[0]).toHaveTextContent('Brutor Demo System(root)');

        fireEvent.click(screen.getByTestId('graph-system-sys-fraud'));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-fraud:composition');
        // The system link does not also open the graph.
        expect(screen.queryByTestId('graph-detail')).toBeNull();
    });

    it('an unattributed graph shows no system strip', async () => {
        render(<A2ADelegationGraphsTab setActiveTab={setActiveTab} />);
        await screen.findByTestId('graph-systems');
        expect(screen.getAllByTestId('graph-systems')).toHaveLength(1);
    });
});

describe('A2ADelegationGraphsTab — list, filters and paging', () => {
    const STATS = {
        total_graphs: 2,
        total_steps: 6,
        success_rate: 0.875,
        failure_steps: 1,
        p50_duration_ms: 250,
        p95_duration_ms: 1500,
    };

    function lastListQuery(): unknown {
        const calls = listA2ADelegationGraphs.mock.calls;
        return calls[calls.length - 1][0];
    }

    function lastStatsQuery(): { since?: string } {
        const calls = getA2ADelegationStats.mock.calls;
        return calls[calls.length - 1][0] as { since?: string };
    }

    async function load() {
        render(<A2ADelegationGraphsTab setActiveTab={setActiveTab} />);
        await screen.findByTestId('graph-systems');
    }

    beforeEach(() => {
        getA2ADelegationStats.mockResolvedValue(STATS);
    });

    it('asks for the last 24 hours first and shows the KPIs', async () => {
        const before = Date.now();
        await load();

        const since = lastStatsQuery().since ?? '';
        const age = before - new Date(since).getTime();
        expect(age).toBeGreaterThan(23.9 * 3600_000);
        expect(age).toBeLessThan(24.1 * 3600_000);
        expect(lastListQuery()).toEqual({ limit: 50, offset: 0, since, success: undefined });

        expect(screen.getByText('2 graphs, 6 steps')).toBeInTheDocument();
        expect(screen.getByText('87.5%')).toBeInTheDocument();
        expect(screen.getByText('250 ms')).toBeInTheDocument();
        expect(screen.getByText('1.50 s')).toBeInTheDocument();
    });

    it('renders the pagination range with a real dash, not an escape sequence', async () => {
        await load();
        expect(screen.getByText(/Showing 1–2 of/)).toBeInTheDocument();
        expect(document.body.textContent).not.toMatch(/\\u20/);
    });

    it('describes a row: who called whom, chain length, direction', async () => {
        await load();
        expect(screen.getAllByText('Scoring Agent')).toHaveLength(2);
        expect(screen.getAllByText('fraud-screener-card')).toHaveLength(2);
        expect(screen.getAllByText('3-step chain')).toHaveLength(2);
        expect(screen.getAllByText('depth 2')).toHaveLength(2);
        expect(screen.getAllByText('1 inbound')).toHaveLength(2);
        expect(screen.getAllByText('2 outbound')).toHaveLength(2);
        expect(screen.getAllByTitle('Success')).toHaveLength(2);
    });

    it('falls back when the caller and callee are not named, and does not call one step a chain', async () => {
        listA2ADelegationGraphs.mockResolvedValue({
            graphs: [graph({
                overall_success: false,
                total_steps: 1,
                root_actor_agent_name: null,
                root_card_name: null,
                root_card_id: null,
                systems: [],
            })],
            total: 1,
        });
        render(<A2ADelegationGraphsTab />);

        expect(await screen.findByText('caller')).toBeInTheDocument();
        expect(screen.getByText('callee')).toBeInTheDocument();
        expect(screen.getByText('1 step')).toBeInTheDocument();
        expect(screen.queryByText(/-step chain/)).toBeNull();
        expect(screen.getByTitle('Failure')).toBeInTheDocument();
    });

    it('shows systems as plain text when there is nowhere to navigate', async () => {
        render(<A2ADelegationGraphsTab />);
        const systems = await screen.findByTestId('graph-systems');
        expect(systems.querySelectorAll('button')).toHaveLength(0);
        expect(systems).toHaveTextContent('Fraud Screener');
    });

    it('filters by outcome', async () => {
        await load();
        fireEvent.click(screen.getByRole('button', { name: 'Failure' }));
        await waitFor(() => expect(lastListQuery()).toMatchObject({ success: false }));

        await screen.findByTestId('graph-systems');
        fireEvent.click(screen.getByRole('button', { name: 'Success' }));
        await waitFor(() => expect(lastListQuery()).toMatchObject({ success: true }));

        await screen.findByTestId('graph-systems');
        fireEvent.click(screen.getByRole('button', { name: 'All' }));
        await waitFor(() => expect(lastListQuery()).toMatchObject({ success: undefined }));
    });

    it.each([
        ['hour', 1],
        ['week', 7 * 24],
    ])('changes the window to the last %s for list and KPIs', async (value, hours) => {
        await load();
        const before = Date.now();
        fireEvent.change(screen.getByLabelText('Time window'), { target: { value } });

        await waitFor(() => {
            const age = before - new Date(lastStatsQuery().since ?? 0).getTime();
            expect(age).toBeGreaterThan((hours - 0.1) * 3600_000);
            expect(age).toBeLessThan((hours + 0.1) * 3600_000);
        });
    });

    it('sends no lower bound for all time', async () => {
        await load();
        fireEvent.change(screen.getByLabelText('Time window'), { target: { value: 'all' } });
        await waitFor(() => expect(lastStatsQuery().since).toBeUndefined());
        expect(lastListQuery()).toMatchObject({ since: undefined });
    });

    it('pages forward and back, and resets to the first page when the page size changes', async () => {
        listA2ADelegationGraphs.mockResolvedValue({ graphs: [graph()], total: 120 });
        render(<A2ADelegationGraphsTab />);
        await screen.findByTestId('graph-systems');
        expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Prev' }).disabled).toBe(true);

        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        await waitFor(() => expect(lastListQuery()).toMatchObject({ offset: 50 }));
        await screen.findByText(/Showing 51–51 of/);

        fireEvent.click(screen.getByRole('button', { name: 'Prev' }));
        await waitFor(() => expect(lastListQuery()).toMatchObject({ offset: 0 }));
        await screen.findByText(/Showing 1–1 of/);

        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        await waitFor(() => expect(lastListQuery()).toMatchObject({ offset: 50 }));
        await screen.findByText(/Showing 51–51 of/);

        fireEvent.change(screen.getByLabelText('Page size'), { target: { value: '100' } });
        await waitFor(() => expect(lastListQuery()).toMatchObject({ limit: 100, offset: 0 }));
    });

    it('stops Next at the last page', async () => {
        await load();
        expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Next' }).disabled).toBe(true);
    });

    it('shows the empty state', async () => {
        listA2ADelegationGraphs.mockResolvedValue({ graphs: [], total: 0 });
        render(<A2ADelegationGraphsTab />);
        expect(await screen.findByText('No delegation graphs in this window')).toBeInTheDocument();
        expect(screen.getByText(/Showing 0–0 of/)).toBeInTheDocument();
    });

    it('shows a load failure', async () => {
        listA2ADelegationGraphs.mockRejectedValue(new Error('gateway down'));
        render(<A2ADelegationGraphsTab />);
        expect(await screen.findByText('gateway down')).toBeInTheDocument();
    });

    it('refreshes', async () => {
        await load();
        const calls = listA2ADelegationGraphs.mock.calls.length;
        fireEvent.click(screen.getByTitle('Refresh'));
        await waitFor(() => expect(listA2ADelegationGraphs.mock.calls.length).toBe(calls + 1));
    });

    it('opens a graph from its row and comes back', async () => {
        await load();
        fireEvent.click(screen.getByText('task-root-1'));
        expect(screen.getByTestId('graph-detail').getAttribute('data-root')).toBe('task-root-1');

        fireEvent.click(screen.getByText('back to graphs'));
        expect(screen.queryByTestId('graph-detail')).toBeNull();
        expect(screen.getByText('Delegation Graphs')).toBeInTheDocument();
    });
});

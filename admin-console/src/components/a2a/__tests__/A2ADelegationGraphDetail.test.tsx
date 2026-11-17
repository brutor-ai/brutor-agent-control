/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Drill-down into one A2A delegation graph. This is the screen an operator
 * opens when an agent-to-agent chain misbehaved, so what it must never do is
 * lose a step.
 *
 * The tree builder is unit-tested separately; what is checked here is the
 * rendering contract that surrounds it:
 *
 *   - every step reaches the page, including one whose parent fell off the far
 *     side of log retention — that step is shown and badged `detached`, not
 *     dropped, because a silently missing row reads as "the call never
 *     happened";
 *   - a failed hop shows its status and error, not just a red tint;
 *   - depth drives the indent, so the chain reads top-to-bottom;
 *   - a refresh that fails leaves the already-loaded graph on screen rather
 *     than replacing it with an empty panel.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const getA2ADelegationGraph = vi.fn();
const stubClient = { getA2ADelegationGraph };

vi.mock('../../../hooks/useAuth', () => ({
    useAuth: () => ({ gatewayClient: stubClient }),
}));

import A2ADelegationGraphDetail from '../A2ADelegationGraphDetail';

const step = (over: Record<string, unknown> = {}) => ({
    proxy_log_id: 'log-1',
    direction: 'inbound',
    depth: 0,
    current_task_id: 'task-root',
    parent_task_id: null,
    actor_agent_name: 'orchestrator',
    card_name: 'planner',
    endpoint: 'message:send',
    target_url: null,
    http_status_code: 200,
    success: true,
    duration_ms: 120,
    timestamp: '2026-06-01T10:00:00Z',
    error_message: null,
    ...over,
});

const GRAPH = {
    tenant_id: 'acme',
    total_steps: 2,
    max_depth: 1,
    steps: [
        step(),
        step({
            proxy_log_id: 'log-2',
            direction: 'outbound',
            depth: 1,
            current_task_id: 'task-child',
            parent_task_id: 'task-root',
            actor_agent_name: 'planner',
            card_name: 'researcher',
            endpoint: 'message:stream',
            timestamp: '2026-06-01T10:00:05Z',
        }),
    ],
};

beforeEach(() => {
    vi.clearAllMocks();
    getA2ADelegationGraph.mockResolvedValue(GRAPH);
});

describe('A2ADelegationGraphDetail', () => {
    it('loads the graph for the root task it was given', async () => {
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        await waitFor(() =>
            expect(getA2ADelegationGraph).toHaveBeenCalledWith('task-root')
        );
        expect(await screen.findByText('root task-root')).toBeInTheDocument();
    });

    it('summarises the graph shape before the rows', async () => {
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        await screen.findByText('Steps');
        expect(screen.getByText('Max depth')).toBeInTheDocument();
        expect(screen.getByText('acme')).toBeInTheDocument();
    });

    it('renders one row per hop with caller, callee and endpoint', async () => {
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        await screen.findByText('Steps');

        expect(screen.getAllByRole('listitem')).toHaveLength(2);
        expect(screen.getByText('orchestrator')).toBeInTheDocument();
        expect(screen.getByText('researcher')).toBeInTheDocument();
        expect(screen.getByText('message:stream')).toBeInTheDocument();
    });

    it('indents by depth so the chain reads as a tree', async () => {
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        await screen.findByText('Steps');

        const [root, child] = screen.getAllByRole('listitem');
        expect(root.style.marginLeft).toBe('0px');
        expect(child.style.marginLeft).toBe('16px');
    });

    it('keeps a step whose parent fell outside retention, badged detached', async () => {
        // Dropping it would read as "the call never happened".
        getA2ADelegationGraph.mockResolvedValue({
            ...GRAPH,
            total_steps: 2,
            steps: [
                GRAPH.steps[0],
                step({
                    proxy_log_id: 'log-3',
                    depth: 2,
                    current_task_id: 'task-orphan',
                    parent_task_id: 'task-long-gone',
                    card_name: 'archiver',
                    timestamp: '2026-06-01T10:00:09Z',
                }),
            ],
        });

        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        await screen.findByText('Steps');

        expect(screen.getByText('archiver')).toBeInTheDocument();
        // The badge, not the explanatory <em>detached</em> in the info panel.
        const badge = screen.getByTitle('Parent step was not found in this graph');
        expect(badge).toHaveTextContent('detached');
    });

    it('shows the failure detail on a hop that errored', async () => {
        getA2ADelegationGraph.mockResolvedValue({
            ...GRAPH,
            total_steps: 1,
            max_depth: 0,
            steps: [
                step({
                    success: false,
                    http_status_code: 502,
                    error_message: 'upstream refused connection',
                }),
            ],
        });

        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        expect(await screen.findByText('502')).toBeInTheDocument();
        expect(screen.getByText('upstream refused connection')).toBeInTheDocument();
        expect(screen.getByText('502')).toHaveClass('text-red-600');
    });

    it('says "No steps" rather than rendering an empty card body', async () => {
        getA2ADelegationGraph.mockResolvedValue({
            ...GRAPH,
            total_steps: 0,
            max_depth: 0,
            steps: [],
        });
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        expect(await screen.findByText('No steps')).toBeInTheDocument();
    });

    it('surfaces a load failure', async () => {
        getA2ADelegationGraph.mockRejectedValue(new Error('graph not found'));
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        expect(await screen.findByText('graph not found')).toBeInTheDocument();
        expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
    });

    it('keeps the loaded graph on screen when a refresh fails', async () => {
        // Replacing a rendered graph with an error panel would destroy the
        // evidence the operator was reading.
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        await screen.findByText('orchestrator');

        getA2ADelegationGraph.mockRejectedValue(new Error('timeout'));
        fireEvent.click(screen.getByTitle('Refresh'));

        expect(await screen.findByText('timeout')).toBeInTheDocument();
        expect(screen.getByText('orchestrator')).toBeInTheDocument();
    });

    it('clears a stale error once a refresh succeeds', async () => {
        getA2ADelegationGraph.mockRejectedValueOnce(new Error('boom'));
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />);
        await screen.findByText('boom');

        fireEvent.click(screen.getByTitle('Refresh'));
        expect(await screen.findByText('orchestrator')).toBeInTheDocument();
        expect(screen.queryByText('boom')).not.toBeInTheDocument();
    });

    it('goes back to the list', async () => {
        const onBack = vi.fn();
        render(<A2ADelegationGraphDetail rootTaskId="task-root" onBack={onBack} />);
        await screen.findByText('Steps');

        fireEvent.click(screen.getByTitle('Back to list'));
        expect(onBack).toHaveBeenCalled();
    });

    it('reloads when the operator opens a different graph', async () => {
        const { rerender } = render(
            <A2ADelegationGraphDetail rootTaskId="task-root" onBack={vi.fn()} />
        );
        await screen.findByText('Steps');

        rerender(<A2ADelegationGraphDetail rootTaskId="task-other" onBack={vi.fn()} />);
        await waitFor(() =>
            expect(getA2ADelegationGraph).toHaveBeenLastCalledWith('task-other')
        );
    });
});

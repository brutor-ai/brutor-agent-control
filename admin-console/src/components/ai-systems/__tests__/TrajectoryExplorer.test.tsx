/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Trajectory Explorer.
 *
 * These tests are written against the *honesty rules*, not the layout: the
 * vocabulary fix is only worth shipping if the UI refuses to blur the same
 * two distinctions the ledger refuses to blur —
 *
 *   1. a run with no asserted phases is `unphased`, never "1 step";
 *   2. an inferred turn is marked as inferred, never shown as reported;
 *   3. the consequence pane states co-occurrence, never causation.
 *
 * A layout change should not break them; a change that quietly upgrades a
 * guess to a report should.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TrajectoryExplorer from '../TrajectoryExplorer';

const mockTrajectory = vi.fn();
const mockConsequences = vi.fn();
const mockSearch = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchRunTrajectory: mockTrajectory,
        fetchRunConsequences: mockConsequences,
        searchActions: mockSearch,
    }),
}));

const phased = {
    run_id: 'run-1',
    root_task_id: 'root-1',
    action_count: 9,
    turn_count: 3,
    turn_attribution: 'client_asserted',
    step_count: 2,
    unphased: false,
    steps: [
        { step_id: 's1', step_name: 'research', turns: 2, actions: 6, cost_usd: 0.12, tokens: 900 },
        { step_id: 's2', step_name: 'settle', turns: 1, actions: 3, cost_usd: 0.04, tokens: 300 },
    ],
    turns: [
        {
            turn_id: 't1', turn_seq: 1, inferred_turn: 1, step_id: 's1', step_name: 'research',
            action_count: 4, tool_action_count: 2, opened_by_model: 'gpt-5.2',
            tools: ['crm_get_deal', 'crm_list_pipeline'], cost_usd: 0.08, blocked: false,
        },
    ],
};

const unphased = {
    ...phased,
    step_count: 0,
    unphased: true,
    steps: [],
    turn_attribution: 'inferred',
    turns: [
        {
            turn_id: null, turn_seq: null, inferred_turn: 2, step_id: null, step_name: null,
            action_count: 3, tool_action_count: 1, opened_by_model: 'gpt-5.2',
            tools: ['crm_get_deal'], cost_usd: 0.02, blocked: false,
        },
    ],
};

beforeEach(() => {
    vi.clearAllMocks();
    mockConsequences.mockResolvedValue({ run_id: 'run-1', within: 10, pairs: [] });
    mockSearch.mockResolvedValue({ actions: [], total: 0 });
});

describe('TrajectoryExplorer', () => {
    it('shows asserted phases as selectable steps', async () => {
        mockTrajectory.mockResolvedValue(phased);
        render(<TrajectoryExplorer initialRunId="run-1" />);

        const steps = await screen.findByTestId('trajectory-steps');
        expect(steps).toHaveTextContent('research');
        expect(steps).toHaveTextContent('settle');
        expect(screen.queryByTestId('unphased')).toBeNull();
    });

    it('says "unphased" rather than inventing a single phase', async () => {
        mockTrajectory.mockResolvedValue(unphased);
        render(<TrajectoryExplorer initialRunId="run-1" />);

        // The distinction the ledger makes: no assertion is not "one phase".
        expect(await screen.findByTestId('unphased')).toBeInTheDocument();
        expect(screen.queryByTestId('trajectory-steps')).toBeNull();
        expect(screen.queryByText(/\b1 steps?\b/)).toBeNull();
    });

    it('marks an inferred turn as inferred', async () => {
        mockTrajectory.mockResolvedValue(unphased);
        render(<TrajectoryExplorer initialRunId="run-1" />);

        const turns = await screen.findByTestId('trajectory-turns');
        // `~` is the marker; without it an inferred ordinal reads as reported.
        expect(turns).toHaveTextContent('~2');
    });

    it('does not present an asserted turn as inferred', async () => {
        mockTrajectory.mockResolvedValue(phased);
        render(<TrajectoryExplorer initialRunId="run-1" />);

        const turns = await screen.findByTestId('trajectory-turns');
        expect(turns).not.toHaveTextContent('~1');
    });

    it('states co-occurrence, not causation, on the consequence pane', async () => {
        mockTrajectory.mockResolvedValue(phased);
        mockConsequences.mockResolvedValue({
            run_id: 'run-1',
            within: 10,
            pairs: [
                {
                    trip_action_no: 2, trip_check: 'pii', trip_step_name: 'research',
                    after_action_no: 4, after_capability: 'crm_update_deal_stage',
                    after_type: 'mcp', actions_apart: 2,
                },
            ],
        });
        render(<TrajectoryExplorer initialRunId="run-1" />);

        const pane = await screen.findByTestId('trajectory-consequences');
        expect(pane).toHaveTextContent(/co-occurrence/i);
        expect(pane).toHaveTextContent(/not.*causal/i);
        expect(pane).toHaveTextContent('crm_update_deal_stage');
        expect(pane).not.toHaveTextContent(/caused/i);
    });

    it('filters the trajectory to one step when a phase is clicked', async () => {
        mockTrajectory.mockResolvedValue(phased);
        render(<TrajectoryExplorer initialRunId="run-1" />);

        const steps = await screen.findByTestId('trajectory-steps');
        await userEvent.click(within(steps).getByText(/research/));

        await waitFor(() =>
            expect(mockTrajectory).toHaveBeenLastCalledWith('run-1', 's1'),
        );
    });

    it('passes the AI System through to fleet search', async () => {
        mockTrajectory.mockResolvedValue(phased);
        render(<TrajectoryExplorer aiSystemId="sys-1" />);

        await userEvent.click(screen.getByTestId('trajectory-search'));
        await waitFor(() =>
            expect(mockSearch).toHaveBeenCalledWith(
                expect.objectContaining({ ai_system_id: 'sys-1' }),
            ),
        );
    });
});

describe('two-column layout', () => {
    const hit = {
        correlation_id: 'c-1',
        timestamp: '2026-08-24T18:08:36Z',
        proxy_type: 'mcp',
        mcp_capability_name: 'crm_get_pipeline',
        step_name: null,
        step_id: null,
        root_task_id: 'nordica-seed-run-00134434',
        guardrail_blocked: false,
        argument_policy_denied: false,
    };

    it('puts finding and inspecting side by side, 60/40', async () => {
        // Stacked, the answer sat a full result-table's scroll below the
        // question; the operator's loop is find → inspect → find again.
        render(<TrajectoryExplorer />);
        const shell = await screen.findByTestId('trajectory-explorer');
        expect(shell.className).toContain('lg:grid-cols-5');

        const find = screen.getByText('Find actions').closest('section');
        const inspect = screen.getByTestId('trajectory-inspect-pane');
        expect(find?.className).toContain('lg:col-span-3'); // 3/5 = 60%
        expect(inspect.className).toContain('lg:col-span-2'); // 2/5 = 40%
        // The right pane stays put while the left list scrolls.
        expect(inspect.className).toContain('lg:sticky');
    });

    it('a result loads the run into the right pane and marks the row', async () => {
        mockSearch.mockResolvedValue({ actions: [hit], total: 1 });
        mockTrajectory.mockResolvedValue(unphased);
        render(<TrajectoryExplorer />);

        const open = await screen.findByTestId('open-trajectory-0');
        fireEvent.click(open);

        await waitFor(() =>
            expect(mockTrajectory).toHaveBeenCalledWith(
                'nordica-seed-run-00134434',
                undefined,
            )
        );
        // The two columns must read as connected: the row whose run is
        // loaded on the right is highlighted on the left.
        await waitFor(() =>
            expect(open.closest('tr')?.className).toContain('bg-primary-50')
        );
    });

    it('drops the run column — the id lives in the pane that shows the run', async () => {
        mockSearch.mockResolvedValue({ actions: [hit], total: 1 });
        render(<TrajectoryExplorer />);
        await screen.findByTestId('open-trajectory-0');
        // A 25-character run id twice over is what made the narrow column
        // unreadable; it survives as the button's tooltip.
        const headers = screen.getAllByRole('columnheader').map((h) => h.textContent);
        expect(headers).not.toContain('Run');
        expect(screen.getByTestId('open-trajectory-0')).toHaveAttribute(
            'title',
            'nordica-seed-run-00134434',
        );
    });
});

describe('observed-vocabulary prefill', () => {
    const recent = {
        total: 2,
        actions: [
            {
                correlation_id: 'c1', timestamp: '2026-08-20T10:00:00Z', proxy_type: 'mcp',
                mcp_capability_name: 'crm_update_deal_stage', llm_model_used: null,
                step_name: 'settle', root_task_id: 'root-77',
                guardrail_blocked: false, argument_policy_denied: false, step_id: 's9',
            },
            {
                correlation_id: 'c2', timestamp: '2026-08-20T09:00:00Z', proxy_type: 'llm',
                mcp_capability_name: null, llm_model_used: 'gpt-5.2',
                step_name: 'research', root_task_id: 'root-42',
                guardrail_blocked: false, argument_policy_denied: false, step_id: null,
            },
        ],
    };

    it('opens showing recent actions instead of an empty form', async () => {
        mockSearch.mockResolvedValue(recent);
        render(<TrajectoryExplorer aiSystemId="sys-1" />);
        // No Search click — the mount sweep fills the table.
        expect(await screen.findByText('crm_update_deal_stage')).toBeInTheDocument();
        expect(mockSearch).toHaveBeenCalledWith(
            expect.objectContaining({ ai_system_id: 'sys-1', limit: 200 })
        );
    });

    it('seeds datalists from what was actually observed', async () => {
        mockSearch.mockResolvedValue(recent);
        const { container } = render(<TrajectoryExplorer aiSystemId="sys-1" />);
        await screen.findByText('crm_update_deal_stage');
        const caps = container.querySelector('#trajectory-observed-caps');
        expect(
            caps?.querySelector('option[value="crm_update_deal_stage"]')
        ).not.toBeNull();
        const steps = container.querySelector('#trajectory-observed-steps');
        expect(steps?.querySelector('option[value="research"]')).not.toBeNull();
        const runs = container.querySelector('#trajectory-recent-runs');
        expect(runs?.querySelector('option[value="root-77"]')).not.toBeNull();
    });

    it('a manual search result is not overwritten by the mount sweep', async () => {
        // The sweep resolves late, after the operator already searched.
        let resolveSweep: (v: unknown) => void = () => undefined;
        mockSearch
            .mockReturnValueOnce(new Promise((r) => { resolveSweep = r; }))
            .mockResolvedValueOnce({ total: 1, actions: [recent.actions[1]] });
        render(<TrajectoryExplorer aiSystemId="sys-1" />);
        await userEvent.click(screen.getByTestId('trajectory-search'));
        expect(await screen.findByText('gpt-5.2')).toBeInTheDocument();
        resolveSweep(recent);
        await waitFor(() => {
            expect(screen.queryByText('crm_update_deal_stage')).toBeNull();
        });
    });

    it('explains what the tab is when the help marker is clicked', async () => {
        mockSearch.mockResolvedValue({ actions: [], total: 0 });
        render(<TrajectoryExplorer aiSystemId="sys-1" />);
        const tips = screen.getAllByTestId('help-tip');
        await userEvent.click(tips[0]);
        expect(
            await screen.findByText(/did it do X/)
        ).toBeInTheDocument();
    });
});

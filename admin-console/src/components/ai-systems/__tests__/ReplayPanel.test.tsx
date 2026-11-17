/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockStatus = vi.fn();
const mockBuild = vi.fn();
const mockRun = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchReplayStatus: mockStatus,
        buildReplaySuite: mockBuild,
        requestReplayRun: mockRun,
    }),
}));

import ReplayPanel from '../ReplayPanel';

function noSuite() {
    return {
        bound: false,
        replayable: true,
        enabled: true,
        case_count: 0,
        coverage: {},
        gate_state: 'no_suite' as const,
    };
}

function withSuite(overrides: Record<string, unknown> = {}) {
    return {
        suite_id: 'suite-1',
        suite_name: 'pre-v5-promotion',
        bound: true,
        replayable: true,
        enabled: true,
        pass_threshold: 0.9,
        case_count: 30,
        coverage: { completed: 18, errored: 7, exhausted: 5 },
        last_run: null,
        gate_state: 'never_run' as const,
        ...overrides,
    };
}

function finishedRun(overrides: Record<string, unknown> = {}) {
    return {
        id: 'rr-1',
        suite_id: 'suite-1',
        ai_system_id: 'sys-1',
        status: 'completed',
        started_at: '2026-07-29T12:00:00Z',
        finished_at: '2026-07-29T12:01:00Z',
        total_cases: 30,
        matched: 27,
        shape_changed: 1,
        policy_changed: 1,
        outcome_changed: 1,
        failed: 0,
        skipped: 0,
        pass_rate: 0.9,
        critical_outcome_changed: 0,
        is_finished: true,
        ...overrides,
    };
}

describe('ReplayPanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockStatus.mockResolvedValue(noSuite());
    });

    it('explains what replay is when nothing is bound, rather than showing an empty box', async () => {
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('replay-gate-state')).toBeInTheDocument()
        );
        expect(screen.getByText(/No replay suite bound/)).toBeInTheDocument();
        expect(screen.getByText(/Nothing gates promotion yet/)).toBeInTheDocument();
    });

    it('does not offer to run a replay when no suite exists', async () => {
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockStatus).toHaveBeenCalled());
        expect(screen.queryByTestId('run-replay')).not.toBeInTheDocument();
    });

    it('leaves gate binding off by default', async () => {
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockStatus).toHaveBeenCalled());
        fireEvent.click(screen.getByTestId('toggle-build'));

        // The API defaults this off so an operator reads the coverage report
        // before a suite starts deciding whether releases ship. The UI must
        // not quietly re-enable it.
        expect(screen.getByTestId('bind-to-gate')).not.toBeChecked();

        fireEvent.click(screen.getByTestId('build-suite'));
        await waitFor(() => expect(mockBuild).toHaveBeenCalled());
        expect(mockBuild.mock.calls[0][1].bind_to_gate).toBe(false);
    });

    it('shows coverage gaps as prominently as coverage', async () => {
        // The finding this guards: a gate that reports a clean pass while
        // silently covering none of the errored runs is worse than no gate,
        // because it converts absent evidence into apparent assurance.
        mockBuild.mockResolvedValue({
            suite_id: 'suite-1',
            name: 's',
            eligible_runs: 200,
            cases_created: 30,
            coverage: { completed: 30 },
            coverage_gaps: ['errored', 'exhausted'],
            bound_to_gate: false,
            warnings: [],
        });
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockStatus).toHaveBeenCalled());
        fireEvent.click(screen.getByTestId('toggle-build'));
        fireEvent.click(screen.getByTestId('build-suite'));

        await waitFor(() =>
            expect(screen.getByTestId('coverage-gaps')).toBeInTheDocument()
        );
        expect(screen.getByText(/Not covered: errored, exhausted/)).toBeInTheDocument();
        expect(
            screen.getByText(/A pass from this suite says nothing about them/)
        ).toBeInTheDocument();
    });

    it('reports the verdict breakdown, not just a pass rate', async () => {
        mockStatus.mockResolvedValue(
            withSuite({ gate_state: 'passing', last_run: finishedRun() })
        );
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('last-run')).toBeInTheDocument());

        // Outcome-changed is the release-relevant kind and must be visible
        // separately — a single changed authorization outcome matters more
        // than a 90% pass rate.
        expect(screen.getByText('Outcome changed')).toBeInTheDocument();
        expect(screen.getByText('Shape changed')).toBeInTheDocument();
        expect(screen.getByText('Skipped')).toBeInTheDocument();
        expect(screen.getByText('90.0%')).toBeInTheDocument();
    });

    it('shows a run still in flight as in progress, not as a failed replay', async () => {
        mockStatus.mockResolvedValue(
            withSuite({
                gate_state: 'running',
                last_run: finishedRun({
                    status: 'running',
                    finished_at: null,
                    is_finished: false,
                    matched: 0,
                    shape_changed: 0,
                    policy_changed: 0,
                    outcome_changed: 0,
                    pass_rate: null,
                }),
            })
        );
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByText('Replay in progress')).toBeInTheDocument());
        expect(screen.queryByText('Last replay did not pass')).toBeNull();
    });

    it('calls out a critical outcome change above the pass rate', async () => {
        mockStatus.mockResolvedValue(
            withSuite({
                gate_state: 'failing',
                last_run: finishedRun({ critical_outcome_changed: 2, pass_rate: 0.97 }),
            })
        );
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('critical-change')).toBeInTheDocument()
        );
        expect(screen.getByText(/outweighs a good pass rate/)).toBeInTheDocument();
        // A count, not a flag: `0 && …` would render a literal 0 on the page.
        expect(screen.getByText('2')).toBeInTheDocument();
    });

    it('surfaces why a suite is not replayable instead of just disabling the button', async () => {
        mockStatus.mockResolvedValue(
            withSuite({
                gate_state: 'not_replayable',
                replayable: false,
                not_replayable_reason: 'Recorded steps predate the current contract schema',
            })
        );
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByTestId('not-replayable-reason')).toBeInTheDocument()
        );
        expect(screen.getByTestId('run-replay')).toBeDisabled();
    });

    it('says a requested run is queued rather than implying a verdict', async () => {
        // The control plane writes the request row; gateway-core executes it.
        // Reporting anything but "queued" on the click would be a lie.
        mockStatus.mockResolvedValue(withSuite());
        mockRun.mockResolvedValue({ replay_run_id: 'rr-9', status: 'running', note: '' });
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(screen.getByTestId('run-replay')).toBeInTheDocument());

        fireEvent.click(screen.getByTestId('run-replay'));
        await waitFor(() => expect(screen.getByTestId('run-queued')).toBeInTheDocument());
        expect(mockRun).toHaveBeenCalledWith('sys-1', 'suite-1');
    });

    it('stops polling once the run reports itself finished', async () => {
        mockStatus.mockResolvedValue(withSuite());
        mockRun.mockResolvedValue({ replay_run_id: 'rr-9', status: 'running', note: '' });
        render(<ReplayPanel aiSystemId="sys-1" pollMs={20} />);
        await waitFor(() => expect(screen.getByTestId('run-replay')).toBeInTheDocument());

        fireEvent.click(screen.getByTestId('run-replay'));
        await waitFor(() => expect(screen.getByTestId('run-queued')).toBeInTheDocument());

        // Terminate on the run's own `is_finished`, not on a status string, so
        // a state this UI has never heard of still ends the poll.
        mockStatus.mockResolvedValue(
            withSuite({ gate_state: 'passing', last_run: finishedRun() })
        );
        await waitFor(() =>
            expect(screen.queryByTestId('run-queued')).not.toBeInTheDocument()
        );

        const callsWhenDone = mockStatus.mock.calls.length;
        await new Promise((r) => setTimeout(r, 120));
        expect(mockStatus.mock.calls.length).toBe(callsWhenDone);
    });

    it('surfaces an API failure instead of rendering an empty panel', async () => {
        mockStatus.mockRejectedValue(new Error('control plane unreachable'));
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() =>
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument()
        );
    });

    it('lets an unbound suite be reviewed and run, and says it is not gating', async () => {
        // The API has no bind endpoint — binding happens only at build time —
        // so an operator who left the box unticked must still be able to read
        // the suite and run it. Without this, "review the coverage before you
        // bind" is a dead end: the run executes and its verdict is invisible.
        mockBuild.mockResolvedValue({
            suite_id: 'suite-new',
            name: 's',
            eligible_runs: 176,
            cases_created: 30,
            coverage: { 'terminal:completed': 12 },
            coverage_gaps: [],
            bound_to_gate: false,
            warnings: [],
        });
        mockRun.mockResolvedValue({ replay_run_id: 'rr-1', status: 'running', note: '' });
        render(<ReplayPanel aiSystemId="sys-1" />);
        await waitFor(() => expect(mockStatus).toHaveBeenCalled());
        fireEvent.click(screen.getByTestId('toggle-build'));

        // After building, the panel inspects the new suite by id — `bound`
        // stays false because the gate still reads nothing.
        mockStatus.mockResolvedValue(
            withSuite({ suite_id: 'suite-new', bound: false, gate_state: 'never_run' })
        );
        fireEvent.click(screen.getByTestId('build-suite'));

        await waitFor(() => expect(screen.getByTestId('not-bound-note')).toBeInTheDocument());
        expect(mockStatus).toHaveBeenLastCalledWith('sys-1', 'suite-new');

        fireEvent.click(screen.getByTestId('run-replay'));
        await waitFor(() => expect(mockRun).toHaveBeenCalledWith('sys-1', 'suite-new'));
    });
});

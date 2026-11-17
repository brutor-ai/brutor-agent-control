/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Tests for DriftPanel.
 *
 * The properties worth defending are all about what the operator reads
 * first:
 *
 *   - the **cause** leads, not the statistic;
 *   - an unexplained finding says so rather than inventing a reason;
 *   - "still learning" is visibly different from "no problems";
 *   - closing asks WHICH fact it is — the kinds live in the
 *     close dialog (ResolveDriftDialog.test.tsx); "Investigating" stays a
 *     separate, non-closing action.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';

const mockDrift = vi.fn();
const mockBaselines = vi.fn();
const mockAck = vi.fn();
const mockClose = vi.fn();
const mockEpochs = vi.fn();
const mockWindows = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemDrift: mockDrift,
        fetchAISystemBaselines: mockBaselines,
        fetchAISystemBaselineEpochs: mockEpochs,
        fetchAISystemIncidentWindows: mockWindows,
        acknowledgeDriftEvent: mockAck,
        closeDriftEvent: mockClose,
    }),
}));

import DriftPanel from '../DriftPanel';
import type { DriftEvent } from '../../../lib/aiSystemTypes';

function event(overrides: Partial<DriftEvent> = {}): DriftEvent {
    return {
        id: 'drift-1',
        ai_system_id: 'sys-1',
        drift_class: 'behavioural',
        metric_key: 'action_count',
        detected_at: '2026-07-14T15:00:00Z',
        change_point_at: '2026-07-14T14:00:00Z',
        baseline_value: 5,
        observed_value: 14,
        effect_size: 8.4,
        direction: 'up',
        severity: 'high',
        attributed_cause: 'model_change',
        cause_confidence: 0.85,
        cause_evidence: {
            summary:
                'The model serving this system changed at 2026-07-14T13:52:00Z.',
        },
        correlated_metrics: [],
        response_taken: {},
        status: 'active',
        is_open: true,
        is_explained: true,
        ...overrides,
    };
}

function list(events: DriftEvent[]) {
    return {
        events,
        total: events.length,
        open_count: events.filter((e) => e.is_open).length,
        unexplained_count: events.filter((e) => e.is_open && !e.is_explained).length,
    };
}

const readyBaselines = {
    baselines: [
        { id: 'b1', ai_system_id: 'sys-1', metric_key: 'action_count', bucket: 'all',
          distribution: {}, sample_count: 120, state: 'ready' as const, is_ready: true },
    ],
    total: 1,
    learning: 0,
};

describe('DriftPanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockDrift.mockResolvedValue(list([event()]));
        mockBaselines.mockResolvedValue(readyBaselines);
        mockAck.mockResolvedValue(event({ status: 'acknowledged' }));
        mockClose.mockResolvedValue({ ...event({ status: 'resolved', is_open: false }), seal: { sealed: true } });
        mockEpochs.mockResolvedValue({ epochs: [], total: 0 });
        mockWindows.mockResolvedValue({ windows: [], total: 0 });
    });

    it('leads with what shifted, in words; the cause is a labeled line', async () => {
        render(<DriftPanel aiSystemId="sys-1" />);
        const title = await screen.findByTestId('drift-title');
        expect(title).toHaveTextContent('Actions per run shifted up');
        // Raw slug stays out of the headline but greppable in the detail.
        expect(title).not.toHaveTextContent('action_count');
        expect(screen.getByTestId('drift-detail')).toHaveTextContent('action_count');
        expect(screen.getByTestId('drift-cause')).toHaveTextContent(
            'Cause: The model changed'
        );
    });

    it('the finding card sits on its own surface', async () => {
        render(<DriftPanel aiSystemId="sys-1" />);
        const card = await screen.findByTestId('drift-event-drift-1');
        expect(card.className).toContain('bg-surface');
    });

    it('shows the evidence summary the ladder produced', async () => {
        render(<DriftPanel aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(
                screen.getByText(/The model serving this system changed/)
            ).toBeInTheDocument();
        });
    });

    it('shows when the behaviour moved, not just when it was noticed', async () => {
        render(<DriftPanel aiSystemId="sys-1" />);
        const detail = await screen.findByTestId('drift-detail');
        expect(detail).toHaveTextContent('changed');
    });

    it('names the outcome a per-outcome finding was learned on', async () => {
        mockDrift.mockResolvedValue(
            list([
                event({
                    metric_key: 'action_count',
                    direction: 'up',
                    cause_evidence: { bucket: 'application_processed.loan_approved' },
                }),
            ])
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('drift-title')).toHaveTextContent(
            'shifted up in application_processed.loan_approved runs'
        );
    });

    it('names the outcome mix as its own finding, without a bucket', async () => {
        mockDrift.mockResolvedValue(
            list([event({ metric_key: 'outcome_mix', cause_evidence: { bucket: 'all' } })])
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        const title = await screen.findByTestId('drift-title');
        expect(title).toHaveTextContent('Outcome mix shifted');
        expect(title).not.toHaveTextContent(' in all runs');
    });

    it('says plainly when there is no identified cause — exactly once', async () => {
        mockDrift.mockResolvedValue(
            list([
                event({
                    metric_key: 'terminal_state_mix',
                    attributed_cause: 'unexplained',
                    cause_evidence: {},
                    is_explained: false,
                }),
            ])
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        // What shifted is the headline, explained in operator words…
        expect(await screen.findByTestId('drift-title')).toHaveTextContent(
            'Run execution mix shifted'
        );
        expect(
            screen.getByText(/distribution of how runs end/)
        ).toBeInTheDocument();
        // …and the absent cause is stated once, not repeated as a subtitle.
        expect(screen.getByTestId('drift-cause')).toHaveTextContent(
            'Cause: No cause identified'
        );
        expect(screen.getAllByText(/No cause identified/)).toHaveLength(1);
        expect(screen.getByTestId('drift-unexplained')).toHaveTextContent(
            'Worth looking at manually'
        );
    });

    it('distinguishes still-learning from no-problems', async () => {
        mockDrift.mockResolvedValue(list([]));
        mockBaselines.mockResolvedValue({
            baselines: [
                { id: 'b1', ai_system_id: 'sys-1', metric_key: 'action_count', bucket: 'all',
                  distribution: {}, sample_count: 8, state: 'learning' as const, is_ready: false },
            ],
            total: 1,
            learning: 1,
        });
        render(<DriftPanel aiSystemId="sys-1" />);
        const note = await screen.findByTestId('drift-learning');
        expect(note).toHaveTextContent('not a clean bill of health');
        expect(screen.queryByText('No drift detected')).not.toBeInTheDocument();
    });

    it('shows a clean result only once baselines are ready', async () => {
        mockDrift.mockResolvedValue(list([]));
        render(<DriftPanel aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText('No drift detected')).toBeInTheDocument();
        });
        expect(screen.queryByTestId('drift-learning')).not.toBeInTheDocument();
    });

    it('offers acknowledge and one close action; acknowledge never closes', async () => {
        render(<DriftPanel aiSystemId="sys-1" />);
        const row = await screen.findByTestId('drift-event-drift-1');
        expect(within(row).getByTestId('ack-button')).toBeInTheDocument();
        expect(within(row).getByTestId('close-finding-button')).toBeInTheDocument();
        fireEvent.click(within(row).getByTestId('ack-button'));
        await waitFor(() => expect(mockAck).toHaveBeenCalledWith('drift-1'));
        expect(mockClose).not.toHaveBeenCalled();
        expect(screen.queryByTestId('resolve-drift-dialog')).not.toBeInTheDocument();
    });

    it('close opens the kind dialog instead of closing straight away', async () => {
        render(<DriftPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('close-finding-button'));
        expect(screen.getByTestId('resolve-drift-dialog')).toBeInTheDocument();
        expect(mockClose).not.toHaveBeenCalled();
    });

    it('closes with the chosen kind and note, then reloads', async () => {
        render(<DriftPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('close-finding-button'));
        fireEvent.click(within(screen.getByTestId('resolve-kind-accepted_change')).getByRole('radio'));
        fireEvent.change(screen.getByTestId('resolve-note'), { target: { value: 'the 09-26 retune' } });
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        await waitFor(() =>
            expect(mockClose).toHaveBeenCalledWith('drift-1', { kind: 'accepted_change', note: 'the 09-26 retune' }),
        );
        await waitFor(() => expect(mockDrift).toHaveBeenCalledTimes(2));
        expect(screen.queryByTestId('resolve-drift-dialog')).not.toBeInTheDocument();
    });

    it('a grouped card closes every member with the same request', async () => {
        mockDrift.mockResolvedValue(list([event({ id: 'd1' }), event({ id: 'd2', detected_at: '2026-07-14T16:00:00Z' })]));
        render(<DriftPanel aiSystemId="sys-1" />);
        fireEvent.click(await screen.findByTestId('close-finding-button'));
        fireEvent.click(within(screen.getByTestId('resolve-kind-false_positive')).getByRole('radio'));
        fireEvent.change(screen.getByTestId('resolve-note'), { target: { value: 'seasonal' } });
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        await waitFor(() => expect(mockClose).toHaveBeenCalledTimes(2));
        expect(mockClose.mock.calls.map((c) => c[0] as string).sort()).toEqual(['d1', 'd2']);
        for (const c of mockClose.mock.calls) expect(c[1]).toEqual({ kind: 'false_positive', note: 'seasonal' });
    });

    // A grouped cause_fixed close is ONE incident: the first
    // member declares the window, the others attach to it.
    const three = () =>
        list([
            event({ id: 'd1' }),
            event({ id: 'd2', detected_at: '2026-07-14T16:00:00Z' }),
            event({ id: 'd3', detected_at: '2026-07-14T17:00:00Z' }),
        ]);
    const closeAsCauseFixed = async () => {
        fireEvent.click(await screen.findByTestId('close-finding-button'));
        fireEvent.click(within(screen.getByTestId('resolve-kind-cause_fixed')).getByRole('radio'));
        fireEvent.change(screen.getByTestId('resolve-note'), { target: { value: 'provider outage' } });
        fireEvent.click(screen.getByTestId('resolve-confirm'));
    };
    const resolution = (windowId: string | null) => ({
        ...event({ status: 'resolved', is_open: false }),
        incident_window: windowId ? { id: windowId } : null,
        seal: { sealed: true },
    });

    it('a ×3 grouped cause_fixed close declares one window and attaches the other two', async () => {
        mockDrift.mockResolvedValue(three());
        mockClose.mockResolvedValue(resolution('iw-9'));
        render(<DriftPanel aiSystemId="sys-1" />);
        await closeAsCauseFixed();
        await waitFor(() => expect(mockClose).toHaveBeenCalledTimes(3));
        const bodies = mockClose.mock.calls.map((c) => c[1] as Record<string, unknown>);
        const declarations = bodies.filter((b) => 'window_starts_at' in b);
        const attachments = bodies.filter((b) => b.incident_window_id === 'iw-9');
        expect(declarations).toHaveLength(1);
        expect(declarations[0]).toMatchObject({ kind: 'cause_fixed', note: 'provider outage' });
        expect(declarations[0]).not.toHaveProperty('incident_window_id');
        expect(attachments).toEqual([
            { kind: 'cause_fixed', note: 'provider outage', incident_window_id: 'iw-9' },
            { kind: 'cause_fixed', note: 'provider outage', incident_window_id: 'iw-9' },
        ]);
        await waitFor(() => expect(screen.queryByTestId('resolve-drift-dialog')).not.toBeInTheDocument());
    });

    it('a member refused mid-way is reported by id; the rest still close; a retry attaches', async () => {
        mockDrift.mockResolvedValue(three());
        mockClose
            .mockResolvedValueOnce(resolution('iw-9'))
            .mockRejectedValueOnce(new Error('already closed'))
            .mockResolvedValueOnce(resolution(null));
        render(<DriftPanel aiSystemId="sys-1" />);
        await closeAsCauseFixed();
        const error = await screen.findByTestId('resolve-error');
        expect(error).toHaveTextContent('Closed 2 of 3');
        expect(error).toHaveTextContent('d2 (already closed)');
        expect(mockClose).toHaveBeenCalledTimes(3);
        expect(mockClose.mock.calls[2][0]).toBe('d3');
        // The dialog stays open on what is still open; a retry attaches
        // to the window already declared instead of declaring another.
        expect(screen.getByTestId('resolve-drift-dialog')).toBeInTheDocument();
        mockClose.mockResolvedValue(resolution(null));
        mockDrift.mockResolvedValue(list([event({ id: 'd2' })]));
        fireEvent.click(screen.getByTestId('resolve-confirm'));
        await waitFor(() => expect(mockClose).toHaveBeenCalledTimes(4));
        expect(mockClose.mock.calls[3]).toEqual([
            'd2',
            { kind: 'cause_fixed', note: 'provider outage', incident_window_id: 'iw-9' },
        ]);
    });

    it('hides triage controls on a closed finding', async () => {
        mockDrift.mockResolvedValue(
            list([event({ status: 'false_positive', is_open: false, resolved_by: 'alice' })])
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText(/Marked not drift/)).toBeInTheDocument();
        });
        expect(screen.queryByTestId('close-finding-button')).not.toBeInTheDocument();
        expect(screen.queryByTestId('ack-button')).not.toBeInTheDocument();
    });

    it('reports how many open findings have no cause', async () => {
        mockDrift.mockResolvedValue(
            list([
                event({ id: 'd1' }),
                event({ id: 'd2', attributed_cause: 'unexplained', is_explained: false }),
            ])
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('unexplained-count')).toHaveTextContent(
            '1 of 2'
        );
    });

    it('surfaces an API failure instead of an empty panel', async () => {
        mockDrift.mockRejectedValue(new Error('control plane unreachable'));
        mockBaselines.mockRejectedValue(new Error('control plane unreachable'));
        render(<DriftPanel aiSystemId="sys-1" />);
        await waitFor(() => {
            expect(screen.getByText('control plane unreachable')).toBeInTheDocument();
        });
        expect(screen.queryByText('No drift detected')).not.toBeInTheDocument();
    });
});

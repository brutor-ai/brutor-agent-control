/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * DriftPanel — how a finding was closed. A closed finding says which kind of fact closed it
 * and what that did to detection; an `incident_window` cause names the
 * window; a baseline an accepted change reset says it is re-learning, and
 * its learning state stays its own.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

const mockDrift = vi.fn();
const mockBaselines = vi.fn();
const mockEpochs = vi.fn();
const mockWindows = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemDrift: mockDrift,
        fetchAISystemBaselines: mockBaselines,
        fetchAISystemBaselineEpochs: mockEpochs,
        fetchAISystemIncidentWindows: mockWindows,
        acknowledgeDriftEvent: vi.fn(),
        closeDriftEvent: vi.fn(),
    }),
}));

import DriftPanel from '../DriftPanel';
import type { Baseline, DriftEvent } from '../../../lib/aiSystemTypes';
import type { BaselineEpoch, IncidentWindow } from '../../../lib/driftResolutionTypes';

function event(overrides: Partial<DriftEvent> = {}): DriftEvent {
    return {
        id: 'drift-1',
        ai_system_id: 'sys-1',
        drift_class: 'behavioural',
        metric_key: 'action_count',
        detected_at: '2026-09-28T07:27:00Z',
        change_point_at: '2026-09-26T06:06:00Z',
        baseline_value: 5,
        observed_value: 3,
        direction: 'down',
        severity: 'high',
        attributed_cause: 'unexplained',
        cause_evidence: {},
        correlated_metrics: [],
        response_taken: {},
        status: 'active',
        is_open: true,
        is_explained: false,
        ...overrides,
    };
}

const EPOCH: BaselineEpoch = {
    id: 'ep-1',
    ai_system_id: 'sys-1',
    metric_key: 'action_count',
    basis: 'runs',
    caller_system_id: 'all',
    bucket: 'all',
    starts_at: '2026-09-26T06:06:00Z',
    kind: 'accepted_change',
    drift_event_id: 'drift-1',
    reason: 'the demo retune',
    declared_by: 'bob',
    record_id: 'rec-1',
};

const WINDOW: IncidentWindow = {
    id: 'iw-1',
    ai_system_id: 'sys-1',
    tenant_wide: false,
    starts_at: '2026-09-27T21:00:00Z',
    ends_at: '2026-09-28T06:40:00Z',
    cause_category: 'provider_outage',
    reason: 'credits ran out',
    drift_event_id: 'drift-2',
    declared_by: 'alice',
    runs_excluded: 14,
};

function baseline(overrides: Partial<Baseline> = {}): Baseline {
    return {
        id: 'b1',
        ai_system_id: 'sys-1',
        metric_key: 'action_count',
        bucket: 'all',
        actor_id: 'all',
        basis: 'runs',
        caller_system_id: 'all',
        distribution: {},
        sample_count: 12,
        state: 'learning',
        is_ready: false,
        ...overrides,
    };
}

const list = (events: DriftEvent[]) => ({
    events,
    total: events.length,
    open_count: events.filter((e) => e.is_open).length,
    unexplained_count: 0,
});

describe('DriftPanel — resolution', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockBaselines.mockResolvedValue({ baselines: [baseline({ state: 'ready', is_ready: true, sample_count: 90 })], total: 1, learning: 0 });
        mockEpochs.mockResolvedValue({ epochs: [EPOCH], total: 1 });
        mockWindows.mockResolvedValue({ windows: [WINDOW], total: 1 });
    });

    it('an accepted change shows its kind and that the baseline is re-learning', async () => {
        mockDrift.mockResolvedValue(
            list([event({ status: 'resolved', is_open: false, resolution_kind: 'accepted_change', resolved_by: 'bob', resolution_note: 'the demo retune' })]),
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        const closed = await screen.findByTestId('drift-closed');
        expect(within(closed).getByTestId('drift-closed-kind')).toHaveTextContent('Accepted change');
        expect(within(closed).getByTestId('drift-closed-effect')).toHaveTextContent(
            `baseline re-learning since ${new Date(EPOCH.starts_at!).toLocaleString()}`,
        );
        expect(closed).toHaveTextContent('the demo retune');
    });

    it('a cause fixed shows the incident window it declared', async () => {
        mockDrift.mockResolvedValue(
            list([event({ id: 'drift-2', status: 'resolved', is_open: false, resolution_kind: 'cause_fixed', resolved_by: 'alice' })]),
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        const effect = await screen.findByTestId('drift-closed-effect');
        expect(effect).toHaveTextContent(
            `incident window ${new Date(WINDOW.starts_at!).toLocaleString()} → ${new Date(WINDOW.ends_at!).toLocaleString()}`,
        );
    });

    it('labels the incident_window cause and names the window', async () => {
        mockDrift.mockResolvedValue(
            list([
                event({
                    attributed_cause: 'incident_window',
                    is_explained: true,
                    cause_evidence: {
                        incident_window_id: 'iw-1',
                        tenant_wide: true,
                        starts_at: '2026-09-27T21:00:00Z',
                        ends_at: '2026-09-28T06:40:00Z',
                        cause_category: 'provider_outage',
                        reason: 'credits ran out',
                    },
                }),
            ]),
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        expect(await screen.findByTestId('drift-cause')).toHaveTextContent('Cause: Inside a declared incident window');
        const detail = screen.getByTestId('drift-cause-window');
        expect(detail).toHaveTextContent('Provider outage, tenant-wide');
        expect(detail).toHaveTextContent('credits ran out');
    });

    it('a baseline reset by an accepted change says so, and stays learning until it is not', async () => {
        mockDrift.mockResolvedValue(list([]));
        mockBaselines.mockResolvedValue({
            baselines: [baseline({ epoch: EPOCH }), baseline({ id: 'b2', metric_key: 'cost_per_run', state: 'ready', is_ready: true })],
            total: 2,
            learning: 1,
        });
        render(<DriftPanel aiSystemId="sys-1" />);
        const row = await screen.findByTestId('baseline-epoch-action_count');
        expect(row).toHaveTextContent(
            `re-learning since ${new Date(EPOCH.starts_at!).toLocaleString()} — accepted change by bob`,
        );
        expect(within(row).getByTestId('baseline-epoch-state')).toHaveTextContent('learning · 12 samples so far');
        // Only the reset baseline is listed.
        expect(screen.queryByTestId('baseline-epoch-cost_per_run')).not.toBeInTheDocument();
    });
});

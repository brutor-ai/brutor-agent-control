/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * DriftPanel for systems with dependencies:
 *
 * * a finding measured on a shared service's served calls says so, and a
 *   per-caller finding names the caller;
 * * the aggregate and a per-caller finding on the same metric are two
 *   findings, never folded into one card;
 * * a `dependency` finding on the root is labelled by the dependency and
 *   links to it — it is not the root's own behaviour.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const mockDrift = vi.fn();
const mockBaselines = vi.fn();
const mockTriage = vi.fn();
const mockFetchAISystems = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchAISystemBaselineEpochs: vi.fn().mockResolvedValue({ epochs: [], total: 0 }),
    fetchAISystemIncidentWindows: vi.fn().mockResolvedValue({ windows: [], total: 0 }),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemDrift: mockDrift,
        fetchAISystemBaselines: mockBaselines,
        fetchAISystemBaselineEpochs: stableGateway.fetchAISystemBaselineEpochs,
        fetchAISystemIncidentWindows: stableGateway.fetchAISystemIncidentWindows,
        acknowledgeDriftEvent: mockTriage,
        closeDriftEvent: mockTriage,
        fetchAISystems: mockFetchAISystems,
    }),
}));

import DriftPanel from '../DriftPanel';
import type { DriftEvent } from '../../../lib/aiSystemTypes';
import { callsWhileIdle } from '../../../test/idle';

function event(overrides: Partial<DriftEvent>): DriftEvent {
    return {
        id: 'drift-1',
        ai_system_id: 'sys-scoring',
        drift_class: 'behavioural',
        metric_key: 'action_count',
        basis: 'runs',
        caller_system_id: null,
        detected_at: '2026-09-26T15:00:00Z',
        change_point_at: '2026-09-26T14:00:00Z',
        baseline_value: 5,
        observed_value: 14,
        effect_size: 8.4,
        direction: 'up',
        severity: 'high',
        attributed_cause: 'unexplained',
        cause_confidence: null,
        cause_evidence: {},
        correlated_metrics: [],
        response_taken: {},
        status: 'active',
        is_open: true,
        is_explained: false,
        ...overrides,
    };
}

function list(events: DriftEvent[]) {
    return { events, total: events.length, open_count: events.length, unexplained_count: 0 };
}

const setActiveTab = vi.fn();

beforeEach(() => {
    vi.clearAllMocks();
    mockBaselines.mockResolvedValue({ baselines: [], total: 0, learning: 0 });
    mockFetchAISystems.mockResolvedValue({
        systems: [{ id: 'sys-demo', name: 'demo', display_name: 'Brutor Demo System' }],
        total: 1,
    });
});

describe('served-call drift', () => {
    it('labels the metric per served call and names the caller', async () => {
        mockDrift.mockResolvedValue(list([
            event({ id: 'd-agg', basis: 'segments' }),
            event({ id: 'd-caller', basis: 'segments', caller_system_id: 'sys-demo' }),
        ]));
        render(<DriftPanel aiSystemId="sys-scoring" />);
        const agg = await screen.findByTestId('drift-event-d-agg');
        const perCaller = screen.getByTestId('drift-event-d-caller');
        // Two findings: the per-caller one is not folded into the aggregate.
        expect(within(agg).queryByTestId('drift-group-count')).toBeNull();
        expect(within(agg).getByTestId('drift-title')).toHaveTextContent('Actions per served call shifted up');
        expect(within(agg).getByTestId('drift-basis')).toHaveTextContent('served calls');
        expect(within(agg).getByTestId('drift-detail-basis')).toHaveTextContent('on served calls');
        // The caller shows by id first; its name arrives with a later fetch.
        await waitFor(() => expect(within(perCaller).getByTestId('drift-caller')).toHaveTextContent('caller Brutor Demo System'));
        expect(within(perCaller).getByTestId('drift-basis')).toHaveTextContent('served calls from Brutor Demo System');
    });

    it('names that could not be loaded: the caller is shown by id, and the panel says why, once, with a Retry', async () => {
        mockFetchAISystems.mockRejectedValueOnce(new Error('systems down'));
        mockDrift.mockResolvedValue(list([
            event({ id: 'd-one', basis: 'segments', caller_system_id: 'sys-demo' }),
            event({ id: 'd-two', basis: 'segments', caller_system_id: 'sys-other', metric_key: 'tool_error_rate' }),
        ]));
        render(<DriftPanel aiSystemId="sys-scoring" />);
        const perCaller = await screen.findByTestId('drift-event-d-one');
        const notice = await screen.findByTestId('ai-system-names-failed');
        expect(notice).toHaveTextContent('Failed to load the names of the AI Systems; ids are shown in their place: systems down');
        expect(screen.getAllByTestId('ai-system-names-failed')).toHaveLength(1);
        expect(within(perCaller).getByTestId('drift-caller')).toHaveTextContent('caller sys-demo');

        fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(within(perCaller).getByTestId('drift-caller')).toHaveTextContent('caller Brutor Demo System'));
        expect(screen.queryByTestId('ai-system-names-failed')).toBeNull();
    });

    it('marks own-run drift as such and resolves no names when no caller is named', async () => {
        mockDrift.mockResolvedValue(list([event({ id: 'd-own', basis: 'runs' })]));
        render(<DriftPanel aiSystemId="sys-scoring" />);
        const card = await screen.findByTestId('drift-event-d-own');
        expect(within(card).getByTestId('drift-title')).toHaveTextContent('Actions per run shifted up');
        expect(within(card).queryByTestId('drift-basis')).toBeNull();
        expect(within(card).getByTestId('drift-detail-basis')).toHaveTextContent('on own runs');
        expect(mockFetchAISystems).not.toHaveBeenCalled();
    });

    it('labels the new service metric keys', async () => {
        mockDrift.mockResolvedValue(list([
            event({ id: 'd-llm', metric_key: 'llm_call_count', basis: 'segments' }),
            event({ id: 'd-tok', metric_key: 'total_tokens', basis: 'segments' }),
            event({ id: 'd-err', metric_key: 'error_rate', basis: 'segments' }),
        ]));
        render(<DriftPanel aiSystemId="sys-scoring" />);
        expect(within(await screen.findByTestId('drift-event-d-llm')).getByTestId('drift-title')).toHaveTextContent('LLM calls per served call');
        expect(within(screen.getByTestId('drift-event-d-tok')).getByTestId('drift-title')).toHaveTextContent('Tokens per served call');
        expect(within(screen.getByTestId('drift-event-d-err')).getByTestId('drift-title')).toHaveTextContent('Error rate');
    });
});

describe('dependency drift on the root', () => {
    const dependencyEvent = event({
        id: 'd-dep',
        ai_system_id: 'sys-demo',
        drift_class: 'dependency',
        metric_key: 'dependency:sys-scoring',
        attributed_cause: 'dependency_degraded',
        direction: 'down',
        is_explained: true,
        severity: 'critical',
        baseline_value: null,
        observed_value: null,
        cause_evidence: {
            edge_id: 'dep-scoring',
            dependency_system_id: 'sys-scoring',
            dependency_name: 'Scoring Service',
            criticality: 'critical',
            status: 'degraded',
            failing_signal: 'liveness',
        },
    });

    it('is titled by the dependency and links to it', async () => {
        mockDrift.mockResolvedValue(list([dependencyEvent]));
        render(<DriftPanel aiSystemId="sys-demo" setActiveTab={setActiveTab} />);
        const card = await screen.findByTestId('drift-event-d-dep');
        expect(within(card).getByTestId('drift-title')).toHaveTextContent('Dependency Scoring Service is not healthy');
        expect(within(card).getByTestId('drift-dependency')).toHaveTextContent("not this system's own behaviour");
        fireEvent.click(within(card).getByTestId('drift-dependency-link'));
        expect(setActiveTab).toHaveBeenCalledWith('ai-systems:sys-scoring:signals');
    });

    it('names the dependency even without navigation', async () => {
        mockDrift.mockResolvedValue(list([dependencyEvent]));
        render(<DriftPanel aiSystemId="sys-demo" />);
        const card = await screen.findByTestId('drift-event-d-dep');
        expect(within(card).queryByTestId('drift-dependency-link')).toBeNull();
        expect(within(card).getByTestId('drift-dependency')).toHaveTextContent('Scoring Service');
    });
});

describe('DriftPanel — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        mockDrift.mockResolvedValue(list([event({ id: 'd-agg', basis: 'segments' })]));
        render(<DriftPanel aiSystemId="sys-scoring" />);
        await screen.findByTestId('drift-event-d-agg');
        expect(await callsWhileIdle([mockDrift, mockBaselines])).toBe(0);
    });
});

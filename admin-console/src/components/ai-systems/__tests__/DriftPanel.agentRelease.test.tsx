/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Drift shows the `agent_release` cause with BOTH releases,
 * the undeclared change as "no version change, new fingerprint", and the
 * control plane's agent findings (drift_class `agent`) by their name.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

const mockDrift = vi.fn();

// The page keys effects on these: one identity for the whole file, or every
// render hands the effects a new dependency and the page fetches for ever.
const stableGateway = {
    fetchAISystemBaselines: vi.fn().mockResolvedValue({ baselines: [], total: 0, learning: 0 }),
    fetchAISystemBaselineEpochs: vi.fn().mockResolvedValue({ epochs: [], total: 0 }),
    fetchAISystemIncidentWindows: vi.fn().mockResolvedValue({ windows: [], total: 0 }),
    acknowledgeDriftEvent: vi.fn(),
    closeDriftEvent: vi.fn(),
};

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchAISystemDrift: mockDrift,
        fetchAISystemBaselines: stableGateway.fetchAISystemBaselines,
        fetchAISystemBaselineEpochs: stableGateway.fetchAISystemBaselineEpochs,
        fetchAISystemIncidentWindows: stableGateway.fetchAISystemIncidentWindows,
        acknowledgeDriftEvent: stableGateway.acknowledgeDriftEvent,
        closeDriftEvent: stableGateway.closeDriftEvent,
    }),
}));

import DriftPanel from '../DriftPanel';
import type { DriftEvent } from '../../../lib/aiSystemTypes';
import { callsWhileIdle } from '../../../test/idle';

function event(overrides: Partial<DriftEvent>): DriftEvent {
    return {
        id: 'drift-1',
        ai_system_id: 'sys-1',
        drift_class: 'behavioural',
        metric_key: 'action_count',
        detected_at: '2026-09-27T15:00:00Z',
        change_point_at: '2026-09-27T14:00:00Z',
        baseline_value: 5,
        observed_value: 14,
        direction: 'up',
        severity: 'high',
        attributed_cause: 'agent_release',
        cause_confidence: 0.85,
        cause_evidence: {},
        correlated_metrics: [],
        response_taken: {},
        status: 'active',
        is_open: true,
        is_explained: true,
        ...overrides,
    };
}

const list = (events: DriftEvent[]) => ({
    events,
    total: events.length,
    open_count: events.length,
    unexplained_count: 0,
});

const setActiveTab = vi.fn();

beforeEach(() => {
    vi.clearAllMocks();
});

describe('DriftPanel — the agent_release cause', () => {
    it('names the cause and both releases of a transition', async () => {
        mockDrift.mockResolvedValue(
            list([
                event({
                    cause_evidence: {
                        agent_id: 'agent-1',
                        from: { name: 'screening-agent', version: '1.4.2', build: null },
                        to: { name: 'screening-agent', version: '1.5.0', build: 'sha256:beef' },
                        changed_at: '2026-09-27T14:00:00Z',
                        version_change: true,
                        from_fingerprint: 'sha256:aa',
                        to_fingerprint: 'sha256:bb',
                        trust: 'declared',
                    },
                }),
            ]),
        );
        render(<DriftPanel aiSystemId="sys-1" setActiveTab={setActiveTab} />);
        expect(await screen.findByTestId('drift-cause')).toHaveTextContent('Cause: The agent shipped a new release');
        expect(screen.getByTestId('drift-release-transition')).toHaveTextContent(
            'screening-agent@1.4.2 → screening-agent@1.5.0 (sha256:beef)',
        );
        expect(screen.getByTestId('drift-release-trust')).toHaveAttribute('data-trust', 'declared');
        // Still a behavioural shift of this system.
        expect(screen.getByTestId('drift-title')).toHaveTextContent('Actions per run shifted up');
        fireEvent.click(screen.getByTestId('drift-release-link'));
        expect(setActiveTab).toHaveBeenCalledWith('a2a-identities:agent-1:implementation');
    });

    it('says "no version change" with the fingerprints for the undeclared change', async () => {
        mockDrift.mockResolvedValue(
            list([
                event({
                    cause_evidence: {
                        agent_id: 'agent-1',
                        from: { name: 'screening-agent', version: '1.5.0', build: null },
                        to: { name: 'screening-agent', version: '1.5.0', build: null },
                        version_change: false,
                        from_fingerprint: `sha256:${'1'.repeat(64)}`,
                        to_fingerprint: `sha256:${'2'.repeat(64)}`,
                        trust: 'declared',
                    },
                }),
            ]),
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        const line = await screen.findByTestId('drift-release-no-version-change');
        expect(line).toHaveTextContent('No version change: still screening-agent@1.5.0');
        expect(line).toHaveTextContent('sha256:111111111111 → sha256:222222222222');
        expect(screen.queryByTestId('drift-release-transition')).toBeNull();
    });

    it('titles an agent finding by what it is, with the agent, never "shifted"', async () => {
        mockDrift.mockResolvedValue(
            list([
                event({
                    drift_class: 'agent',
                    metric_key: 'agent.unapproved_release:agent-1',
                    direction: 'shift',
                    baseline_value: null,
                    observed_value: null,
                    cause_evidence: {
                        code: 'agent.unapproved_release',
                        agent_id: 'agent-1',
                        agent_name: 'Screening',
                        release: { name: 'screening-agent', version: '1.6.0', build: null, trust: 'verified', approved: false },
                        message: 'Screening ran release screening-agent@1.6.0 (verified), which is not among its approved versions (1.5.*).',
                    },
                }),
            ]),
        );
        render(<DriftPanel aiSystemId="sys-1" />);
        const title = await screen.findByTestId('drift-title');
        expect(title).toHaveTextContent('Unapproved release — Screening');
        expect(title).not.toHaveTextContent('shifted');
        const release = screen.getByTestId('drift-release');
        expect(within(release).getByTestId('drift-release-message')).toHaveTextContent('not among its approved versions');
        expect(within(release).getByTestId('drift-release-transition')).toHaveTextContent('screening-agent@1.6.0');
        expect(within(release).getByTestId('drift-release-trust')).toHaveAttribute('data-trust', 'verified');
        expect(screen.getByTestId('drift-detail')).toHaveTextContent('Agent release');
    });

    it('other causes carry no release block', async () => {
        mockDrift.mockResolvedValue(list([event({ attributed_cause: 'model_change', cause_evidence: { from: { name: 'x', version: '1' } } })]));
        render(<DriftPanel aiSystemId="sys-1" />);
        await screen.findByTestId('drift-cause');
        expect(screen.queryByTestId('drift-release')).toBeNull();
    });
});

describe('DriftPanel — at rest', () => {
    it('loads once: left alone, it fetches nothing more', async () => {
        mockDrift.mockResolvedValue(list([event({})]));
        render(<DriftPanel aiSystemId="sys-1" />);
        await screen.findByTestId('drift-event-drift-1');
        expect(await callsWhileIdle([mockDrift])).toBe(0);
    });
});

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const mockHealth = vi.fn();
const mockLiveness = vi.fn();
const mockDrift = vi.fn();

vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({
        fetchSystemHealth: mockHealth,
        fetchAISystemLiveness: mockLiveness,
        fetchAISystemDrift: mockDrift,
    }),
}));

import StatusStrip from '../StatusStrip';
import { HEALTH_STYLE, type RunStatsResponse } from '../../../lib/aiSystemTypes';

function health(overrides: Record<string, unknown> = {}) {
    return {
        ai_system_id: 'sys-1',
        health_state: 'learning',
        health_score: 0.97,
        runs_24h: 120,
        open_drift_events: 0,
        reasons: ['Still learning'],
        is_trustworthy: true,
        computed_at: new Date(Date.now() - 3 * 60_000).toISOString(),
        ...overrides,
    };
}

function liveness(overrides: Record<string, unknown> = {}) {
    return {
        ai_system_id: 'sys-1',
        configured: true,
        expectation: { alerts_on_silence: true },
        state: 'alive',
        reason: '12 run(s) in the window.',
        has_ever_run: true,
        is_silent: false,
        quiet_for_seconds: 60,
        ...overrides,
    };
}

const stats = { observed_pct: 1, asserted_pct: 0.4, chain_integrity_pct: 0.6 } as RunStatsResponse;

// Every chip renders immediately with a placeholder ("Drift —", "Coverage —")
// and fills in when the async load resolves. That makes `findByTestId` the
// wrong tool here: it waits for the element to *exist*, which it already does
// on first paint, so the assertion that follows races the state update. It
// passes when the microtask queue happens to drain first and fails under load.
//
// Assert on CONTENT via waitFor instead. It is two lines longer and it is not
// a race.
describe('StatusStrip', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockHealth.mockResolvedValue(health());
        mockLiveness.mockResolvedValue(liveness());
        mockDrift.mockResolvedValue({ events: [], total: 0, open_count: 3, unexplained_count: 0 });
    });

    it('reads Failing in red, judged on its latest runs, when every latest run failed', async () => {
        mockHealth.mockResolvedValue(health({
            health_state: 'failing',
            health_score: 0,
            reasons: ['The last 3 finished runs all failed: this system is failing now.'],
            recent_runs: {
                verdict: 'down', considered: 3, failed: 3, rule_runs: 3, latest: [],
                window_success_rate: 0.95, window_hours: 720,
            },
            windows: { health_period_hours: 720, recent_runs: 3, freshness_seconds: 300 },
        }));
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() => expect(screen.getByTestId('strip-health')).toHaveTextContent('Failing'));
        const pill = screen.getByTestId('strip-health');
        expect(pill).toHaveTextContent('0%');
        expect(pill.className).toContain(HEALTH_STYLE.failing.chip);
        expect(screen.getByTestId('health-basis')).toHaveTextContent('last 3 runs failed');
        expect(pill.title).toMatch(/How health is calculated/);
        expect(pill.title).toMatch(/range buttons on this page do not change it/);
    });

    it('says the score is over the fixed health window', async () => {
        mockHealth.mockResolvedValue(health({ health_state: 'healthy', health_score: 0.9 }));
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() => expect(screen.getByTestId('health-basis')).toHaveTextContent('30-day'));
    });

    it('colours a state the same as every other surface (one map)', async () => {
        mockHealth.mockResolvedValue(health({ health_state: 'degraded', health_score: 0.5 }));
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() => expect(screen.getByTestId('strip-health')).toHaveTextContent('Degraded'));
        expect(screen.getByTestId('strip-health').className).toContain(HEALTH_STYLE.degraded.chip);
    });

    it('shows the health verdict with its age', async () => {
        // A materialised verdict is only worth its timestamp: the strip must
        // say WHEN the state was computed, or a day-old "learning" reads as
        // a statement about now.
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() =>
            expect(screen.getByTestId('strip-health')).toHaveTextContent('Still learning')
        );
        expect(screen.getByTestId('health-as-of')).toHaveTextContent('3m ago');
    });

    it('shows coverage as complete when asserted runs fill the gap', async () => {
        // 60% signed + 40% asserted = 100% observed. The chip reports
        // coverage; provenance lives in the tooltip, split three ways.
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() =>
            expect(screen.getByTestId('strip-coverage')).toHaveTextContent('Coverage 100%')
        );
        const chip = screen.getByTestId('strip-coverage');
        expect(chip.title).toContain('60% gateway-signed');
        expect(chip.title).toContain('40% client-asserted');
    });

    it('reports open drift and routes the chip to the drift tab', async () => {
        const onOpenTab = vi.fn();
        render(<StatusStrip aiSystemId="sys-1" stats={stats} onOpenTab={onOpenTab} />);
        await waitFor(() =>
            expect(screen.getByTestId('strip-drift')).toHaveTextContent('Drift 3 open')
        );
        fireEvent.click(screen.getByTestId('strip-drift'));
        expect(onOpenTab).toHaveBeenCalledWith('drift');
    });

    it('refresh recomputes rather than re-reading the materialised row', async () => {
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() => expect(mockHealth).toHaveBeenCalledWith('sys-1', false));
        fireEvent.click(screen.getByTestId('strip-refresh'));
        await waitFor(() => expect(mockHealth).toHaveBeenCalledWith('sys-1', true));
    });

    it('re-reads its verdicts when reloadKey bumps (drift triage below it)', async () => {
        // The drift chip's open-count must follow a triage performed in the
        // tab underneath — a verdict that survives the action it reports on
        // is worse than no verdict at all.
        const { rerender } = render(
            <StatusStrip aiSystemId="sys-1" stats={stats} reloadKey={0} />
        );
        await waitFor(() =>
            expect(screen.getByTestId('strip-drift')).toHaveTextContent('Drift 3 open')
        );
        mockDrift.mockResolvedValue({ events: [], total: 0, open_count: 2, unexplained_count: 0 });
        rerender(<StatusStrip aiSystemId="sys-1" stats={stats} reloadKey={1} />);
        await waitFor(() =>
            expect(screen.getByTestId('strip-drift')).toHaveTextContent('Drift 2 open')
        );
    });

    // The chip is the endpoint's ONE `state` through the
    // shared mapping, the reason in its tooltip. One case per state.
    it.each([
        ['alive', 'Alive', 'ok'],
        ['silent', 'Silent', 'alert'],
        ['idle', 'Idle', 'neutral'],
        ['unstarted', 'Unstarted', 'neutral'],
        ['not_configured', 'Not configured', 'neutral'],
    ] as const)('renders liveness state %s as "%s"', async (state, label, tone) => {
        mockLiveness.mockResolvedValue(liveness({ state, reason: `because ${state}` }));
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() => expect(screen.getByTestId('strip-liveness')).toHaveTextContent(label));
        const chip = screen.getByTestId('strip-liveness');
        expect(chip).toHaveAttribute('data-state', state);
        expect(chip.title).toContain(`because ${state}`);
        const green = chip.className.includes('emerald');
        expect(green).toBe(tone === 'ok');
    });

    it('a service nobody called reads Idle even though it ran before and is configured', async () => {
        // A label derived from has_ever_run / is_silent / configured would
        // read "Alive" here.
        mockLiveness.mockResolvedValue(
            liveness({ state: 'idle', reason: 'No inbound calls in the window.', has_ever_run: true, is_silent: false, configured: true })
        );
        render(<StatusStrip aiSystemId="sys-1" stats={stats} />);
        await waitFor(() => expect(screen.getByTestId('strip-liveness')).toHaveTextContent('Idle'));
        expect(screen.getByTestId('strip-liveness')).not.toHaveTextContent('Alive');
    });
});

describe('StatusStrip evidence source', () => {
    it('discloses the reported share inside the coverage chip', async () => {
        // Ingested runs count as observed — the chip stays green — but the
        // split must say how much of that observation is the exporter's word.
        render(
            <StatusStrip
                aiSystemId="sys-1"
                stats={
                    {
                        observed_pct: 1,
                        asserted_pct: 0.2,
                        external_pct: 0.3,
                        chain_integrity_pct: 0.5,
                    } as RunStatsResponse
                }
            />,
        );
        await waitFor(() =>
            expect(screen.getByTestId('strip-coverage')).toHaveTextContent('Coverage 100%'),
        );
        const chip = screen.getByTestId('strip-coverage');
        expect(chip.getAttribute('title')).toContain('30% reported (OpenTelemetry)');
        expect(chip.getAttribute('title')).toContain('50% gateway-signed');
        expect(chip.className).not.toContain('amber');
    });
});

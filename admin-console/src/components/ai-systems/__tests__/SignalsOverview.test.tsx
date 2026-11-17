/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Signals → Overview: the overall verdict is worst-of, coloured exactly as
 * the Mission Control card colours it, and the panel names the signal the
 * verdict rests on. Pinned here:
 *
 * * the chips and stripe come from the shared card style maps;
 * * a detector-driven state (silent) badges its signal even when that
 *   signal's score reads 100%;
 * * a score-driven state (degraded) badges the lowest enforced signal and
 *   never an advisory or not-applicable one;
 * * `not_applicable` renders as "n/a", never as a pass;
 * * the "Open" links hand off to the signal's own sub-tab.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const fetchSystemHealth = vi.fn();
const fetchAssuranceHistory = vi.fn();
vi.mock('../../../hooks/useGateway', () => ({
    useGateway: () => ({ fetchSystemHealth, fetchAssuranceHistory }),
}));

import SignalsOverview from '../SignalsOverview';
import { deriveSignalRows, drivingSentence } from '../../../lib/signalsOverview';
import { assuranceStripe, HEALTH_STYLE, VERDICT_STYLE, type SystemHealth } from '../../../lib/aiSystemTypes';

function levels(scores: Record<string, number | null>, overrides: Record<string, string> = {}) {
    const out: Record<string, { score: number | null; level: string }> = {};
    for (const [k, v] of Object.entries(scores)) out[k] = { score: v, level: overrides[k] ?? 'enforced' };
    return out;
}

function health(overrides: Partial<SystemHealth>): SystemHealth {
    return {
        ai_system_id: 'sys-1',
        health_state: 'healthy',
        health_score: 0.92,
        runs_24h: 40,
        open_drift_events: 0,
        reasons: [],
        is_trustworthy: true,
        computed_at: new Date().toISOString(),
        signal_levels: levels({
            liveness: 1, behaviour: 0.92, reliability: 0.97, cost: 0.95,
            conformance: 1, oversight: null, review: 1,
        }),
        ...overrides,
    };
}

beforeEach(() => {
    fetchSystemHealth.mockReset();
    fetchAssuranceHistory.mockReset();
    fetchAssuranceHistory.mockResolvedValue({ reports: [], total: 0 });
});

describe('deriveSignalRows', () => {
    it('badges the liveness row for a silent system even at 100%', () => {
        const rows = deriveSignalRows(health({ health_state: 'silent',
            signal_levels: levels({ liveness: 1, behaviour: 0.5, reliability: 1, cost: 1, conformance: 1, oversight: 1, review: 1 }) }));
        expect(rows.find((r) => r.drives)?.id).toBe('liveness');
        expect(drivingSentence(health({ health_state: 'silent' }), rows)).toMatch(/Liveness drives the verdict/);
    });

    it('badges the lowest enforced signal for a degraded system, skipping advisory and n/a', () => {
        const h = health({
            health_state: 'degraded',
            signal_levels: levels(
                { liveness: 1, behaviour: 0.3, reliability: 0.6, cost: 0.2, conformance: 1, oversight: 1, review: 1 },
                { behaviour: 'advisory', cost: 'not_applicable' },
            ),
        });
        const rows = deriveSignalRows(h);
        expect(rows.find((r) => r.drives)?.id).toBe('reliability');
        expect(rows.find((r) => r.id === 'cost')?.level).toBe('not_applicable');
        expect(drivingSentence(h, rows)).toBe(
            'Reliability drives the verdict: it is the weakest enforced signal at 60%, below the 80% floor.',
        );
    });

    it('names the tightest margin for a healthy system', () => {
        const h = health({});
        const rows = deriveSignalRows(h);
        expect(rows.find((r) => r.drives)?.id).toBe('behaviour');
        expect(drivingSentence(h, rows)).toMatch(/Behaviour is the tightest margin at 92%/);
    });

    it('drives nothing when suspended or unknown', () => {
        expect(deriveSignalRows(health({ health_state: 'suspended' })).some((r) => r.drives)).toBe(false);
        expect(deriveSignalRows(health({ health_state: 'unknown' })).some((r) => r.drives)).toBe(false);
    });
});

describe('a failing system', () => {
    const latestFailure = '2026-10-25 19:04 UTC';
    const failing = (): SystemHealth => health({
        health_state: 'failing',
        health_score: 0,
        reasons: [
            `The last 3 finished runs all failed: model unavailable (latest ${latestFailure}): this system is failing now. ` +
                'Over the 30-day health window 95% of runs succeeded — the period rate alone would still read healthy.',
        ],
        signal_levels: levels({ liveness: 1, behaviour: 1, reliability: 0, cost: 1, conformance: 0.9, oversight: 1, review: 1 }),
        recent_runs: {
            verdict: 'down', considered: 3, failed: 3, rule_runs: 3,
            latest: ['a', 'b', 'c'].map((id) => ({ run_id: id, terminal_state: 'errored', ended_at: null, failed: true })),
            window_success_rate: 163 / 171, window_hours: 720,
        },
        windows: { health_period_hours: 720, recent_runs: 3, freshness_seconds: 300 },
    });

    it('badges reliability and reads the cause from the verdict’s own reason', () => {
        const h = failing();
        const rows = deriveSignalRows(h);
        expect(rows.find((r) => r.drives)?.id).toBe('reliability');
        expect(drivingSentence(h, rows)).toBe(
            'Reliability drives the verdict: the last 3 finished runs all failed: model unavailable ' +
                '(latest 2026-10-25 19:04 UTC): this system is failing now. Over the 30-day health window 95% of runs ' +
                'succeeded — the period rate alone would still read healthy.',
        );
    });

    it('without a reason, says the window rate alone would have looked healthy', () => {
        const h = { ...failing(), reasons: [] };
        expect(drivingSentence(h, deriveSignalRows(h))).toBe(
            'Reliability drives the verdict: the last 3 finished runs all failed. The system is failing now, ' +
                'even though 95% of runs over the 30-day window succeeded.',
        );
    });

    it('paints the stripe red, like a suspension', () => {
        expect(assuranceStripe('failing', 'assured')).toBe('bg-red-500');
    });

    it('shows the runs it judged, the window, and how health is calculated', async () => {
        fetchSystemHealth.mockResolvedValue(failing());
        render(<SignalsOverview aiSystemId="sys-1" />);
        const chip = await screen.findByTestId('overview-health-chip');
        expect(chip).toHaveTextContent('Failing');
        expect(chip.className).toContain(HEALTH_STYLE.failing.chip);
        expect(screen.getByTestId('overview-window')).toHaveTextContent('30-day window');
        const judged = screen.getAllByTestId('overview-recent-run');
        expect(judged).toHaveLength(3);
        expect(judged.every((el) => el.getAttribute('data-failed') === 'true')).toBe(true);
        // Said once, by the driver sentence and the runs row — not again as a raw reason.
        expect(screen.queryByTestId('overview-reason')).not.toBeInTheDocument();
        expect(screen.getByTestId('overview-explainer')).toHaveTextContent('How health is calculated');
        expect(screen.getByTestId('overview-explainer')).toHaveTextContent(/fixed — the range buttons/);
        // The cause comes from the verdict, in the driver sentence.
        expect(screen.getByTestId('overview-driver')).toHaveTextContent('model unavailable');
    });

    it('a degraded system names the cause of its failed recent runs from the verdict', async () => {
        const latestFailure = '2026-10-26 08:00 UTC';
        fetchSystemHealth.mockResolvedValue(
            health({
                health_state: 'degraded',
                health_score: 0.5,
                reasons: [
                    `1 of the last 3 finished runs failed: blocked by policy (latest ${latestFailure}); reliability is capped until 3 runs in a row succeed.`,
                ],
                signal_levels: levels({ liveness: 1, behaviour: 1, reliability: 0.5, cost: 1, conformance: 1, oversight: 1, review: 1 }),
                recent_runs: {
                    verdict: 'degraded', considered: 3, failed: 1, rule_runs: 3,
                    latest: [
                        { run_id: 'a', terminal_state: 'errored', ended_at: null, failed: true },
                        { run_id: 'b', terminal_state: 'completed', ended_at: null, failed: false },
                        { run_id: 'c', terminal_state: 'completed', ended_at: null, failed: false },
                    ],
                    window_success_rate: 0.9, window_hours: 720,
                },
            }),
        );
        render(<SignalsOverview aiSystemId="sys-1" />);
        const why = await screen.findByTestId('overview-recent-runs-why');
        expect(why).toHaveTextContent('blocked by policy');
        // Said once: not repeated as a raw reason line.
        expect(screen.queryByTestId('overview-reason')).not.toBeInTheDocument();
    });
});

describe('SignalsOverview', () => {
    it('colours the verdict with the Mission Control card maps', async () => {
        fetchSystemHealth.mockResolvedValue(health({ health_state: 'silent', reasons: ['This system has stopped calling through the gateway.'] }));
        fetchAssuranceHistory.mockResolvedValue({
            total: 2,
            reports: [
                { id: 'old', ai_system_id: 'sys-1', period_hours: 24, trigger: 'manual', created_at: '2026-09-01T00:00:00Z', verdict_status: 'assured' },
                { id: 'new', ai_system_id: 'sys-1', period_hours: 24, trigger: 'manual', created_at: '2026-09-06T00:00:00Z', verdict_status: 'assured_with_exceptions' },
            ],
        });
        render(<SignalsOverview aiSystemId="sys-1" />);

        const healthChip = await screen.findByTestId('overview-health-chip');
        expect(healthChip).toHaveTextContent(HEALTH_STYLE.silent.label);
        expect(healthChip.className).toContain(HEALTH_STYLE.silent.chip);
        // Newest report wins regardless of list order.
        const verdictChip = screen.getByTestId('overview-verdict-chip');
        expect(verdictChip).toHaveTextContent(VERDICT_STYLE.assured_with_exceptions.label);
        expect(verdictChip.className).toContain(VERDICT_STYLE.assured_with_exceptions.chip);
        expect(screen.getByTestId('overview-stripe').className).toContain(assuranceStripe('silent', 'assured_with_exceptions'));
        expect(screen.getByTestId('overview-signal-standing-liveness')).toHaveTextContent('Silent · drives the verdict');
        expect(screen.getByTestId('overview-reason')).toHaveTextContent('stopped calling');
        expect(screen.getByTestId('overview-as-of')).toHaveTextContent('as of just now');
    });

    it('states the approval holds an incident window excluded from oversight beside its score', async () => {
        // The reasons are stored with the score by the same computation, so
        // the excluded count travels with the number it qualifies.
        fetchSystemHealth.mockResolvedValue(health({
            health_state: 'degraded',
            signal_levels: levels({
                liveness: 1, behaviour: 0.92, reliability: 0.97, cost: 0.95,
                conformance: 1, oversight: 0.7, review: 1,
            }),
            reasons: [
                'oversight is the weakest component at 70%',
                '3 of 10 judged approval request(s) expired without a human decision.',
                '250 approval request(s) raised inside declared incident windows: recorded, not judged.',
            ],
        }));
        render(<SignalsOverview aiSystemId="sys-1" />);
        await screen.findByTestId('overview-verdict-card');
        const reasons = screen.getAllByTestId('overview-reason').map((r) => r.textContent);
        expect(reasons).toEqual([
            '3 of 10 judged approval request(s) expired without a human decision.',
            '250 approval request(s) raised inside declared incident windows: recorded, not judged.',
        ]);
        expect(screen.getByTestId('overview-signal-score-oversight')).toHaveTextContent('70%');
    });

    it('renders n/a and not-measured rows without a pass and links rows to their sub-tabs', async () => {
        fetchSystemHealth.mockResolvedValue(health({
            health_state: 'degraded',
            system_kind: 'assistant',
            signal_levels: levels(
                { liveness: 1, behaviour: 0.7, reliability: 0.9, cost: null, conformance: 1, oversight: null, review: 1 },
                { cost: 'not_applicable' },
            ),
            profile_relaxations: [{ signal: 'cost', level: 'not_applicable', reason: 'assistants have no per-run budget' }],
        }));
        const onOpenSignal = vi.fn();
        render(<SignalsOverview aiSystemId="sys-1" onOpenSignal={onOpenSignal} />);

        expect(await screen.findByTestId('overview-signal-score-cost')).toHaveTextContent('n/a');
        expect(screen.getByTestId('overview-signal-standing-cost')).toHaveTextContent('Not scored for this kind');
        expect(screen.getByTestId('overview-signal-score-oversight')).toHaveTextContent('not measured');
        expect(screen.getByTestId('overview-signal-standing-behaviour')).toHaveTextContent('Below floor · drives the verdict');
        expect(screen.getByTestId('overview-signal-standing-behaviour').className).toContain(HEALTH_STYLE.degraded.chip);
        expect(screen.getByTestId('overview-signal-standing-reliability').className).toContain(HEALTH_STYLE.healthy.chip);
        expect(screen.getByTestId('overview-verdict-none')).toBeInTheDocument();
        expect(screen.getByTestId('overview-relaxations')).toHaveTextContent('assistants have no per-run budget');

        fireEvent.click(screen.getByTestId('overview-open-behaviour'));
        expect(onOpenSignal).toHaveBeenCalledWith('trajectory');
        expect(screen.queryByTestId('overview-open-review')).not.toBeInTheDocument();
    });

    it('gives every signal row a help button that explains how it is scored', async () => {
        fetchSystemHealth.mockResolvedValue(health({}));
        render(<SignalsOverview aiSystemId="sys-1" />);
        const row = await screen.findByTestId('overview-signal-row-reliability');
        expect(screen.getAllByRole('button', { name: /How .* is scored/ })).toHaveLength(7);
        fireEvent.click(within(row).getByRole('button', { name: 'How Reliability is scored' }));
        const pop = await screen.findByTestId('help-tip-popover');
        expect(pop).toHaveTextContent('Reliability: does it do its job?');
        // The success rate as the platform computes it.
        expect(pop).toHaveTextContent('declared outcome that counts toward the baseline');
        expect(pop).toHaveTextContent('policy blocks included');
        expect(pop).toHaveTextContent('empty poll');
        expect(pop).toHaveTextContent('incident windows');
    });

    it('recomputes on refresh with the refresh flag', async () => {
        fetchSystemHealth.mockResolvedValue(health({}));
        render(<SignalsOverview aiSystemId="sys-1" />);
        await screen.findByTestId('overview-health-chip');
        expect(fetchSystemHealth).toHaveBeenLastCalledWith('sys-1', false);
        fireEvent.click(screen.getByTestId('overview-refresh'));
        expect(fetchSystemHealth).toHaveBeenLastCalledWith('sys-1', true);
    });

    it('surfaces a load failure instead of an empty verdict', async () => {
        fetchSystemHealth.mockRejectedValue(new Error('health 503'));
        render(<SignalsOverview aiSystemId="sys-1" />);
        expect(await screen.findByTestId('signals-overview-error')).toHaveTextContent('health 503');
    });
});

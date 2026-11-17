/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Which row the verdict rests on. The trap this pins: a detector-driven
 * state (silent / stalled / drifting) is set by a FLAG, not a score, so the
 * row it belongs to must drive the verdict even when its score reads 100%.
 * Picking the lowest score there would name an innocent signal.
 */

import { describe, it, expect } from 'vitest';
import {
    DEGRADED_BELOW,
    OVERVIEW_SIGNALS,
    deriveSignalRows,
    drivingSentence,
    recentRunsReason,
    signalHelp,
} from '../signalsOverview';
import type { SystemHealth } from '../aiSystemTypes';

const health = (over: Partial<SystemHealth> = {}): SystemHealth => ({
    ai_system_id: 's1',
    health_state: 'healthy',
    runs_24h: 100,
    open_drift_events: 0,
    reasons: [],
    is_trustworthy: true,
    ...over,
});

const driver = (h: SystemHealth) => deriveSignalRows(h).find((r) => r.drives);

describe('deriveSignalRows', () => {
    it('returns one row per signal in the profile order', () => {
        const rows = deriveSignalRows(health());
        expect(rows.map((r) => r.id)).toEqual(OVERVIEW_SIGNALS.map((s) => s.id));
    });

    it('reads a score from signal_levels in preference to the top-level field', () => {
        const rows = deriveSignalRows(
            health({
                liveness_score: 0.5,
                signal_levels: { liveness: { score: 0.95, level: 'enforced' } },
            }),
        );
        expect(rows.find((r) => r.id === 'liveness')?.score).toBe(0.95);
    });

    it('marks a signal with no score anywhere as unmeasured, not zero', () => {
        const row = deriveSignalRows(health()).find((r) => r.id === 'cost');
        expect(row?.score).toBeNull();
        expect(row?.level).toBe('unmeasured');
    });

    it('honours not_applicable and advisory levels even when a score exists', () => {
        const rows = deriveSignalRows(
            health({
                cost_score: 0.4,
                oversight_score: 0.3,
                signal_levels: {
                    cost: { score: 0.4, level: 'not_applicable' },
                    oversight: { score: 0.3, level: 'advisory' },
                },
            }),
        );
        expect(rows.find((r) => r.id === 'cost')?.level).toBe('not_applicable');
        expect(rows.find((r) => r.id === 'oversight')?.level).toBe('advisory');
    });

    it('lets the detector state pick the row, not the lowest score', () => {
        // Liveness reads 100% — it measures the runs that DID happen — while
        // reliability is the worst score. `silent` must still name liveness.
        expect(
            driver(
                health({
                    health_state: 'silent',
                    liveness_score: 1,
                    reliability_score: 0.2,
                }),
            )?.id,
        ).toBe('liveness');
        expect(driver(health({ health_state: 'stalled', reliability_score: 1 }))?.id).toBe(
            'reliability',
        );
        expect(driver(health({ health_state: 'drifting', behaviour_score: 1 }))?.id).toBe(
            'behaviour',
        );
    });

    it('names no driver at all when autonomy is suspended or nothing was scored', () => {
        expect(driver(health({ health_state: 'suspended', cost_score: 0.1 }))).toBeUndefined();
        expect(driver(health({ health_state: 'unknown', cost_score: 0.1 }))).toBeUndefined();
    });

    it('picks the single lowest enforced score for a score-driven state', () => {
        expect(
            driver(
                health({
                    health_state: 'degraded',
                    liveness_score: 0.9,
                    reliability_score: 0.55,
                    cost_score: 0.7,
                }),
            )?.id,
        ).toBe('reliability');
    });

    it('never lets an advisory or not-applicable row drive the verdict', () => {
        const rows = deriveSignalRows(
            health({
                liveness_score: 0.99,
                signal_levels: {
                    cost: { score: 0.01, level: 'advisory' },
                    behaviour: { score: 0.02, level: 'not_applicable' },
                },
            }),
        );
        expect(rows.filter((r) => r.drives).map((r) => r.id)).toEqual(['liveness']);
    });

    it('marks exactly one driver, or none — never two', () => {
        const states = ['healthy', 'degraded', 'silent', 'learning', 'unknown'];
        const counts = states.map(
            (state) =>
                deriveSignalRows(
                    health({
                        health_state: state as SystemHealth['health_state'],
                        liveness_score: 0.5,
                    }),
                ).filter((r) => r.drives).length,
        );
        expect(counts.filter((c) => c > 1)).toEqual([]);
    });
});

describe('drivingSentence', () => {
    const say = (h: SystemHealth) => drivingSentence(h, deriveSignalRows(h));

    it('explains suspension as an autonomy decision, not a signal', () => {
        expect(say(health({ health_state: 'suspended', liveness_score: 1 }))).toContain(
            'Autonomy is suspended',
        );
    });

    it('distinguishes "not yet scored" from "not trustworthy"', () => {
        expect(say(health({ health_state: 'unknown', is_trustworthy: true }))).toContain(
            'No enforced signal has been scored yet',
        );
        expect(say(health({ health_state: 'unknown', is_trustworthy: false }))).toContain(
            'Too few runs were observed intact',
        );
    });

    it('warns that a silent system can read 100% on liveness', () => {
        const s = say(health({ health_state: 'silent', liveness_score: 1 }));
        expect(s).toContain('stopped calling through the gateway');
        expect(s).toContain('100%');
    });

    it('has its own sentence for stalled, drifting and learning', () => {
        expect(say(health({ health_state: 'stalled' }))).toContain('starting but not finishing');
        expect(say(health({ health_state: 'drifting' }))).toContain('drift events');
        expect(say(health({ health_state: 'learning' }))).toContain('still learning');
    });

    it('names the weakest signal and the floor when degraded', () => {
        const s = say(health({ health_state: 'degraded', reliability_score: 0.55 }));
        expect(s).toContain('Reliability drives the verdict');
        expect(s).toContain('55%');
        expect(s).toContain(`${Math.round(DEGRADED_BELOW * 100)}%`);
    });

    it('frames a healthy verdict as the tightest margin, not a failure', () => {
        const s = say(health({ health_state: 'healthy', liveness_score: 0.92, cost_score: 0.99 }));
        expect(s).toContain('Liveness');
        expect(s).toContain('tightest margin');
        expect(s).toContain('92%');
    });

    it('says so plainly when a healthy state has nothing scored', () => {
        expect(say(health({ health_state: 'healthy' }))).toBe(
            'No enforced signal has been scored.',
        );
    });
});

describe('OVERVIEW_SIGNALS', () => {
    it('carries review with no top-level score field — it only travels in signal_levels', () => {
        expect(OVERVIEW_SIGNALS.find((s) => s.id === 'review')?.scoreKey).toBeNull();
    });

    it('lists the seven signals once each', () => {
        expect(OVERVIEW_SIGNALS).toHaveLength(7);
        expect(new Set(OVERVIEW_SIGNALS.map((s) => s.id)).size).toBe(7);
    });
});

describe('signalHelp', () => {
    it('explains every signal in the table', () => {
        for (const sig of OVERVIEW_SIGNALS) {
            const help = signalHelp(sig.id, health());
            expect(help?.title).toMatch(new RegExp(`^${sig.label}:`));
            expect(help?.text.length).toBeGreaterThan(40);
        }
        expect(signalHelp('nonsense', health())).toBeNull();
    });

    it('names the windows the health payload reports', () => {
        const h = health({ windows: { health_period_hours: 168, recent_runs: 5, freshness_seconds: 300 } });
        const text = signalHelp('reliability', h)!.text;
        expect(text).toContain('the 7-day health window');
        expect(text).toContain('latest 5 decisive finished runs');
        expect(text).toContain('Failing');
    });

    it('describes a shared service by the calls it serves, not its own runs', () => {
        const h = health({ signal_basis: 'segments' });
        expect(signalHelp('reliability', h)!.text).toMatch(/calls served/);
        expect(signalHelp('reliability', h)!.text).not.toMatch(/finished runs/);
        expect(signalHelp('cost', h)!.text).toMatch(/per served call/);
        expect(signalHelp('liveness', h)!.text).toMatch(/calls other systems make/);
    });
});

describe('reliability is the success rate, and the verdict names the cause', () => {
    it('describes success as a completed run in a declared outcome that counts, over success + deviations + every failure', () => {
        const text = signalHelp('reliability', health())!.text;
        expect(text).toContain('completed without errors AND ended in a declared outcome that counts toward the baseline');
        expect(text).toContain('successes + deviations');
        expect(text).toContain('every failed run (errored or exhausted, policy blocks included');
        expect(text).toContain('an empty poll, are in neither');
        expect(text).toContain('abandoned and cancelled runs are not decisive');
        expect(text).not.toMatch(/completed or completed degraded/);
    });

    it('a shared service counts an answer outside its contract against it', () => {
        const text = signalHelp('reliability', health({ signal_basis: 'segments' }))!.text;
        expect(text).toContain('does not declare counts against it');
    });

    it('cost speaks of processed outcomes, never averaging empty polls in', () => {
        const text = signalHelp('cost', health())!.text;
        expect(text).toContain('cost per processed outcome');
        expect(text).toContain('empty polls are never averaged in');
    });

    it('conformance names the outcome findings and their weights', () => {
        const text = signalHelp('conformance', health())!.text;
        expect(text).toContain('undeclared outcome reported takes off 25');
        expect(text).toContain('runs reporting no outcome');
        expect(text).toContain('declared outcome never seen takes off 5');
    });

    it('reads the latest-runs reason the verdict carries', () => {
        const latest = '2026-10-26 08:00 UTC';
        const reason = `The last 3 finished runs all failed: blocked by policy (latest ${latest}): this system is failing now.`;
        expect(recentRunsReason(health({ reasons: ['weakest component reliability', reason] }))).toBe(reason);
        expect(recentRunsReason(health({ reasons: ['weakest component reliability'] }))).toBeNull();
    });

    it('drives a failing verdict with the server’s cause, lower-cased into the sentence', () => {
        const latest = '2026-10-25 19:04 UTC';
        const h = health({
            health_state: 'failing',
            reasons: [`The last 3 finished runs all failed: model unavailable (latest ${latest}): this system is failing now.`],
        });
        expect(drivingSentence(h, deriveSignalRows(h))).toBe(
            `Reliability drives the verdict: the last 3 finished runs all failed: model unavailable (latest ${latest}): this system is failing now.`,
        );
    });
});

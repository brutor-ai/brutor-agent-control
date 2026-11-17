/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { healthBasisTag, healthExplainer, recentRunsSentence, windowSpan } from '../healthWindows';
import type { RecentRunsVerdict, SystemHealth } from '../aiSystemTypes';

const WINDOWS = { health_period_hours: 720, recent_runs: 3, freshness_seconds: 300 };

function recent(failed: boolean[]): RecentRunsVerdict {
    return {
        verdict: failed.every(Boolean) ? 'down' : failed.some(Boolean) ? 'degraded' : 'ok',
        considered: failed.length,
        failed: failed.filter(Boolean).length,
        rule_runs: 3,
        latest: failed.map((f, i) => ({
            run_id: `r${i}`,
            terminal_state: f ? 'errored' : 'completed',
            ended_at: null,
            failed: f,
        })),
        window_success_rate: 163 / 171,
        window_hours: 720,
    };
}

function health(overrides: Partial<SystemHealth>): SystemHealth {
    return {
        ai_system_id: 'sys',
        health_state: 'healthy',
        runs_24h: 0,
        open_drift_events: 0,
        reasons: [],
        is_trustworthy: true,
        windows: WINDOWS,
        ...overrides,
    };
}

describe('windowSpan', () => {
    it('reads whole days as days and anything else as hours', () => {
        expect(windowSpan(720)).toBe('30-day');
        expect(windowSpan(36)).toBe('36-hour');
    });

    it('falls back to the control plane default when a server omits it', () => {
        expect(windowSpan(undefined)).toBe('30-day');
    });
});

describe('recentRunsSentence', () => {
    it('says all of them failed — the demo outage', () => {
        expect(recentRunsSentence(recent([true, true, true]))).toBe('the last 3 finished runs all failed.');
    });

    it('counts a partial failure', () => {
        expect(recentRunsSentence(recent([true, false, false]))).toBe('1 of the last 3 finished runs failed.');
    });

    it('says nothing when no run was judged', () => {
        expect(recentRunsSentence(null)).toBeNull();
        expect(recentRunsSentence({ ...recent([]), verdict: null })).toBeNull();
    });
});

describe('healthBasisTag', () => {
    it('names the fixed health window for a scored state', () => {
        expect(healthBasisTag(health({}))).toBe('30-day');
    });

    it('names the latest runs, not the window, when the system is failing', () => {
        expect(healthBasisTag(health({ health_state: 'failing', recent_runs: recent([true, true, true]) }))).toBe(
            'last 3 runs failed',
        );
    });
});

describe('healthExplainer', () => {
    it('says the score is worst-of, the window is fixed, and the latest runs are checked on their own', () => {
        const text = healthExplainer(health({})).join(' ');
        expect(text).toMatch(/worst enforced signal, not an average/);
        expect(text).toMatch(/last 30 days/);
        expect(text).toMatch(/range buttons on this page do not change it/);
        expect(text).toMatch(/latest 3 finished runs .* all 3 failed means Failing/);
        expect(text).toMatch(/older than 5 minutes/);
    });

    it('explains a shared service by the calls it served', () => {
        expect(healthExplainer(health({ signal_basis: 'segments' })).join(' ')).toMatch(/shared service/);
    });
});

describe('healthExplainer — which runs count', () => {
    it('says which latest runs count: empty polls skipped, deviations failed', () => {
        const text = healthExplainer(health({})).join(' ');
        expect(text).toContain('an empty poll) is skipped');
        expect(text).toContain('undeclared or no outcome counts as failed');
    });
});

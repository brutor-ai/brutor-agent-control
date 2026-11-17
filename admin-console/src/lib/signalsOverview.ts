/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The worst-of model behind Signals → Overview, kept free of React so the
 * ladder can be pinned without rendering. It mirrors `score_health` in the
 * control plane: suspended → unknown → silent → stalled → failing →
 * drifting → learning → degraded (any enforced signal below 80%) → healthy.
 * Detector-driven states (silent, stalled, failing, drifting) come from a
 * flag, not a score, so the row they belong to drives the verdict even when
 * its score reads fine.
 */

import { failingSignalLabel, type SystemHealth } from './aiSystemTypes';
import { recentRunsSentence, windowSpan } from './healthWindows';

/** The seven signals in the profile's order (see `ai_system_signal_profile`). */
export const OVERVIEW_SIGNALS: { id: string; label: string; scoreKey: keyof SystemHealth | null }[] = [
    { id: 'liveness', label: 'Liveness', scoreKey: 'liveness_score' },
    { id: 'behaviour', label: 'Behaviour', scoreKey: 'behaviour_score' },
    { id: 'reliability', label: 'Reliability', scoreKey: 'reliability_score' },
    { id: 'cost', label: 'Cost', scoreKey: 'cost_score' },
    { id: 'conformance', label: 'Conformance', scoreKey: 'conformance_score' },
    { id: 'oversight', label: 'Oversight', scoreKey: 'oversight_score' },
    // Review has no top-level score field; it only travels in
    // `signal_levels`.
    { id: 'review', label: 'Review', scoreKey: null },
];

/** Below this an enforced signal turns the state to `degraded`
 *  (`score_health`: `score < 0.8`). Pinned here so the row band and the
 *  verdict agree. */
export const DEGRADED_BELOW = 0.8;

/** Which signal a detector-driven state belongs to. Suspended is an
 *  autonomy decision, not a signal, and `unknown` means nothing was scored. */
export const STATE_SIGNAL: Partial<Record<string, string>> = {
    silent: 'liveness',
    stalled: 'reliability',
    failing: 'reliability',
    drifting: 'behaviour',
};

/** Sub-tabs of the Signals tab that show a signal's own evidence. */
export const SIGNAL_TAB: Partial<Record<string, string>> = {
    liveness: 'liveness',
    cost: 'cost',
    behaviour: 'trajectory',
    reliability: 'errors',
};

export interface SignalRow {
    id: string;
    label: string;
    score: number | null;
    level: 'enforced' | 'advisory' | 'not_applicable' | 'unmeasured';
    /** This row is the one the verdict rests on. */
    drives: boolean;
}

function signalScore(health: SystemHealth, sig: (typeof OVERVIEW_SIGNALS)[number]): number | null {
    const fromLevels = health.signal_levels?.[sig.id]?.score;
    if (fromLevels !== undefined && fromLevels !== null) return fromLevels;
    if (sig.scoreKey) {
        const v = health[sig.scoreKey];
        if (typeof v === 'number') return v;
    }
    return null;
}

/**
 * The worst signal and every row's standing, from the health payload alone
 * — pure so the tests can pin the ladder without rendering.
 */
export function deriveSignalRows(health: SystemHealth): SignalRow[] {
    const rows = OVERVIEW_SIGNALS.map((sig) => {
        const lvl = health.signal_levels?.[sig.id]?.level;
        const score = signalScore(health, sig);
        let level: SignalRow['level'];
        if (lvl === 'not_applicable') level = 'not_applicable';
        else if (lvl === 'advisory') level = 'advisory';
        else if (score === null) level = 'unmeasured';
        else level = 'enforced';
        return { id: sig.id, label: sig.label, score, level, drives: false };
    });

    const detectorSignal = STATE_SIGNAL[health.health_state];
    if (detectorSignal) {
        const row = rows.find((r) => r.id === detectorSignal);
        if (row) row.drives = true;
        return rows;
    }
    if (health.health_state === 'suspended' || health.health_state === 'unknown') return rows;
    // A critical dependency caps the verdict at degraded
    // before any score is consulted: no signal of this system drives it.
    if (isDependencyCapped(health)) return rows;

    // Score-driven states: the lowest enforced score is the verdict — for
    // `degraded` it is the failing one, for `healthy` it is the margin.
    let worst: SignalRow | null = null;
    for (const r of rows) {
        if (r.level !== 'enforced' || r.score === null) continue;
        if (worst === null || r.score < (worst.score as number)) worst = r;
    }
    if (worst) worst.drives = true;
    return rows;
}

/** The verdict is `degraded` because a critical dependency is not healthy
 *  (the scorer's cap, `dependency_cap`), not because of a signal here. */
export function isDependencyCapped(health: SystemHealth): boolean {
    return health.health_state === 'degraded' && Boolean(health.dependency_cap);
}

/** How an unscored signal reads. Oversight on a shared service that raised
 *  no approval hold has no loop to judge — "not applicable", never 0 %. */
export function notApplicableText(signalId: string, health: SystemHealth): string {
    if (signalId === 'oversight' && health.signal_basis === 'segments') {
        return 'Not applicable: raised no approval holds';
    }
    return 'Not scored for this kind';
}

/** The health verdict's own sentence on the latest finished runs — the
 *  control plane names the error causes there ("…all failed: model
 *  unavailable"), so the UI reads it rather than re-deriving a cause. */
export function recentRunsReason(health: SystemHealth): string | null {
    return health.reasons.find((r) => /finished runs/.test(r)) ?? null;
}

/** One sentence on what the verdict rests on. */
export function drivingSentence(health: SystemHealth, rows: SignalRow[]): string {
    const driver = rows.find((r) => r.drives);
    if (isDependencyCapped(health)) {
        const cap = health.dependency_cap!;
        const signal = failingSignalLabel(cap.failing_signal);
        return (
            `A dependency drives the verdict: critical dependency ${cap.name ?? cap.system_id ?? ''} ` +
            `is not healthy${signal ? ` (${signal})` : ''}, which caps this system at degraded ` +
            `whatever its own signals read.`
        );
    }
    switch (health.health_state) {
        case 'suspended':
            return 'Autonomy is suspended; no signal can lift the verdict until it is restored.';
        case 'unknown':
            return health.is_trustworthy
                ? 'No enforced signal has been scored yet.'
                : 'Too few runs were observed intact for any signal to be trusted.';
        case 'silent':
            return 'Liveness drives the verdict: the system has stopped calling through the gateway. Its score is a measure of the runs it did make, so it can read 100% while the system is silent.';
        case 'stalled':
            return 'Reliability drives the verdict: runs are starting but not finishing.';
        case 'failing': {
            // The server's reason names the cause; it already says "failing
            // now" and the period rate, so it stands on its own.
            const reason = recentRunsReason(health);
            if (reason) return `Reliability drives the verdict: ${reason.replace(/^./, (c) => c.toLowerCase())}`;
            const rate = health.recent_runs?.window_success_rate;
            const span = windowSpan(health.windows?.health_period_hours);
            return (
                `Reliability drives the verdict: ${recentRunsSentence(health.recent_runs) ?? 'the latest finished runs all failed.'} ` +
                `The system is failing now` +
                (rate !== null && rate !== undefined
                    ? `, even though ${Math.round(rate * 100)}% of runs over the ${span} window succeeded.`
                    : '.')
            );
        }
        case 'drifting':
            return 'Behaviour drives the verdict: open drift events stand against the baseline.';
        case 'learning':
            return 'Baselines are still learning; behaviour cannot be judged until they are ready.';
        default:
            break;
    }
    if (!driver || driver.score === null) return 'No enforced signal has been scored.';
    const pct = Math.round(driver.score * 100);
    return health.health_state === 'degraded'
        ? `${driver.label} drives the verdict: it is the weakest enforced signal at ${pct}%, below the ${Math.round(DEGRADED_BELOW * 100)}% floor.`
        : `Every enforced signal is at or above ${Math.round(DEGRADED_BELOW * 100)}%. ${driver.label} is the tightest margin at ${pct}%.`;
}

/**
 * The "?" beside each signal row: what the signal asks, how it is scored and
 * when it is not measured. Mirrors the control plane's scorers — liveness
 * (`ai_system_liveness_state`), behaviour (open drift against ready
 * baselines), reliability (`_run_facts` + `_recent_runs` + envelope),
 * cost (envelope, or `segment_cost_score` for a shared service),
 * conformance (`ai_system_conformance._score`), oversight
 * (`score_oversight`) and review (`AssuranceInboxService.review_score`).
 * A shared service is scored on the calls it serves, so three of them read
 * differently on the segment basis.
 */
export function signalHelp(signalId: string, health: SystemHealth): { title: string; text: string } | null {
    const span = windowSpan(health.windows?.health_period_hours);
    const recent = health.windows?.recent_runs ?? 3;
    const service = health.signal_basis === 'segments';
    switch (signalId) {
        case 'liveness':
            return {
                title: 'Liveness: is it still running?',
                text: service
                    ? `Judged on the calls other systems make to this service. 100% while it answers them; 50% when one of the latest ${recent} calls went unanswered; Silent when all of them did. No calls in the window is idle, not a fault.`
                    : 'Compares the system\'s activity with the liveness expectation set on the Liveness tab (continuous, scheduled, on-demand or sporadic). 100% means it is active as expected; Silent means it stopped calling when it was expected to. Every other signal is measured on traffic that arrived, so only this one notices a system that has gone quiet.',
            };
        case 'behaviour':
            return {
                title: 'Behaviour: does it still act like itself?',
                text: 'Once the baselines of the system\'s normal runs have been learned, any open drift event against them drops this to 50% and the state to Drifting. 100% means no open drift. Not measured while the baselines are still learning. Triage drift on the Drift tab.',
            };
        case 'reliability':
            return {
                title: 'Reliability: does it do its job?',
                text: service
                    ? `The share of calls served over the ${span} health window that were answered, capped by the share of its tool calls that succeeded. An answer with an outcome the service's contract does not declare counts against it.`
                    : `The success rate over the ${span} health window. A success is a run that completed without errors AND ended in a declared outcome that counts toward the baseline. ` +
                      `It is divided by successes + deviations (completed runs whose outcome was undeclared or not reported) + every failed run (errored or exhausted, policy blocks included — whatever the run was doing). ` +
                      `Runs whose declared outcome does not count toward the baseline, such as an empty poll, are in neither, so a system that keeps polling cannot look reliable while its real work fails; abandoned and cancelled runs are not decisive. ` +
                      `Replays and runs inside declared incident windows are left out. The latest ${recent} decisive finished runs are also checked on their own (empty polls skipped): one or two failed caps it at 50%, all ${recent} failed makes the system Failing, and the verdict names why they failed. ` +
                      `A breached reliability term in the operating envelope caps it too (50%, or 0% for a high-severity term).`,
            };
        case 'cost':
            return {
                title: 'Cost: is it within its declared budget?',
                text: service
                    ? 'Cost per served call against the service\'s own learned baseline: 100% with no open cost drift, 50% with one. Not measured until that baseline is ready.'
                    : 'Scored against the cost terms of the operating envelope in the system\'s active contract (cost per processed outcome or per declared outcome, cost per run or conversation, model calls, tokens, delegation depth, 30-day spend). A processed outcome is a run that completed in a declared outcome counting toward the baseline; empty polls are never averaged in. All terms met is 100%; a breached term caps it at 50%, or 0% for a high-severity term. Without an envelope there is no target, so it is not scored; getting more expensive than usual shows as drift instead.',
            };
        case 'conformance':
            return {
                title: 'Conformance: does it use only what it declared?',
                text: `Compares what the gateway saw over the ${span} health window with what the contract declares. Calls: each undeclared caller takes off 25 points; each granted resource it never used takes off 5. Outcomes: each undeclared outcome reported takes off 25; runs reporting no outcome, runs breaking the evidence their outcome expects, and delegate answers outside what this system expects take off 10 each; each declared outcome never seen takes off 5. Not measured when no calls were observed.`,
            };
        case 'oversight':
            return {
                title: 'Oversight: are human approvals handled?',
                text: 'When the system asks for human approval, the score is 100% minus the share of approval requests that lapsed with no decision. With at least 5 decisions all made by one person it is capped at 80%. Requests raised inside declared incident windows are not judged. Not scored when no approval was requested.',
            };
        case 'review':
            return {
                title: 'Review: is the assurance inbox kept up?',
                text: '100% while no critical assurance-inbox item for this system has stayed open longer than the 7-day review SLA; 50% once one has. Not measured until the system has had an inbox item.',
            };
        default:
            return null;
    }
}

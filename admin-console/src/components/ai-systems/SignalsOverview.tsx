/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Signals → Overview: the overall verdict and the signal that drives it.
 *
 * The health state is worst-of, never an average — one failing signal is
 * the verdict, and this panel says which one. It reads the same pinned
 * health payload the Mission Control card reads (`fetchSystemHealth`) and
 * the latest captured report's verdict (`fetchAssuranceHistory`), and it
 * colours both with the card's own style maps (`HEALTH_STYLE`,
 * `VERDICT_STYLE`, `assuranceStripe`) so the card in the fleet view and the
 * overview here can never disagree on what a colour means.
 *
 * The ladder it explains mirrors `score_health` in the control plane:
 * suspended → unknown (integrity floor / nothing scored) → silent →
 * stalled → failing → drifting → learning → degraded (any enforced signal
 * below 80%) → healthy. Detector-driven states (silent, stalled, failing,
 * drifting) come from a flag, not a score, so the row they belong to is
 * badged even when its score reads fine — a quiet system scores 100% on
 * liveness and is still silent.
 *
 * It also says what the verdict is computed over (`healthWindows`): the
 * fixed health window, the latest finished runs it judged for "failing
 * now", and that the page's range pills do not move either.
 */

import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, RefreshCw, XCircle } from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import { TABLE_CONTAINER, TABLE_TBODY, TABLE_TH, TABLE_THEAD } from '../ui/tableStyles';
import { ago } from '../../lib/relativeTime';
import {
    assuranceStripe,
    HEALTH_STYLE,
    VERDICT_STYLE,
    type AssuranceSummary,
    type SystemHealth,
    type VerdictStatus,
} from '../../lib/aiSystemTypes';
import {
    DEGRADED_BELOW,
    deriveSignalRows,
    drivingSentence,
    recentRunsReason,
    isDependencyCapped,
    notApplicableText,
    SIGNAL_TAB,
    signalHelp,
    STATE_SIGNAL,
    type SignalRow,
} from '../../lib/signalsOverview';
import { healthExplainer, recentRunsSentence, windowSpan } from '../../lib/healthWindows';
import { DependencyCapBanner, DependencyHealthList, SignalBasisNote } from './DependencyHealthList';
import { HelpTip } from '../ui/HelpTip';
import { recentRunChipText } from '../../lib/runOutcomes';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

function latestVerdict(reports: AssuranceSummary[]): AssuranceSummary | null {
    let best: AssuranceSummary | null = null;
    for (const r of reports) {
        if (!best) { best = r; continue; }
        const a = r.created_at ? Date.parse(r.created_at) : 0;
        const b = best.created_at ? Date.parse(best.created_at) : 0;
        if (a > b) best = r;
    }
    return best;
}

interface SignalsOverviewProps {
    aiSystemId: string;
    /** Jump to a signal's own sub-tab. */
    onOpenSignal?: (sub: string) => void;
    /** Open another page (App tab id) — a dependency's own signals. */
    setActiveTab?: (tab: string) => void;
}

export default function SignalsOverview({ aiSystemId, onOpenSignal, setActiveTab }: SignalsOverviewProps) {
    const { fetchSystemHealth, fetchAssuranceHistory, fetchAssuranceReport } = useGateway();
    const [health, setHealth] = useState<SystemHealth | null>(null);
    const [latest, setLatest] = useState<AssuranceSummary | null>(null);
    // How many calls a shared service's signals rest on.
    // Only the live report counts them, so it is read only for a system on
    // the segment basis, and only after the verdict has rendered.
    const [servedCalls, setServedCalls] = useState<{ id: string; n: number } | null>(null);
    const [readFailure, reads] = useReadFailures<'served'>();
    const [servedAsked, setServedAsked] = useState(0);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async (refresh = false) => {
        setLoading(true);
        setError(null);
        try {
            const [h, hist] = await Promise.all([
                fetchSystemHealth(aiSystemId, refresh),
                fetchAssuranceHistory(aiSystemId),
            ]);
            setHealth(h);
            setLatest(latestVerdict(hist?.reports ?? []));
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to load the overview');
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchSystemHealth, fetchAssuranceHistory]);

    useEffect(() => { void load(); }, [load]);

    const onSegments = health?.signal_basis === 'segments';
    useEffect(() => {
        if (!onSegments) return;
        let cancelled = false;
        fetchAssuranceReport(aiSystemId).then(
            (r) => {
                const service = r?.service as { served_calls?: unknown } | null | undefined;
                const n = service?.served_calls;
                if (cancelled) return;
                if (typeof n === 'number') setServedCalls({ id: aiSystemId, n });
                reads.loaded('served');
            },
            (err: unknown) => {
                // The sentence falls back to "the calls it served".
                if (!cancelled) reads.lost('served', 'Failed to load how many calls these signals rest on', err);
            },
        );
        return () => { cancelled = true; };
    }, [aiSystemId, onSegments, fetchAssuranceReport, servedAsked, reads]);

    if (error) {
        return (
            <div className="text-sm text-red-700 dark:text-red-300" data-testid="signals-overview-error">
                {error}
            </div>
        );
    }
    if (!health) {
        return (
            <div className="text-sm text-muted" data-testid="signals-overview-loading">
                Loading the overall verdict…
            </div>
        );
    }

    const rows = deriveSignalRows(health);
    const hs = HEALTH_STYLE[health.health_state] ?? HEALTH_STYLE.unknown;
    const verdictStatus: VerdictStatus | null = latest?.verdict_status ?? null;
    const vs = verdictStatus ? VERDICT_STYLE[verdictStatus] : null;
    const stripe = assuranceStripe(health.health_state, verdictStatus);
    const driver = rows.find((r) => r.drives) ?? null;
    const relaxations = health.profile_relaxations ?? [];

    return (
        <div className="space-y-4" data-testid="signals-overview">
            <ReadFailureNotice compact failure={readFailure('served')} onRetry={() => setServedAsked((n) => n + 1)} />
            {/* The verdict card — same stripe, chips and wording as the
                Mission Control card, so the fleet view and this page agree. */}
            <section
                className="relative overflow-hidden rounded-lg border border-default bg-surface"
                data-testid="overview-verdict-card"
            >
                <div
                    className={`absolute inset-y-0 left-0 w-1.5 ${stripe}`}
                    data-testid="overview-stripe"
                    aria-hidden="true"
                />
                <div className="pl-5 pr-4 py-4 space-y-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="text-xs uppercase tracking-wide text-muted">Overall verdict</span>
                            <span className="text-xs text-muted" data-testid="overview-window">
                                {windowSpan(health.windows?.health_period_hours)} window
                            </span>
                            <span
                                className={`rounded-full px-2.5 py-0.5 text-sm font-medium ${hs.chip}`}
                                data-testid="overview-health-chip"
                            >
                                {hs.label}
                            </span>
                            {vs ? (
                                <span
                                    className={`rounded-full px-2.5 py-0.5 text-sm font-medium ${vs.chip}`}
                                    data-testid="overview-verdict-chip"
                                    title={`Latest captured report${latest?.created_at ? `, ${ago(latest.created_at)}` : ''}`}
                                >
                                    {vs.label}
                                </span>
                            ) : (
                                <span className="text-xs text-muted" data-testid="overview-verdict-none">
                                    No assurance report captured yet
                                </span>
                            )}
                        </div>
                        <div className="flex items-center gap-2 text-xs text-muted">
                            <span data-testid="overview-as-of">as of {ago(health.computed_at)}</span>
                            <button
                                type="button"
                                onClick={() => void load(true)}
                                disabled={loading}
                                className="rounded p-1 hover:bg-surface-alt disabled:opacity-50"
                                title="Recompute now"
                                aria-label="Recompute the verdict"
                                data-testid="overview-refresh"
                            >
                                <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
                            </button>
                        </div>
                    </div>

                    {health.dependency_cap && isDependencyCapped(health) && (
                        <DependencyCapBanner cap={health.dependency_cap} open={setActiveTab} />
                    )}
                    <p className="text-sm text-default" data-testid="overview-driver">
                        {drivingSentence(health, rows)}
                    </p>
                    <SignalBasisNote
                        basis={health.signal_basis}
                        servedCalls={servedCalls?.id === aiSystemId ? servedCalls.n : null}
                    />
                    {health.reasons
                        // Said by the driver sentence and the recent-runs row below.
                        .filter((r) => !/weakest component|finished runs/.test(r))
                        .map((reason, i) => (
                            <p key={i} className="text-sm text-muted" data-testid="overview-reason">
                                {reason}
                            </p>
                        ))}
                    {health.recent_runs && health.recent_runs.considered > 0 && (
                        <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="overview-recent-runs">
                            <span className="text-muted">
                                Latest finished runs{' '}
                                <span className="text-xs">(checked on their own, newest first)</span>:
                            </span>
                            {health.recent_runs.latest.map((run) => (
                                <span
                                    key={run.run_id}
                                    className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
                                        run.failed ? HEALTH_STYLE.failing.chip : HEALTH_STYLE.healthy.chip
                                    }`}
                                    title={`${run.run_id}${run.ended_at ? ` · ended ${ago(run.ended_at)}` : ''}`}
                                    data-testid="overview-recent-run"
                                    data-failed={run.failed}
                                >
                                    {run.failed ? <XCircle className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
                                    {recentRunChipText(run)}
                                </span>
                            ))}
                            <span className="text-xs text-muted" data-testid="overview-recent-runs-why">
                                {/* The verdict's own sentence names the error
                                    causes; fall back to the plain count. */}
                                {health.health_state === 'failing'
                                    ? (recentRunsSentence(health.recent_runs) ?? '').replace(/^./, (c) => c.toUpperCase())
                                    : (recentRunsReason(health) ??
                                      (recentRunsSentence(health.recent_runs) ?? '').replace(/^./, (c) => c.toUpperCase()))}
                            </span>
                        </div>
                    )}
                    <details className="text-sm" data-testid="overview-explainer">
                        <summary className="cursor-pointer text-xs text-muted hover:text-default">
                            How health is calculated
                        </summary>
                        <ul className="mt-2 list-disc space-y-1 pl-5 text-muted">
                            {healthExplainer(health).map((line, i) => (
                                <li key={i}>{line}</li>
                            ))}
                        </ul>
                    </details>
                </div>
            </section>

            {/* One row per signal, worst-of made visible. */}
            <section className={`${TABLE_CONTAINER} overflow-x-auto`}>
                <table className="w-full text-sm" data-testid="overview-signals">
                    <thead className={TABLE_THEAD}>
                        <tr>
                            <th className={TABLE_TH}>Signal</th>
                            <th className={`${TABLE_TH} text-right`}>Score</th>
                            <th className={TABLE_TH}>Standing</th>
                            <th className={`${TABLE_TH} sr-only`}>Open</th>
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {rows.map((row) => {
                            const sub = SIGNAL_TAB[row.id];
                            const help = signalHelp(row.id, health);
                            return (
                                <tr
                                    key={row.id}
                                    className={row.drives ? 'bg-surface-alt' : undefined}
                                    data-testid={`overview-signal-row-${row.id}`}
                                >
                                    <td className="px-4 py-2 font-medium text-default">
                                        <span className="inline-flex items-center gap-1.5">
                                            {row.label}
                                            {help && (
                                                <HelpTip
                                                    title={help.title}
                                                    text={help.text}
                                                    ariaLabel={`How ${row.label} is scored`}
                                                />
                                            )}
                                        </span>
                                        {row.level === 'advisory' && (
                                            <span
                                                className="ml-2 rounded bg-surface-alt px-1 py-0.5 text-[10px] uppercase tracking-wide text-muted"
                                                title="Shown but not counted toward the health verdict"
                                                data-testid={`overview-signal-advisory-${row.id}`}
                                            >
                                                advisory
                                            </span>
                                        )}
                                    </td>
                                    <td
                                        className="px-4 py-2 text-right tabular-nums text-default"
                                        data-testid={`overview-signal-score-${row.id}`}
                                    >
                                        {row.level === 'not_applicable'
                                            ? 'n/a'
                                            : row.score === null
                                              ? 'not measured'
                                              : `${Math.round(row.score * 100)}%`}
                                    </td>
                                    <td className="px-4 py-2">
                                        <SignalStanding row={row} health={health} />
                                    </td>
                                    <td className="px-4 py-2 text-right">
                                        {sub && onOpenSignal && (
                                            <button
                                                type="button"
                                                className="text-xs text-primary-700 hover:underline"
                                                onClick={() => onOpenSignal(sub)}
                                                data-testid={`overview-open-${row.id}`}
                                            >
                                                Open
                                            </button>
                                        )}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </section>

            {relaxations.length > 0 && (
                <section className="text-xs text-muted space-y-1" data-testid="overview-relaxations">
                    <p className="font-medium text-default">
                        Relaxed for a {health.system_kind ?? 'system'} of this kind
                    </p>
                    <ul className="list-disc pl-5 space-y-0.5">
                        {relaxations.map((r) => (
                            <li key={r.signal}>
                                <span className="capitalize">{r.signal}</span> is {r.level.replace('_', ' ')}: {r.reason}
                            </li>
                        ))}
                    </ul>
                </section>
            )}
            <DependencyHealthList entries={health.dependency_health} open={setActiveTab} />

            {driver === null && health.health_state !== 'suspended' && health.health_state !== 'unknown' && !isDependencyCapped(health) && (
                <p className="text-xs text-muted" data-testid="overview-no-driver">
                    No enforced signal has a score yet, so nothing drives the verdict.
                </p>
            )}
        </div>
    );
}

/** The chip in the Standing column: the card's colours, per row. */
function SignalStanding({ row, health }: { row: SignalRow; health: SystemHealth }) {
    const healthState = health.health_state;
    const base = 'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium';
    if (row.level === 'not_applicable') {
        return (
            <span className={`${base} ${HEALTH_STYLE.unknown.chip}`} data-testid={`overview-signal-standing-${row.id}`}>
                {notApplicableText(row.id, health)}
            </span>
        );
    }
    if (row.drives && STATE_SIGNAL[healthState] === row.id) {
        // A detector state: the card's own chip for that state, on the row
        // it belongs to.
        const st = HEALTH_STYLE[healthState];
        return (
            <span className={`${base} ${st.chip}`} data-testid={`overview-signal-standing-${row.id}`}>
                {st.label} · drives the verdict
            </span>
        );
    }
    if (row.score === null) {
        return (
            <span className={`${base} ${HEALTH_STYLE.unknown.chip}`} data-testid={`overview-signal-standing-${row.id}`}>
                Not measured
            </span>
        );
    }
    const below = row.score < DEGRADED_BELOW;
    const style = below ? HEALTH_STYLE.degraded : HEALTH_STYLE.healthy;
    const text = row.level === 'advisory'
        ? (below ? 'Below floor (advisory)' : 'Within floor (advisory)')
        : below ? 'Below floor' : 'Within floor';
    return (
        <span className={`${base} ${style.chip}`} data-testid={`overview-signal-standing-${row.id}`}>
            {text}
            {row.drives && (
                <span data-testid={`overview-drives-${row.id}`}>
                    {' · '}{below ? 'drives the verdict' : 'tightest margin'}
                </span>
            )}
        </span>
    );
}

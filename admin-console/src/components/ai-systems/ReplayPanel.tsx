/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Replay.
 *
 * Recorded production traffic frozen into a suite, and a candidate contract
 * evaluated against it before promotion. Three things shape this panel, all of
 * them about not letting a gate look stronger than its evidence:
 *
 * **Coverage and its gaps are the evidence, so they are shown together.**
 * A suite that samples "the last fifty runs" encodes the happy path and gates
 * nothing. The build response reports which strata were sampled *and which
 * were present in the population but missed*, and the gaps are rendered as
 * prominently as the coverage — a gate is only as good as what it covers, and
 * an operator who never sees the gaps will over-trust a green result.
 *
 * **Building a suite and gating releases on it are separate decisions.**
 * `bind_to_gate` is off by default in the API for that reason, and the same
 * choice is made here: the coverage report is written to be read before the
 * suite starts deciding whether releases ship.
 *
 * **A run is asynchronous and says so.** The control plane writes the request
 * row; gateway-core executes it. The two are joined by the row rather than a
 * call, so neither service goes down with the other — which means the UI
 * cannot report a verdict on the click. It shows "queued", polls, and stops
 * polling when the run reports itself finished.
 *
 * The verdict breakdown is reported per-kind rather than as one pass/fail
 * number because the kinds mean different things to a release: a changed
 * *shape* is usually a schema edit, while a changed *outcome* is a call that
 * would now be allowed or denied differently. Only the second is a reason to
 * stop a promotion, and `critical_outcome_changed` is called out separately
 * again because one critical change matters more than a good pass rate.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
    AlertTriangle,
    CheckCircle2,
    Layers,
    Loader2,
    Play,
    RefreshCw,
    ShieldAlert,
    XCircle,
} from 'lucide-react';

import { HelpTip } from '../ui/HelpTip';

import { useGateway } from '../../hooks/useGateway';
import {
    BuildSuiteResponse,
    REPLAY_GATE_LABELS,
    ReplayGateState,
    ReplayStatus,
} from '../../lib/aiSystemTypes';

interface ReplayPanelProps {
    aiSystemId: string;
    /** Poll interval while a run is outstanding. Exposed only so tests can
     *  exercise the polling loop without fake timers, which deadlock against
     *  the async fetch inside it. */
    pollMs?: number;
}

/** How often to re-check a run the proxy is executing. Slow enough not to
 *  hammer the control plane, fast enough that a short suite finishes while
 *  the operator is still looking at the panel. */
const POLL_MS = 4000;

/** Give up polling rather than spin forever if the executor never picks the
 *  row up. Silence is reported as silence — "still queued" is a real state and
 *  a stuck queue must not render as a run in progress indefinitely. */
const POLL_TIMEOUT_MS = 5 * 60 * 1000;

const GATE_TONE: Record<ReplayGateState, string> = {
    no_suite: 'border-default bg-surface-subtle text-secondary',
    never_run: 'border-default bg-surface-subtle text-secondary',
    not_replayable: 'border-amber-200 bg-amber-50 text-amber-900',
    running: 'border-sky-200 bg-sky-50 text-sky-900',
    passing: 'border-emerald-200 bg-emerald-50 text-emerald-900',
    failing: 'border-red-200 bg-red-50 text-red-900',
};

function GateIcon({ state }: { state: ReplayGateState }) {
    if (state === 'passing') return <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />;
    if (state === 'failing') return <XCircle className="w-4 h-4 shrink-0 mt-0.5" />;
    if (state === 'not_replayable') return <ShieldAlert className="w-4 h-4 shrink-0 mt-0.5" />;
    if (state === 'running') return <Loader2 className="w-4 h-4 shrink-0 mt-0.5 animate-spin" />;
    return <Layers className="w-4 h-4 shrink-0 mt-0.5" />;
}

function pct(v?: number | null): string {
    if (v === null || v === undefined) return '—';
    return `${(v * 100).toFixed(1)}%`;
}

export function ReplayPanel({ aiSystemId, pollMs = POLL_MS }: ReplayPanelProps) {
    const { fetchReplayStatus, buildReplaySuite, requestReplayRun } = useGateway();

    const [status, setStatus] = useState<ReplayStatus | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [showBuild, setShowBuild] = useState(false);
    const [buildResult, setBuildResult] = useState<BuildSuiteResponse | null>(null);
    const [queued, setQueued] = useState(false);
    /** The suite being inspected. Null means "whatever is bound to the gate".
     *  Set after building so an unbound suite can be reviewed and run — the
     *  gate keeps reading the bound one either way. */
    const [inspecting, setInspecting] = useState<string | null>(null);

    // Build form
    const [name, setName] = useState('');
    const [cases, setCases] = useState(30);
    const [windowDays, setWindowDays] = useState(30);
    const [threshold, setThreshold] = useState(0.9);
    const [bindToGate, setBindToGate] = useState(false);

    const pollUntil = useRef<number>(0);

    const load = useCallback(async () => {
        try {
            setError('');
            const s = await fetchReplayStatus(aiSystemId, inspecting ?? undefined);
            setStatus(s);
            return s;
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to load replay status');
            setStatus(null);
            return null;
        } finally {
            setLoading(false);
        }
    }, [aiSystemId, fetchReplayStatus, inspecting]);

    useEffect(() => {
        void load();
    }, [load]);

    // Poll only while a run is outstanding. `is_finished` comes from the run
    // itself rather than being inferred from a status string, so a state the
    // UI has never heard of still terminates the poll.
    useEffect(() => {
        if (!queued) return;
        const timer = setInterval(() => {
            if (Date.now() > pollUntil.current) {
                setQueued(false);
                return;
            }
            void load().then((s) => {
                if (s?.last_run?.is_finished) setQueued(false);
            });
        }, pollMs);
        return () => clearInterval(timer);
    }, [queued, load, pollMs]);

    const onBuild = async () => {
        setBusy(true);
        setError('');
        try {
            const res = await buildReplaySuite(aiSystemId, {
                name: name.trim() || `Suite ${new Date().toISOString().slice(0, 10)}`,
                target_case_count: cases,
                window_days: windowDays,
                pass_threshold: threshold,
                bind_to_gate: bindToGate,
            });
            setBuildResult(res);
            setShowBuild(false);
            setInspecting(res.suite_id);
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to build suite');
        } finally {
            setBusy(false);
        }
    };

    const onRun = async () => {
        setBusy(true);
        setError('');
        try {
            await requestReplayRun(
                aiSystemId,
                (status?.suite_id ?? buildResult?.suite_id) ?? undefined
            );
            pollUntil.current = Date.now() + POLL_TIMEOUT_MS;
            setQueued(true);
            await load();
        } catch (err: unknown) {
            setError(err instanceof Error ? err.message : 'Failed to request a replay run');
        } finally {
            setBusy(false);
        }
    };

    const run = status?.last_run ?? null;

    // A suite that was just built but not bound is still runnable — the run
    // endpoint takes an explicit suite id. That is the point of the default:
    // run it, read the verdict, and only then decide whether releases should
    // depend on it.
    const activeSuiteId = status?.suite_id ?? buildResult?.suite_id ?? null;
    const notGating = Boolean(status?.suite_id) && status?.bound === false;

    return (
        <div className="space-y-4" data-testid="replay-panel">
            <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-2">
                    <h3 className="text-sm font-semibold text-default">Replay</h3>
                    <HelpTip
                        text={
                            'Replay re-evaluates every recorded step’s authorization decision ' +
                            'under a candidate contract. It does not re-run your agent — the loop ' +
                            'that decides what to call next lives in your process, not ours.'
                        }
                    />
                </div>
                <div className="flex items-center gap-2">
                    {activeSuiteId && (
                        <button
                            type="button"
                            onClick={() => void onRun()}
                            disabled={busy || queued || status?.replayable === false}
                            data-testid="run-replay"
                            className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover disabled:opacity-50"
                        >
                            <Play className="w-4 h-4" />
                            {queued ? 'Running…' : 'Run replay'}
                        </button>
                    )}
                    <button
                        type="button"
                        onClick={() => setShowBuild((v) => !v)}
                        data-testid="toggle-build"
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                    >
                        <Layers className="w-4 h-4" />
                        {status?.bound ? 'Build new suite' : 'Build a suite'}
                    </button>
                    <button
                        type="button"
                        onClick={() => void load()}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm border border-strong rounded-lg hover:bg-surface-hover"
                    >
                        <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
                    </button>
                </div>
            </div>

            {error && (
                <div className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded">
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                    <span className="text-sm text-red-800">{error}</span>
                </div>
            )}

            {/* Gate verdict — what the promotion gate makes of this suite. */}
            {status && (
                <div
                    className={`flex items-start gap-3 p-3 border rounded-lg ${GATE_TONE[status.gate_state]}`}
                    data-testid="replay-gate-state"
                >
                    <GateIcon state={status.gate_state} />
                    <div className="min-w-0">
                        <p className="text-sm font-medium">
                            {REPLAY_GATE_LABELS[status.gate_state]}
                        </p>
                        {status.not_replayable_reason && (
                            <p className="text-sm mt-1" data-testid="not-replayable-reason">
                                {status.not_replayable_reason}
                            </p>
                        )}
                        {status.gate_state === 'no_suite' && !status.suite_id && (
                            <p className="text-sm mt-1">
                                Nothing gates promotion yet. Freeze a sample of recorded runs
                                into a suite, read its coverage, then decide whether releases
                                should depend on it.
                            </p>
                        )}
                        {status.gate_state === 'no_suite' && status.suite_id && (
                            <p className="text-sm mt-1">
                                A suite exists but none is bound, so nothing is checking this
                                system before a promotion. Its own result is below.
                            </p>
                        )}
                        {status.gate_state === 'never_run' && (
                            <p className="text-sm mt-1">
                                The suite exists but has never been executed, so it is not
                                evidence of anything yet.
                            </p>
                        )}
                    </div>
                </div>
            )}

            {/* Build form */}
            {showBuild && (
                <div
                    className="p-4 border border-default rounded-lg bg-surface space-y-3"
                    data-testid="build-form"
                >
                    <p className="text-sm text-secondary">
                        Sampling is stratified across terminal states, step-count deciles and
                        tool signatures, <strong>rarest first</strong> — so the single
                        exhausted run among two hundred survives a sample of thirty.
                    </p>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        <label className="block">
                            <span className="block text-xs text-muted uppercase tracking-wider mb-1">
                                Suite name
                            </span>
                            <input
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                placeholder="e.g. pre-v5-promotion"
                                className="w-full px-3 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                            />
                        </label>
                        <label className="block">
                            <span className="block text-xs text-muted uppercase tracking-wider mb-1">
                                Target cases
                            </span>
                            <input
                                type="number"
                                min={1}
                                max={500}
                                value={cases}
                                onChange={(e) => setCases(Number(e.target.value))}
                                className="w-full px-3 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                            />
                        </label>
                        <label className="block">
                            <span className="block text-xs text-muted uppercase tracking-wider mb-1">
                                Window (days)
                            </span>
                            <input
                                type="number"
                                min={1}
                                max={365}
                                value={windowDays}
                                onChange={(e) => setWindowDays(Number(e.target.value))}
                                className="w-full px-3 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                            />
                        </label>
                        <label className="block">
                            <span className="block text-xs text-muted uppercase tracking-wider mb-1">
                                Pass threshold
                            </span>
                            <input
                                type="number"
                                min={0}
                                max={1}
                                step={0.05}
                                value={threshold}
                                onChange={(e) => setThreshold(Number(e.target.value))}
                                className="w-full px-3 py-1.5 border border-strong rounded-lg bg-surface text-sm"
                            />
                        </label>
                    </div>
                    <label className="flex items-start gap-2 text-sm text-secondary">
                        <input
                            type="checkbox"
                            checked={bindToGate}
                            onChange={(e) => setBindToGate(e.target.checked)}
                            data-testid="bind-to-gate"
                            className="mt-0.5"
                        />
                        <span>
                            Bind to the promotion gate immediately.
                            <span className="text-muted">
                                {' '}Off by default — the coverage report below is worth reading
                                before this suite starts deciding whether releases ship.
                            </span>
                        </span>
                    </label>
                    <button
                        type="button"
                        onClick={() => void onBuild()}
                        disabled={busy}
                        data-testid="build-suite"
                        className="px-3 py-1.5 text-sm rounded-lg bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
                    >
                        {busy ? 'Building…' : 'Freeze suite'}
                    </button>
                </div>
            )}

            {/* The coverage report from the most recent build. */}
            {buildResult && (
                <div
                    className="p-4 border border-default rounded-lg bg-surface space-y-3"
                    data-testid="build-result"
                >
                    <div className="flex flex-wrap gap-4 text-sm">
                        <span className="text-secondary">
                            Sampled <strong className="text-default">{buildResult.cases_created}</strong>{' '}
                            of <strong className="text-default">{buildResult.eligible_runs}</strong>{' '}
                            eligible runs
                        </span>
                        {buildResult.bound_to_gate && (
                            <span className="text-emerald-700">Bound to the promotion gate</span>
                        )}
                    </div>

                    {buildResult.suite_id !== status?.suite_id && (
                        <StratumBars coverage={buildResult.coverage} />
                    )}

                    {buildResult.coverage_gaps.length > 0 && (
                        <div
                            className="flex items-start gap-2 p-3 border border-amber-200 bg-amber-50 rounded"
                            data-testid="coverage-gaps"
                        >
                            <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
                            <div className="text-sm text-amber-900">
                                <p className="font-medium">
                                    Not covered: {buildResult.coverage_gaps.join(', ')}
                                </p>
                                <p className="mt-1">
                                    These strata exist in production traffic but no case was
                                    sampled from them. A pass from this suite says nothing about
                                    them.
                                </p>
                            </div>
                        </div>
                    )}

                    {buildResult.warnings.map((w) => (
                        <p key={w} className="text-sm text-amber-800">
                            {w}
                        </p>
                    ))}


                </div>
            )}

            {notGating && (
                <p className="text-sm text-muted" data-testid="not-bound-note">
                    You are inspecting a suite that is <strong>not gating releases</strong>.
                    Run it and read the verdict here; to make promotion depend on it, build
                    again with &ldquo;bind to the promotion gate&rdquo; ticked.
                </p>
            )}

            {/* The suite under inspection. */}
            {status?.suite_id && (
                <div className="p-4 border border-default rounded-lg bg-surface space-y-3">
                    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
                        <span className="text-default font-medium" data-testid="suite-name">
                            {status.suite_name}
                        </span>
                        <span className="text-secondary">{status.case_count} cases</span>
                        <span className="text-secondary">
                            pass threshold {pct(status.pass_threshold)}
                        </span>
                        {!status.enabled && (
                            <span className="text-amber-700">disabled</span>
                        )}
                    </div>
                    <StratumBars coverage={status.coverage} />
                </div>
            )}

            {/* The last run. */}
            {run && (
                <div
                    className="p-4 border border-default rounded-lg bg-surface space-y-3"
                    data-testid="last-run"
                >
                    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
                        <span className="text-default font-medium">Last run</span>
                        <span className="text-secondary">{run.status}</span>
                        <span className="text-secondary">
                            pass rate <strong className="text-default">{pct(run.pass_rate)}</strong>
                        </span>
                        {run.started_at && (
                            <span className="text-muted text-xs">
                                {new Date(run.started_at).toLocaleString()}
                            </span>
                        )}
                    </div>

                    {run.critical_outcome_changed > 0 && (
                        <div
                            className="flex items-start gap-2 p-3 border border-red-200 bg-red-50 rounded"
                            data-testid="critical-change"
                        >
                            <ShieldAlert className="w-4 h-4 text-red-600 shrink-0 mt-0.5" />
                            <span className="text-sm text-red-800">
                                <strong>{run.critical_outcome_changed}</strong> critical
                                authorization outcome{run.critical_outcome_changed === 1 ? '' : 's'}{' '}
                                changed. One of these outweighs a good pass rate — read the
                                cases before promoting.
                            </span>
                        </div>
                    )}

                    <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                        <Verdict label="Matched" value={run.matched} hint="Same decision as recorded" />
                        <Verdict
                            label="Outcome changed"
                            value={run.outcome_changed}
                            tone={run.outcome_changed > 0 ? 'warn' : undefined}
                            hint="Would now be allowed or denied differently — the release-relevant one"
                        />
                        <Verdict
                            label="Policy changed"
                            value={run.policy_changed}
                            hint="A different rule decided it, same outcome"
                        />
                        <Verdict
                            label="Shape changed"
                            value={run.shape_changed}
                            hint="Request or response shape differs; usually a schema edit"
                        />
                        <Verdict
                            label="Failed"
                            value={run.failed}
                            tone={run.failed > 0 ? 'warn' : undefined}
                            hint="The case could not be evaluated"
                        />
                        <Verdict
                            label="Skipped"
                            value={run.skipped}
                            hint="Excluded from the pass rate entirely"
                        />
                    </div>

                    {run.error_message && (
                        <p className="text-sm text-red-700">{run.error_message}</p>
                    )}
                </div>
            )}

            {queued && (
                <p className="text-sm text-muted" data-testid="run-queued">
                    Queued. The request is recorded and the gateway runs it; the verdict appears here when it finishes.
                </p>
            )}

            {!loading && !status?.bound && !showBuild && !error && (
                <p className="text-sm text-muted">
                    Replay turns your own audit log into an eval set — no extra instrumentation,
                    because the steps were recorded as a byproduct of enforcing policy on them.
                </p>
            )}
        </div>
    );
}

/** Cases per stratum. Bars rather than a table: the shape of the sample is the
 *  point, and one stratum dwarfing the rest is what an operator needs to see. */
function StratumBars({ coverage }: { coverage: Record<string, number> }) {
    const entries = Object.entries(coverage);
    if (entries.length === 0) return null;
    const max = Math.max(1, ...entries.map(([, n]) => n));
    return (
        <div className="space-y-1" data-testid="stratum-bars">
            {entries.map(([stratum, n]) => (
                <div key={stratum} className="flex items-center gap-3">
                    <span className="w-40 text-xs text-muted text-right shrink-0 truncate" title={stratum}>
                        {stratum}
                    </span>
                    <div className="flex-1 h-3 bg-surface-subtle rounded overflow-hidden min-h-0 min-w-0">
                        <div
                            className="h-full bg-primary-500"
                            style={{ width: `${(n / max) * 100}%` }}
                        />
                    </div>
                    <span className="w-8 text-xs text-secondary tabular-nums">{n}</span>
                </div>
            ))}
        </div>
    );
}

function Verdict({
    label,
    value,
    hint,
    tone,
}: {
    label: string;
    value: number;
    hint: string;
    tone?: 'warn';
}) {
    return (
        <div className="p-3 border border-default rounded-lg" title={hint}>
            <div className="text-xs text-muted uppercase tracking-wider">{label}</div>
            <div
                className={`text-lg font-semibold mt-0.5 tabular-nums ${
                    tone === 'warn' ? 'text-amber-700' : 'text-default'
                }`}
            >
                {value}
            </div>
        </div>
    );
}

export default ReplayPanel;

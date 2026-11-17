/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Trajectory Explorer
 *
 * The operator-facing answer to the two questions the run list cannot reach:
 *
 *   "All tool calls that originated from Step X inside Run Y,
 *    and show their parent turns."
 *   "The turn where a check tripped, and what ran shortly after."
 *
 * Two panes, because those are two different jobs. FIND locates a trajectory
 * across the fleet (by system, capability, phase, or blocked-only). INSPECT
 * opens one run as Run → Step → Turn → Action.
 *
 * Honesty rules carried from the ledger, and visible in the UI rather than
 * only in the payload:
 *
 *   - a run whose client asserted no phases reads **unphased**, never "1 step";
 *   - a turn the gateway inferred is badged `~n`, never shown as reported;
 *   - the consequence pane says *co-occurrence*, and the column is
 *     "actions apart" — this view does not claim causation, and must not be
 *     read as though it does.
 */
import { useCallback, useEffect, useState } from 'react';

import { HelpTip } from '../ui/HelpTip';
import { useGateway } from '../../hooks/useGateway';
import { TABLE_THEAD, TABLE_TH_TYPE, TABLE_TBODY } from '../ui/tableStyles';
import type {
    ActionHit,
    ConsequencePair,
    TrajectoryResponse,
} from '../../lib/aiSystemTypes';
import { runBackground } from '../../lib/asyncSafe';
import { useReadFailures } from '../../hooks/useRead';
import ReadFailureNotice from '../common/ReadFailureNotice';

const TYPE_CLASS: Record<string, string> = {
    llm: 'bg-sky-100 text-sky-800',
    mcp: 'bg-violet-100 text-violet-800',
    'virtual-mcp': 'bg-violet-100 text-violet-800',
    a2a: 'bg-orange-100 text-orange-800',
    skill: 'bg-teal-100 text-teal-800',
};

const money = (n?: number | null) =>
    n === null || n === undefined ? '—' : `$${n.toFixed(4)}`;

function TypeBadge({ t }: { t?: string | null }) {
    if (!t) return <span className="text-muted">—</span>;
    return (
        <span
            className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                TYPE_CLASS[t] ?? 'bg-slate-100 text-slate-700'
            }`}
        >
            {t}
        </span>
    );
}

export default function TrajectoryExplorer({
    aiSystemId,
    initialRunId,
}: {
    aiSystemId?: string;
    initialRunId?: string;
}) {
    const { fetchRunTrajectory, fetchRunConsequences, searchActions } = useGateway();

    // ── FIND ────────────────────────────────────────────────────────
    const [capability, setCapability] = useState('');
    const [proxyType, setProxyType] = useState('');
    const [stepName, setStepName] = useState('');
    const [onlyBlocked, setOnlyBlocked] = useState(false);
    const [periodHours, setPeriodHours] = useState(168);
    const [hits, setHits] = useState<ActionHit[]>([]);
    const [searching, setSearching] = useState(false);
    const [searchErr, setSearchErr] = useState('');

    // ── INSPECT ─────────────────────────────────────────────────────
    const [runId, setRunId] = useState(initialRunId ?? '');
    const [traj, setTraj] = useState<TrajectoryResponse | null>(null);
    const [pairs, setPairs] = useState<ConsequencePair[]>([]);
    const [within, setWithin] = useState(10);
    const [stepFilter, setStepFilter] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [loadErr, setLoadErr] = useState('');

    const search = useCallback(async () => {
        setSearching(true);
        setSearchErr('');
        try {
            const res = await searchActions({
                ai_system_id: aiSystemId,
                capability: capability || undefined,
                proxy_type: proxyType || undefined,
                step_name: stepName || undefined,
                only_blocked: onlyBlocked || undefined,
                period_hours: periodHours,
                limit: 200,
            });
            setHits(res.actions ?? []);
        } catch (e) {
            setSearchErr(e instanceof Error ? e.message : 'Search failed');
        } finally {
            setSearching(false);
        }
    }, [aiSystemId, capability, proxyType, stepName, onlyBlocked, periodHours, searchActions]);

    const inspect = useCallback(
        async (id: string, step?: string | null) => {
            if (!id) return;
            setLoading(true);
            setLoadErr('');
            try {
                const [t, c] = await Promise.all([
                    fetchRunTrajectory(id, step ?? undefined),
                    fetchRunConsequences(id, within),
                ]);
                setTraj(t);
                setPairs(c.pairs ?? []);
            } catch (e) {
                setLoadErr(e instanceof Error ? e.message : 'Failed to load trajectory');
                setTraj(null);
                setPairs([]);
            } finally {
                setLoading(false);
            }
        },
        [within, fetchRunTrajectory, fetchRunConsequences],
    );

    useEffect(() => {
        if (initialRunId) void inspect(initialRunId, null);
    }, [initialRunId, inspect]);

    // ── Observed vocabulary ─────────────────────────────────────────
    // One broad search on arrival does two jobs: the tab opens showing
    // the system's recent actions instead of an empty form, and the
    // distinct capabilities / steps / run ids seed the datalists so an
    // operator picks from what actually happened rather than guessing
    // free-text values.
    const [observed, setObserved] = useState<{
        caps: string[];
        steps: string[];
        runs: string[];
    }>({ caps: [], steps: [], runs: [] });
    const [readFailure, reads] = useReadFailures<'recent'>();
    const [recentAsked, setRecentAsked] = useState(0);

    useEffect(() => {
        let cancelled = false;
        runBackground((async () => {
            try {
                const res = await searchActions({
                    ai_system_id: aiSystemId,
                    period_hours: 168,
                    limit: 200,
                });
                if (cancelled) return;
                const actions = res.actions ?? [];
                const uniq = (xs: (string | null | undefined)[]) =>
                    [...new Set(xs.filter((x): x is string => Boolean(x)))];
                setObserved({
                    caps: uniq(actions.map((a) => a.mcp_capability_name)).sort(),
                    steps: uniq(actions.map((a) => a.step_name)).sort(),
                    runs: uniq(actions.map((a) => a.root_task_id)).slice(0, 25),
                });
                // Populate the table only if the operator hasn't searched.
                setHits((prev) => (prev.length === 0 ? actions : prev));
                reads.loaded('recent');
            } catch (err) {
                // The form still works by hand; the table below is not "no actions".
                if (!cancelled) reads.lost('recent', 'Failed to load the recent actions of this system', err);
            }
        })(), 'async work');
        return () => {
            cancelled = true;
        };
    }, [aiSystemId, searchActions, recentAsked, reads]);

    return (
        <div
            className="grid grid-cols-1 lg:grid-cols-5 gap-4 items-start"
            data-testid="trajectory-explorer"
        >
            <ReadFailureNotice className="lg:col-span-5 !p-3" failure={readFailure('recent')} onRetry={() => setRecentAsked((n) => n + 1)} />
            {/* ── FIND (60%) ───────────────────────────────────────────
                Side by side rather than stacked: the workflow is find an
                action → open its run → read the trajectory, and stacking
                put the answer a full result-table's scroll below the
                question. */}
            <section className="lg:col-span-3 min-w-0 bg-surface border border-default rounded-xl p-4">
                <div className="flex items-start gap-2 mb-1">
                    <h3 className="text-sm font-semibold text-default">Find actions</h3>
                    <HelpTip
                        title="What this tab is"
                        text={
                            'Every governed call this system makes — LLM, MCP tool, skill, A2A — ' +
                            'is recorded as an action inside a run. This tab answers "did it do X, ' +
                            'and what happened around it": search for an action here, then open its ' +
                            'run below to see the full sequence — what led to it and what it caused. ' +
                            'The table starts with the system\u2019s most recent actions.'
                        }
                    />
                </div>
                <p className="text-xs text-muted mb-3">
                    An <strong>action</strong> is one governed call — LLM, MCP tool,
                    skill, or A2A. Find one, then open its run to see the turns and
                    phases around it.
                </p>
                <div className="flex flex-wrap items-end gap-3">
                    <label className="text-xs text-muted">
                        Capability
                        <input
                            value={capability}
                            onChange={(e) => setCapability(e.target.value)}
                            placeholder={observed.caps[0] ?? 'any capability'}
                            list="trajectory-observed-caps"
                            className="mt-1 block w-52 px-2 py-1.5 text-sm border border-default rounded-lg"
                        />
                        <datalist id="trajectory-observed-caps">
                            {observed.caps.map((c) => (
                                <option key={c} value={c} />
                            ))}
                        </datalist>
                    </label>
                    <label className="text-xs text-muted">
                        Type
                        <select
                            value={proxyType}
                            onChange={(e) => setProxyType(e.target.value)}
                            className="mt-1 block w-32 px-2 py-1.5 text-sm border border-default rounded-lg"
                        >
                            <option value="">any</option>
                            <option value="llm">llm</option>
                            <option value="mcp">mcp</option>
                            <option value="a2a">a2a</option>
                            <option value="skill">skill</option>
                        </select>
                    </label>
                    <label className="text-xs text-muted">
                        Step (phase)
                        <input
                            value={stepName}
                            onChange={(e) => setStepName(e.target.value)}
                            placeholder={observed.steps[0] ?? 'any phase'}
                            list="trajectory-observed-steps"
                            className="mt-1 block w-40 px-2 py-1.5 text-sm border border-default rounded-lg"
                        />
                        <datalist id="trajectory-observed-steps">
                            {observed.steps.map((st) => (
                                <option key={st} value={st} />
                            ))}
                        </datalist>
                    </label>
                    <label className="text-xs text-muted">
                        Window
                        <select
                            value={periodHours}
                            onChange={(e) => setPeriodHours(Number(e.target.value))}
                            className="mt-1 block w-28 px-2 py-1.5 text-sm border border-default rounded-lg"
                        >
                            <option value={24}>24h</option>
                            <option value={168}>7d</option>
                            <option value={720}>30d</option>
                        </select>
                    </label>
                    <label className="flex items-center gap-2 text-xs text-secondary pb-1.5">
                        <input
                            type="checkbox"
                            checked={onlyBlocked}
                            onChange={(e) => setOnlyBlocked(e.target.checked)}
                        />
                        Blocked / denied only
                    </label>
                    <button
                        type="button"
                        onClick={() => void search()}
                        disabled={searching}
                        className="px-3 py-1.5 text-sm rounded-lg bg-primary-600 text-white disabled:opacity-50"
                        data-testid="trajectory-search"
                    >
                        {searching ? 'Searching…' : 'Search'}
                    </button>
                </div>

                {searchErr && (
                    <p className="mt-3 text-sm text-red-700">{searchErr}</p>
                )}

                {hits.length > 0 && (
                    <div className="mt-4 overflow-x-auto max-h-[60vh] overflow-y-auto" data-testid="trajectory-find-results">
                        <table className="min-w-full text-sm">
                            <thead className={`${TABLE_THEAD} sticky top-0 z-10`}>
                                <tr className="text-left">
                                    <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>When</th>
                                    <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Type</th>
                                    <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Action</th>
                                    <th className={`px-3 py-2 ${TABLE_TH_TYPE}`}>Step</th>
                                    <th className="px-3 py-2" />
                                </tr>
                            </thead>
                            <tbody className={TABLE_TBODY}>
                                {hits.map((h, i) => (
                                    <tr
                                        key={`${h.correlation_id ?? i}`}
                                        className={`border-t border-default ${
                                            h.root_task_id && h.root_task_id === runId
                                                ? 'bg-primary-50/60'
                                                : ''
                                        }`}
                                    >
                                        <td className="px-3 py-2 text-muted whitespace-nowrap">
                                            {h.timestamp
                                                ? new Date(h.timestamp).toLocaleString()
                                                : '—'}
                                        </td>
                                        <td className="px-3 py-2">
                                            <TypeBadge t={h.proxy_type} />
                                        </td>
                                        <td className="px-3 py-2 text-default">
                                            {h.mcp_capability_name ?? h.llm_model_used ?? '—'}
                                            {(h.guardrail_blocked ||
                                                h.argument_policy_denied) && (
                                                <span className="ml-2 text-xs text-red-700 font-medium">
                                                    blocked
                                                </span>
                                            )}
                                        </td>
                                        <td className="px-3 py-2 text-xs text-muted">
                                            {h.step_name ?? (
                                                <span title="No phase asserted by the client">
                                                    unphased
                                                </span>
                                            )}
                                        </td>
                                        <td className="px-3 py-2 text-right">
                                            <button
                                                type="button"
                                                className="text-xs text-primary-700 hover:underline whitespace-nowrap"
                                                title={h.root_task_id ?? undefined}
                                                data-testid={`open-trajectory-${i}`}
                                                onClick={() => {
                                                    setRunId(h.root_task_id ?? '');
                                                    setStepFilter(h.step_id ?? null);
                                                    void inspect(
                                                        h.root_task_id ?? '',
                                                        h.step_id ?? null,
                                                    );
                                                }}
                                            >
                                                Inspect →
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
                {!searching && hits.length === 0 && !searchErr && (
                    <p className="mt-3 text-sm text-muted">
                        No actions matched. Widen the window or clear a filter.
                    </p>
                )}
            </section>

            {/* ── INSPECT (40%) ────────────────────────────────────── */}
            <section
                className="lg:col-span-2 min-w-0 bg-surface border border-default rounded-xl p-4 lg:sticky lg:top-4 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto"
                data-testid="trajectory-inspect-pane"
            >
                <div className="flex items-start gap-2 mb-1">
                    <h3 className="text-sm font-semibold text-default">Inspect a run's trajectory</h3>
                    <HelpTip
                        title="Inspect a run's trajectory"
                        text={
                            'A run is one end-to-end execution of this system. Paste or pick a ' +
                            'run id to see its trajectory — every action in order, grouped into ' +
                            'turns and phases — plus consequence pairs: what each action led to ' +
                            'within the look-ahead window. Recent run ids are suggested as you type.'
                        }
                    />
                </div>
                <div className="flex flex-wrap items-end gap-3 mb-3">
                    <label className="text-xs text-muted flex-1 min-w-[16rem] min-h-0 min-w-0">
                        Run
                        <input
                            value={runId}
                            onChange={(e) => setRunId(e.target.value)}
                            placeholder={observed.runs[0] ?? 'run id or root task id'}
                            list="trajectory-recent-runs"
                            className="mt-1 block w-full px-2 py-1.5 text-sm border border-default rounded-lg"
                        />
                        <datalist id="trajectory-recent-runs">
                            {observed.runs.map((r) => (
                                <option key={r} value={r} />
                            ))}
                        </datalist>
                    </label>
                    <label className="text-xs text-muted">
                        Look-ahead
                        <select
                            value={within}
                            onChange={(e) => setWithin(Number(e.target.value))}
                            className="mt-1 block w-28 px-2 py-1.5 text-sm border border-default rounded-lg"
                        >
                            <option value={5}>5 actions</option>
                            <option value={10}>10 actions</option>
                            <option value={25}>25 actions</option>
                        </select>
                    </label>
                    <button
                        type="button"
                        onClick={() => void inspect(runId, stepFilter)}
                        disabled={loading || !runId}
                        className="px-3 py-1.5 text-sm rounded-lg bg-primary-600 hover:bg-primary-700 text-white disabled:opacity-50"
                        data-testid="trajectory-inspect"
                    >
                        {loading ? 'Loading…' : 'Inspect'}
                    </button>
                    {stepFilter && (
                        <button
                            type="button"
                            className="text-xs text-primary-700 hover:underline pb-2"
                            onClick={() => {
                                setStepFilter(null);
                                void inspect(runId, null);
                            }}
                        >
                            Clear step filter
                        </button>
                    )}
                </div>

                {loadErr && <p className="text-sm text-red-700">{loadErr}</p>}

                {traj && (
                    <>
                        <div className="flex flex-wrap gap-4 text-sm mb-3">
                            <span>
                                <b>{traj.action_count}</b>{' '}
                                <span className="text-muted">actions</span>
                            </span>
                            <span>
                                <b>{traj.turn_count}</b>{' '}
                                <span className="text-muted">
                                    turns
                                    {traj.turn_attribution !== 'client_asserted' &&
                                        ' (inferred)'}
                                </span>
                            </span>
                            <span>
                                {traj.unphased ? (
                                    <span
                                        className="text-amber-700"
                                        title="The client asserted no phases. This is not the same as the run having one phase."
                                        data-testid="unphased"
                                    >
                                        unphased
                                    </span>
                                ) : (
                                    <>
                                        <b>{traj.step_count}</b>{' '}
                                        <span className="text-muted">steps</span>
                                    </>
                                )}
                            </span>
                        </div>

                        {traj.steps.length > 0 && (
                            <div className="mb-4" data-testid="trajectory-steps">
                                <h4 className="text-xs uppercase tracking-wide text-muted mb-2">
                                    Phases
                                </h4>
                                <div className="flex flex-wrap gap-2">
                                    {traj.steps.map((s) => (
                                        <button
                                            key={s.step_id}
                                            type="button"
                                            onClick={() => {
                                                setStepFilter(s.step_id);
                                                void inspect(runId, s.step_id);
                                            }}
                                            className={`px-3 py-1.5 rounded-lg border text-xs ${
                                                stepFilter === s.step_id
                                                    ? 'border-primary-600 bg-primary-50'
                                                    : 'border-default'
                                            }`}
                                        >
                                            <b>{s.step_name ?? s.step_id}</b>{' '}
                                            <span className="text-muted">
                                                {s.turns} turns · {s.actions} actions ·{' '}
                                                {money(s.cost_usd)}
                                            </span>
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}

                        <h4 className="text-xs uppercase tracking-wide text-muted mb-2">
                            Turns
                        </h4>
                        <ol className="space-y-1 max-h-[60vh] overflow-y-auto pr-1" data-testid="trajectory-turns">
                            {traj.turns.map((t, i) => (
                                <li
                                    key={t.turn_id ?? `inferred-${t.inferred_turn ?? i}`}
                                    className="flex items-center gap-3 text-sm"
                                >
                                    <span className="w-12 text-right text-xs text-muted tabular-nums">
                                        {t.turn_id ? (
                                            t.turn_seq ?? i + 1
                                        ) : (
                                            <span title="Inferred from model-call boundaries — the client asserted no turn id">
                                                ~{t.inferred_turn ?? i + 1}
                                            </span>
                                        )}
                                    </span>
                                    <span className="text-xs text-muted w-28 truncate">
                                        {t.opened_by_model ?? '—'}
                                    </span>
                                    <span className="text-default truncate flex-1 min-h-0 min-w-0">
                                        {t.tools.length ? t.tools.join(' → ') : '—'}
                                    </span>
                                    <span className="text-xs text-muted tabular-nums">
                                        {t.action_count} actions
                                    </span>
                                    <span className="text-xs text-muted tabular-nums w-20 text-right">
                                        {money(t.cost_usd)}
                                    </span>
                                    {t.blocked && (
                                        <span className="text-xs text-red-700 font-medium">
                                            blocked
                                        </span>
                                    )}
                                </li>
                            ))}
                        </ol>

                        {pairs.length > 0 && (
                            <div className="mt-5" data-testid="trajectory-consequences">
                                <h4 className="text-xs uppercase tracking-wide text-muted mb-1">
                                    After a trip
                                </h4>
                                <p className="text-xs text-muted mb-2">
                                    Tool actions that ran within {within} actions of a
                                    guardrail or policy trip. This is co-occurrence in
                                    execution order — <strong>not</strong> a causal claim.
                                </p>
                                <table className="min-w-full text-sm">
                                    <thead className={`${TABLE_THEAD} sticky top-0 z-10`}>
                                        <tr className="text-left">
                                            <th className={`px-3 py-1.5 ${TABLE_TH_TYPE}`}>Trip</th>
                                            <th className={`px-3 py-1.5 ${TABLE_TH_TYPE}`}>Then ran</th>
                                            <th className={`px-3 py-1.5 ${TABLE_TH_TYPE}`}>Actions apart</th>
                                        </tr>
                                    </thead>
                                    <tbody className={TABLE_TBODY}>
                                        {pairs.map((p, i) => (
                                            <tr
                                                key={`${p.trip_action_no}-${p.after_action_no}-${i}`}
                                                className="border-t border-default"
                                            >
                                                <td className="px-3 py-1.5">
                                                    <span className="text-red-700">
                                                        {p.trip_check ??
                                                            (p.trip_policy_denied
                                                                ? 'argument policy'
                                                                : p.trip_pii
                                                                  ? 'PII'
                                                                  : 'guardrail')}
                                                    </span>
                                                    {p.trip_step_name && (
                                                        <span className="text-xs text-muted ml-2">
                                                            in {p.trip_step_name}
                                                        </span>
                                                    )}
                                                </td>
                                                <td className="px-3 py-1.5 text-default">
                                                    {p.after_capability ?? p.after_type}
                                                </td>
                                                <td className="px-3 py-1.5 tabular-nums text-muted">
                                                    {p.actions_apart}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </>
                )}
            </section>
        </div>
    );
}

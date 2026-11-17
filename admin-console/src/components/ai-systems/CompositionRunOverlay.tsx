/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The run overlay's controls around the Simple graph:
 *
 *  - `RunPicker` — recent runs of this system, newest first, searchable and
 *    paged (time, application/subject, outcome, release, action count);
 *    choosing one applies it to the graph, "Clear" returns to the plain
 *    graph. The selection lives in the route (`ai-systems:{id}:composition:
 *    {runId}`), so a run on the graph is deep-linkable.
 *  - `RunSequenceList` — one row per numbered action (number, step, turn,
 *    kind, target, outcome, duration) under step headers. It is the graph's
 *    accessible equivalent (the graph is role="img"): keyboard navigable,
 *    and hovering or focusing a row highlights its badge (and hovering a
 *    badge highlights its row).
 *  - `RunOverlayLegend` — what the badges, the dimming and the ring mean.
 *
 * With a run applied on a wide enough tab (`runOverlayArrangement`), the
 * graph + its connection legend sit left and the sequence list + this
 * legend right; the list then scrolls on its own under a sticky header.
 *
 * Model: `lib/compositionRunOverlay.ts` (placement only — every number,
 * outcome and target comes from the control plane's run projection, the one
 * Run details reads). METADATA ONLY: nothing here renders an argument, a
 * prompt, a response or an error text.
 */

import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Search, X } from 'lucide-react';

import { useGateway } from '../../hooks/useGateway';
import { releaseLabel, runRelease } from '../../lib/agentRelease';
import {
    humanDurationMs,
    TERMINAL_STATE_CLASSES,
    TERMINAL_STATE_LABELS,
    type RunSummary,
} from '../../lib/aiSystemTypes';
import { RUN_OUTCOME_META, RUN_OUTCOME_ORDER, runOutcomeClass } from '../../lib/compositionConnection';
import { runSubject, type OverlayAction, type RunOverlay } from '../../lib/compositionRunOverlay';
import { errorMessage } from '../../lib/narrow';
import { TABLE_CONTAINER, TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import { RunBadge, type OverlayHighlight } from './CompositionSimpleGraph';

// ---------------------------------------------------------------------------
// Picker
// ---------------------------------------------------------------------------

const PAGE = 20;

function RunOptionLine({ run }: { run: RunSummary }) {
    const release = runRelease(run);
    const subject = runSubject(run);
    return (
        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
            <span className="tabular-nums text-default">
                {run.started_at ? new Date(run.started_at).toLocaleString() : '—'}
            </span>
            {subject && <span className="truncate text-secondary">{subject}</span>}
            <span
                className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                    TERMINAL_STATE_CLASSES[run.terminal_state] ?? 'bg-slate-100 text-slate-700'
                }`}
            >
                {TERMINAL_STATE_LABELS[run.terminal_state] ?? run.terminal_state}
            </span>
            {release && <code className="truncate font-mono text-secondary">{releaseLabel(release)}</code>}
            <span className="text-muted">
                {run.action_count} action{run.action_count === 1 ? '' : 's'}
            </span>
        </span>
    );
}

export function RunPicker({
    aiSystemId,
    selectedId,
    selected,
    onSelect,
}: {
    aiSystemId: string;
    /** The applied run's id (from the route), or null. */
    selectedId: string | null;
    /** Its summary once loaded. */
    selected: RunSummary | null;
    onSelect: (runId: string | null) => void;
}) {
    const { fetchAISystemRuns } = useGateway();
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState('');
    const [debounced, setDebounced] = useState('');
    // "Load more" grows the page; a new search starts again at one page.
    const [pages, setPages] = useState(1);
    const [activeIndex, setActiveIndex] = useState(0);
    const rootRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        const t = setTimeout(() => setDebounced(query.trim()), 250);
        return () => clearTimeout(t);
    }, [query]);

    // One request per (system, search, pages) while open. The result is keyed
    // by the request it answers, so "loading" is derived and a slow stale
    // answer never overwrites a newer one.
    const requestKey = open ? `${aiSystemId}|${debounced}|${pages}` : '';
    const [result, setResult] = useState<{
        key: string;
        runs: RunSummary[];
        total: number;
        error: string;
    } | null>(null);
    useEffect(() => {
        if (!requestKey) return;
        let cancelled = false;
        fetchAISystemRuns(aiSystemId, {
            period_hours: 0,
            limit: PAGE * pages,
            offset: 0,
            q: debounced || undefined,
        }).then(
            (res) => {
                if (!cancelled) setResult({ key: requestKey, runs: res.runs, total: res.total, error: '' });
            },
            (e: unknown) => {
                if (!cancelled)
                    setResult({ key: requestKey, runs: [], total: 0, error: errorMessage(e, 'Failed to load runs') });
            }
        );
        return () => {
            cancelled = true;
        };
    }, [requestKey, aiSystemId, debounced, pages, fetchAISystemRuns]);
    const loading = Boolean(requestKey) && result?.key !== requestKey;
    const runs = result?.runs ?? [];
    const total = result?.total ?? 0;
    const error = result?.error ?? '';

    useEffect(() => {
        if (!open) return;
        const onDown = (e: MouseEvent) => {
            if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', onDown);
        return () => document.removeEventListener('mousedown', onDown);
    }, [open]);

    const choose = (run: RunSummary) => {
        setOpen(false);
        onSelect(run.id);
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Escape') {
            setOpen(false);
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActiveIndex((i) => Math.min(i + 1, Math.max(runs.length - 1, 0)));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActiveIndex((i) => Math.max(i - 1, 0));
        } else if (e.key === 'Enter' && runs[activeIndex]) {
            e.preventDefault();
            choose(runs[activeIndex]);
        }
    };

    const listId = `run-picker-list-${aiSystemId}`;
    return (
        <div className="relative flex flex-wrap items-center gap-2" ref={rootRef} data-testid="run-picker">
            <span className="text-xs font-medium text-secondary">Run overlay</span>
            <button
                type="button"
                className="flex min-w-0 max-w-full items-center gap-2 rounded-lg border border-strong bg-surface px-3 py-1.5 text-left text-sm hover:bg-surface-hover"
                aria-haspopup="listbox"
                aria-expanded={open}
                onClick={() => {
                    setActiveIndex(0);
                    setOpen((o) => !o);
                }}
                data-testid="run-picker-toggle"
            >
                {selected ? (
                    <RunOptionLine run={selected} />
                ) : selectedId ? (
                    <span className="font-mono text-xs text-secondary">{selectedId}</span>
                ) : (
                    <span className="text-muted">Select a run to show on the graph…</span>
                )}
                <ChevronDown className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
            </button>
            {selectedId && (
                <button
                    type="button"
                    className="flex items-center gap-1 rounded-lg border border-strong px-2.5 py-1.5 text-sm hover:bg-surface-hover"
                    onClick={() => onSelect(null)}
                    data-testid="run-picker-clear"
                >
                    <X className="h-3.5 w-3.5" aria-hidden="true" /> Clear
                </button>
            )}
            {open && (
                <div
                    className="absolute left-0 top-full z-20 mt-1 w-full max-w-2xl rounded-lg border border-default bg-surface p-2 shadow-lg"
                    data-testid="run-picker-popover"
                >
                    <label className="flex items-center gap-2 rounded-md border border-strong px-2 py-1">
                        <Search className="h-4 w-4 text-muted" aria-hidden="true" />
                        <input
                            autoFocus
                            type="search"
                            value={query}
                            onChange={(e) => {
                                setQuery(e.target.value);
                                setPages(1);
                                setActiveIndex(0);
                            }}
                            onKeyDown={onKeyDown}
                            placeholder="Find a run: id, application, release, client…"
                            className="w-full bg-transparent text-sm outline-none"
                            role="combobox"
                            aria-expanded="true"
                            aria-controls={listId}
                            aria-activedescendant={runs[activeIndex] ? `${listId}-${runs[activeIndex].id}` : undefined}
                            aria-label="Find a run"
                            data-testid="run-picker-search"
                        />
                    </label>
                    {error && (
                        <p className="mt-2 text-xs text-red-700" data-testid="run-picker-error">
                            {error}
                        </p>
                    )}
                    <ul
                        id={listId}
                        role="listbox"
                        aria-label="Recent runs, newest first"
                        className="mt-2 max-h-80 overflow-y-auto"
                        data-testid="run-picker-list"
                    >
                        {runs.map((run, i) => (
                            <li
                                key={run.id}
                                id={`${listId}-${run.id}`}
                                role="option"
                                aria-selected={i === activeIndex}
                                className={`cursor-pointer rounded-md px-2 py-1.5 ${
                                    i === activeIndex ? 'bg-surface-hover' : ''
                                }`}
                                onMouseDown={(e) => e.preventDefault()}
                                onClick={() => choose(run)}
                                onMouseEnter={() => setActiveIndex(i)}
                                data-testid={`run-option-${run.id}`}
                            >
                                <RunOptionLine run={run} />
                                <span className="block truncate font-mono text-[10px] text-muted">{run.id}</span>
                            </li>
                        ))}
                        {!loading && runs.length === 0 && !error && (
                            <li className="px-2 py-3 text-center text-xs text-muted">
                                {debounced ? 'No run matches.' : 'No runs recorded for this system yet.'}
                            </li>
                        )}
                    </ul>
                    <div className="mt-1 flex items-center justify-between text-xs text-muted">
                        <span data-testid="run-picker-count">
                            {loading ? 'Loading…' : `${runs.length} of ${total} run${total === 1 ? '' : 's'}`}
                        </span>
                        {runs.length < total && !loading && (
                            <button
                                type="button"
                                className="rounded border border-strong px-2 py-0.5 hover:bg-surface-hover"
                                onClick={() => setPages((p) => p + 1)}
                                data-testid="run-picker-more"
                            >
                                Load more
                            </button>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Sequence list — the graph's accessible equivalent
// ---------------------------------------------------------------------------

function Duration({ ms }: { ms: number | null }) {
    if (ms == null) return <span className="text-muted">—</span>;
    if (ms < 1000) return <>{Math.round(ms)} ms</>;
    // One decimal below a minute: a call's latency, not a run's span.
    if (ms < 60_000) return <>{(ms / 1000).toFixed(1)} s</>;
    return <>{humanDurationMs(ms)}</>;
}

/** The list's layout: `full` under the graph (six columns, flows with the
 *  page), `column` beside it (its own scroll box under a sticky header,
 *  and four columns — turn and kind fold into the target's second line —
 *  so it reads in ~24 rem). */
export type RunSequenceLayout = 'full' | 'column';

export function RunSequenceList({
    overlay,
    highlight,
    layout = 'full',
}: {
    overlay: RunOverlay;
    highlight: OverlayHighlight;
    layout?: RunSequenceLayout;
}) {
    const column = layout === 'column';
    const tableRef = useRef<HTMLTableElement>(null);
    const move = (from: HTMLElement, delta: number) => {
        const rows = Array.from(tableRef.current?.querySelectorAll<HTMLElement>('tr[data-run-row]') ?? []);
        const next = rows[rows.indexOf(from) + delta];
        next?.focus();
    };
    const row = (a: OverlayAction) => {
        const meta = RUN_OUTCOME_META[a.outcome];
        const active = Boolean(highlight.active?.includes(a.number));
        return (
            <tr
                key={a.number}
                tabIndex={0}
                data-run-row=""
                data-testid={`run-seq-row-${a.number}`}
                data-active={active ? 'true' : 'false'}
                aria-label={`Action ${a.number}: ${a.kindLabel} ${a.target}${a.via ? ` on ${a.via}` : ''}, ${meta.label}`}
                className={`outline-none focus-visible:ring-2 focus-visible:ring-primary-500 ${active ? 'bg-surface-hover' : ''}`}
                onMouseEnter={() => highlight.setActive([a.number])}
                onMouseLeave={() => highlight.setActive(null)}
                onFocus={() => highlight.setActive([a.number])}
                onBlur={() => highlight.setActive(null)}
                onKeyDown={(e) => {
                    if (e.key === 'ArrowDown') {
                        e.preventDefault();
                        move(e.currentTarget, 1);
                    } else if (e.key === 'ArrowUp') {
                        e.preventDefault();
                        move(e.currentTarget, -1);
                    }
                }}
            >
                <td className="whitespace-nowrap px-3 py-1.5" style={{ paddingLeft: (column ? 8 : 12) + a.nesting * (column ? 10 : 14) }}>
                    <RunBadge text={a.number} numbers={[a.number]} outcome={a.outcome} active={active} testId={`run-seq-badge-${a.number}`} />
                </td>
                {!column && <td className="px-3 py-1.5 text-secondary">{a.turn ?? '—'}</td>}
                {!column && <td className="truncate px-3 py-1.5 text-secondary">{a.kindLabel}</td>}
                <td className={column ? 'px-2 py-1.5' : 'px-3 py-1.5'}>
                    <span className="block truncate font-mono text-xs text-default" title={a.target}>
                        {a.target}
                    </span>
                    {(column || a.via || a.delegateOf) && (
                        <span className="block truncate text-[11px] text-muted" data-testid={`run-seq-detail-${a.number}`}>
                            {[
                                column ? a.kindLabel : null,
                                column && a.turn != null ? `turn ${a.turn}` : null,
                                a.via ? `on ${a.via}` : null,
                                a.delegateOf ? `inside ${a.delegateOf}` : null,
                            ]
                                .filter(Boolean)
                                .join(' · ')}
                        </span>
                    )}
                </td>
                <td className={column ? 'px-2 py-1.5' : 'whitespace-nowrap px-3 py-1.5'}>
                    <span className={`${runOutcomeClass(a.outcome)} composition-connection-swatch text-xs font-medium`}>
                        {meta.glyph && <span aria-hidden="true">{meta.glyph} </span>}
                        {meta.label}
                    </span>
                </td>
                <td className={`whitespace-nowrap py-1.5 text-right tabular-nums text-secondary ${column ? 'px-2' : 'px-3'}`}>
                    <Duration ms={a.durationMs} />
                </td>
            </tr>
        );
    };
    const th = `${TABLE_TH_TYPE} py-2 ${column ? 'px-2' : 'px-3'}`;
    const heads: { label: string; width?: string; right?: boolean }[] = column
        ? [
              { label: '#', width: '3.5rem' },
              { label: 'Target' },
              { label: 'Outcome', width: '6.5rem' },
              { label: 'Duration', width: '4.75rem', right: true },
          ]
        : [
              { label: '#', width: '6.5rem' },
              { label: 'Turn', width: '4rem' },
              { label: 'Kind', width: '7rem' },
              { label: 'Target' },
              { label: 'Outcome', width: '10rem' },
              { label: 'Duration', width: '6rem', right: true },
          ];
    const count = overlay.actions.length;
    const body = (
        <>
            <h3
                className={`text-sm font-semibold text-default ${column ? 'sticky top-0 z-10 bg-surface-raised pb-2' : ''}`}
                data-testid="run-sequence-header"
                data-sticky={column ? 'true' : 'false'}
            >
                Sequence — {count} action{count === 1 ? '' : 's'}
            </h3>
            {overlay.shown < overlay.total && (
                <p className="text-xs text-amber-700" data-testid="run-sequence-capped">
                    Showing the first {overlay.shown} of {overlay.total} actions — the run detail is capped.
                </p>
            )}
            <div className={TABLE_CONTAINER}>
                <table ref={tableRef} className="w-full table-fixed text-sm" data-testid="run-sequence-table">
                    <caption className="sr-only">
                        The run’s actions in the gateway’s order, grouped by step; a delegation’s own
                        actions are numbered under the call that entered them.
                    </caption>
                    <colgroup>
                        {heads.map((h) => (
                            <col key={h.label} style={h.width ? { width: h.width } : undefined} />
                        ))}
                    </colgroup>
                    <thead className={TABLE_THEAD}>
                        <tr>
                            {heads.map((h) => (
                                <th key={h.label} scope="col" className={`${th} ${h.right ? 'text-right' : 'text-left'}`}>
                                    {h.label}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    {overlay.groups.map((g, i) => (
                        <tbody key={`${g.stepId ?? 'none'}-${i}`} className="divide-y divide-default" data-testid={`run-step-${i}`}>
                            <tr className="bg-surface-alt">
                                <th scope="rowgroup" colSpan={heads.length} className="px-3 py-1.5 text-left text-xs font-semibold text-default">
                                    Step: {g.title}
                                </th>
                            </tr>
                            {g.actions.map(row)}
                        </tbody>
                    ))}
                </table>
            </div>
        </>
    );
    return column ? (
        // Beside the graph: the section takes the column's height and its
        // body scrolls on its own under the sticky header, so a long run
        // never pushes the graph off screen.
        <section className="app-column app-region" data-testid="run-sequence" data-layout="column">
            <div className="scroll-region space-y-2 pr-1" data-testid="run-sequence-scroll">
                {body}
            </div>
        </section>
    ) : (
        <section className="space-y-2" data-testid="run-sequence" data-layout="full">
            {body}
        </section>
    );
}

// ---------------------------------------------------------------------------
// Legend
// ---------------------------------------------------------------------------

export function RunOverlayLegend({ compact = false }: { compact?: boolean }) {
    return (
        <div className="rounded-lg border border-default bg-surface px-3 py-2" data-testid="run-overlay-legend">
            <p className="mb-1 text-xs font-medium text-secondary">This run on the graph</p>
            {/* In a side column it is one column wide: two would squeeze each meaning to a few words a line. */}
            <ul className={`grid gap-x-6 gap-y-1 text-xs text-secondary ${compact ? '' : 'sm:grid-cols-2'}`}>
                {RUN_OUTCOME_ORDER.map((o) => (
                    <li key={o} className="flex items-start gap-2" data-testid={`run-overlay-legend-${o}`}>
                        <RunBadge text="1" numbers={['1']} outcome={o} active={false} testId={`run-legend-badge-${o}`} />
                        <span>
                            <span className="font-semibold text-default">{RUN_OUTCOME_META[o].label}</span> —{' '}
                            {RUN_OUTCOME_META[o].meaning}
                        </span>
                    </li>
                ))}
                <li className="flex items-start gap-2">
                    <span className="font-mono font-semibold text-default">5a</span>
                    <span>A step the delegated system took inside call 5, listed under the agent it called.</span>
                </li>
                <li className="flex items-start gap-2">
                    <span className="font-mono font-semibold text-default">7–12</span>
                    <span>Consecutive actions on one line, collapsed when crowded; the list shows each.</span>
                </li>
                <li className="flex items-start gap-2">
                    <span className="inline-block h-3 w-6 shrink-0 rounded bg-surface-alt opacity-40" aria-hidden="true" />
                    <span>Faded — declared, but this run did not use it.</span>
                </li>
                <li className="flex items-start gap-2">
                    <span className="composition-run-undeclared inline-block h-3 w-6 shrink-0 rounded" aria-hidden="true" />
                    <span>Ringed — this run used it, the declared composition does not list it.</span>
                </li>
            </ul>
        </div>
    );
}

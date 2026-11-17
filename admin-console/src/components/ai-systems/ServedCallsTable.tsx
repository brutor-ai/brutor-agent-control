/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Served calls — the Runs tab's view of a shared service.
 *
 * A system called over A2A or as a delegate roots no runs: its work is its
 * segment of each caller's run. Without this list the Runs tab of such a
 * system would say "No runs recorded yet" while it answers calls every
 * day. Each row is the SERVICE's slice of one run — its outcome, its calls,
 * its tokens and cost — never the caller run's totals; clicking a row opens
 * the whole run it served, so the call can be read in context.
 */

import { useState } from 'react';

import type { RunDetail, ServedCall, ServedCallListResponse } from '../../lib/aiSystemTypes';
import { TABLE_TBODY, TABLE_TH_TYPE, TABLE_THEAD } from '../ui/tableStyles';
import { Pagination } from '../ui/Pagination';
import { HelpTip } from '../ui/HelpTip';
import RunDetailsDialog from './RunDetailsDialog';
import { errorMessage } from '../../lib/narrow';

const OUTCOME_STYLE: Record<string, { label: string; cls: string }> = {
    answered: { label: 'Answered', cls: 'text-green-700 dark:text-green-400' },
    rejected: { label: 'Declined', cls: 'text-green-700 dark:text-green-400' },
    failed: { label: 'Failed', cls: 'text-red-600 dark:text-red-400' },
    timed_out: { label: 'Timed out', cls: 'text-red-600 dark:text-red-400' },
    blocked: { label: 'Refused by gateway', cls: 'text-amber-700 dark:text-amber-300' },
};

const HELP =
    'Calls this system served inside other systems’ runs — as an A2A agent ' +
    'or a delegate it starts no runs of its own. Each row is its slice of ' +
    'one run: its outcome, the calls it made, its tokens and cost. Click a ' +
    'row to open the whole run it served. “Declined” is an answer (the ' +
    'service chose not to do the task); “Refused by gateway” never reached ' +
    'the service.';

function fmtWhen(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : '—';
}

function fmtMs(ms: number | null): string {
    if (ms == null) return '—';
    return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

function fmtUsd(v: number): string {
    return v === 0 ? '$0' : v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

function fmtTokens(n: number): string {
    return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n);
}

interface ServedCallsTableProps {
    data: ServedCallListResponse;
    offset: number;
    pageSize: number;
    onPage: (offset: number) => void;
    fetchRun?: (id: string) => Promise<RunDetail>;
}

export default function ServedCallsTable({ data, offset, pageSize, onPage, fetchRun }: ServedCallsTableProps) {
    const [openRun, setOpenRun] = useState<RunDetail | null>(null);
    const [openError, setOpenError] = useState('');

    const open = async (call: ServedCall) => {
        if (!fetchRun) return;
        setOpenError('');
        try {
            setOpenRun(await fetchRun(call.run_id));
        } catch (err: unknown) {
            setOpenError(errorMessage(err, 'Failed to load the run'));
        }
    };

    return (
        <section className="mt-6" data-testid="served-calls">
            {openRun && (
                <RunDetailsDialog run={openRun} fetchRun={fetchRun} onClose={() => setOpenRun(null)} />
            )}
            <div className="flex items-center gap-2 mb-2">
                <h3 className="text-sm font-semibold text-default">Served calls</h3>
                <span className="text-xs text-muted">inside other systems’ runs · {data.total}</span>
                <HelpTip title="Served calls" text={HELP} iconClassName="w-3.5 h-3.5" testId="served-calls-help" />
            </div>
            {openError && (
                <p className="text-sm text-red-600 dark:text-red-400 mb-2" data-testid="served-calls-error">
                    {openError}
                </p>
            )}
            <div className="overflow-x-auto overflow-y-auto max-h-[60vh] border border-default rounded-lg">
                <table className="min-w-full divide-y divide-default text-sm">
                    <thead className={`${TABLE_THEAD} sticky top-0 z-10`}>
                        <tr>
                            {['When', 'Called by', 'Outcome', 'Calls', 'Duration', 'Tokens', 'Cost', 'Models', 'Run'].map((h) => (
                                <th key={h} className={`${TABLE_TH_TYPE} px-3 py-2 text-left whitespace-nowrap`}>
                                    {h}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody className={TABLE_TBODY}>
                        {data.calls.map((c) => {
                            const style = OUTCOME_STYLE[c.outcome] ?? { label: c.outcome, cls: 'text-secondary' };
                            const deviated =
                                c.outcome === 'answered' &&
                                (c.outcome_conformance === 'undeclared' || c.outcome_conformance === 'unreported');
                            return (
                                <tr
                                    key={c.segment_id}
                                    className={fetchRun ? 'hover:bg-surface-hover cursor-pointer' : undefined}
                                    onClick={() => void open(c)}
                                    data-testid={`served-call-${c.segment_id}`}
                                >
                                    <td className="px-3 py-2 whitespace-nowrap text-secondary">{fmtWhen(c.first_action_at)}</td>
                                    <td className="px-3 py-2 whitespace-nowrap">
                                        {c.caller_system_name ?? c.caller_system_id ?? '—'}
                                        {c.root_system_id && c.root_system_id !== c.caller_system_id && (
                                            <span className="block text-[11px] text-muted">
                                                run of {c.root_system_name ?? c.root_system_id}
                                            </span>
                                        )}
                                    </td>
                                    <td className="px-3 py-2 whitespace-nowrap">
                                        <span className={`font-medium ${style.cls}`}>{style.label}</span>
                                        {c.outcome_id && (
                                            <span className="block font-mono text-[11px] text-muted">{c.outcome_id}</span>
                                        )}
                                        {deviated && (
                                            <span className="block text-[11px] text-amber-700 dark:text-amber-300">
                                                outcome not in its contract
                                            </span>
                                        )}
                                    </td>
                                    <td className="px-3 py-2 whitespace-nowrap tabular-nums text-secondary">
                                        {c.llm_call_count} LLM · {c.tool_call_count} tool
                                    </td>
                                    <td className="px-3 py-2 whitespace-nowrap tabular-nums text-secondary">{fmtMs(c.duration_ms)}</td>
                                    <td className="px-3 py-2 whitespace-nowrap tabular-nums text-secondary">
                                        {fmtTokens(c.total_tokens)}
                                        <span className="block text-[11px] text-muted">
                                            in {fmtTokens(c.input_tokens)} · out {fmtTokens(c.output_tokens)}
                                        </span>
                                    </td>
                                    <td className="px-3 py-2 whitespace-nowrap tabular-nums text-secondary">{fmtUsd(c.cost_usd)}</td>
                                    <td className="px-3 py-2 whitespace-nowrap font-mono text-xs text-secondary">
                                        {c.models.length ? c.models.join(', ') : '—'}
                                    </td>
                                    <td className="px-3 py-2 whitespace-nowrap font-mono text-xs text-muted" title={c.run_id}>
                                        {c.run_id.slice(0, 14)}…
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            {data.total > pageSize && (
                <Pagination
                    offset={offset}
                    pageSize={pageSize}
                    total={data.total}
                    onChange={onPage}
                    testIdPrefix="served-calls-page"
                    noun="served calls"
                    className="mt-2"
                />
            )}
        </section>
    );
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The declared-outcome sections of the Signals sub-tabs.
 *
 *   Outcomes   — the run classes and the mix by outcome and variant, with
 *                conformance. No-work is a neutral grey, never green;
 *                undeclared and unreported runs are listed, never hidden.
 *   Cost       — cost per processed outcome by outcome/variant, and the
 *                no-work overhead as its own card (never averaged in).
 *   Run length — the same, for actions per run.
 *   Errors     — runs by error cause, beside calls by call type.
 *
 * Every table names the population it describes: "processed" means `success`
 * runs — completed, in a declared outcome that counts toward the baseline.
 */

import type React from 'react';

import type { ActionCountSignal, CostPerRunSignal, ErrorCauseSignal, OutcomeSignal, TerminalStateSignal } from '../../lib/aiSystemTypes';
import { TERMINAL_STATE_CLASSES, TERMINAL_STATE_LABELS } from '../../lib/aiSystemTypes';
import {
    ERROR_CAUSE_HELP,
    RUN_CLASS_CLASSES,
    RUN_CLASS_HELP,
    RUN_CLASS_LABELS,
    errorCauseLabel,
    isErrorCause,
    outcomeLabel,
    outcomeMixGroups,
    parseOutcomeId,
    runClassBreakdown,
    NO_WORK_HELP,
    SUCCESS_RATE_HELP,
    type OutcomeDecl,
} from '../../lib/runOutcomes';
import { money, num, pct } from '../../lib/signalFormat';
import { HelpTip } from '../ui/HelpTip';
import { TABLE_TBODY, TABLE_THEAD } from '../ui/tableStyles';
import { BaselineMarker, ConformanceChip, RunClassChip } from './RunOutcomeBadges';
import { SignalHeading, Th, Tile } from './SignalParts';

const OUTCOMES_HINT =
    'How runs ended, on three separate questions: did the execution complete without errors; was the outcome the agent reported one the active contract declares; ' +
    'and does that outcome count toward the baseline. The run class combines them. No-work runs (nothing to do) are grey — neither success nor failure.';

/** One outcome id cell: the outcome / variant, labelled from the catalogue
 *  when it declares them, the raw id beneath. */
function OutcomeIdCell({ id, catalogue }: { id: string | null | undefined; catalogue?: OutcomeDecl[] | null }) {
    if (!id) return <span className="italic text-muted">No outcome reported</span>;
    const parsed = parseOutcomeId(id);
    const label = outcomeLabel(id, catalogue);
    return (
        <span className="block min-w-0">
            {parsed?.variant ? (
                <>
                    <span className="text-muted">{parsed.outcome}.</span>
                    <span className="font-medium text-default">{parsed.variant}</span>
                </>
            ) : (
                <span className="font-medium text-default">{id}</span>
            )}
            {label !== id && <span className="block text-[11px] text-muted truncate">{label}</span>}
        </span>
    );
}

export function OutcomesSignalSection({
    outcomes,
    terminalStates,
    catalogue,
    chart,
}: {
    outcomes?: OutcomeSignal;
    terminalStates: TerminalStateSignal;
    catalogue?: OutcomeDecl[] | null;
    chart?: React.ReactNode;
}) {
    const byClass = outcomes?.by_class ?? {};
    const breakdown = runClassBreakdown(byClass);
    const groups = outcomeMixGroups(outcomes?.by_outcome);
    const total = outcomes?.total_runs ?? terminalStates.total_runs;
    const deviations = byClass.deviation ?? 0;

    return (
        <section data-testid="signal-outcomes">
            <SignalHeading title="Outcomes" hint={OUTCOMES_HINT} />
            <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-3">
                <Tile
                    label="Success rate"
                    value={pct(outcomes?.success_rate ?? terminalStates.success_rate)}
                    help={SUCCESS_RATE_HELP}
                    testId="outcomes-success-rate"
                />
                <Tile label="Processed" value={num(byClass.success ?? 0)} hint="success runs" help={RUN_CLASS_HELP.success} testId="outcomes-processed" />
                <Tile label="No work" value={num(byClass.no_work ?? 0)} tone="neutral" hint="not in the rate" help={RUN_CLASS_HELP.no_work} testId="outcomes-no-work" />
                <Tile
                    label="Deviations"
                    value={num(deviations)}
                    tone={deviations > 0 ? 'warn' : undefined}
                    hint="undeclared or no outcome"
                    help={RUN_CLASS_HELP.deviation}
                    testId="outcomes-deviations"
                />
                <Tile
                    label="Failed"
                    value={num(byClass.failed ?? 0)}
                    tone={(byClass.failed ?? 0) > 0 ? 'warn' : undefined}
                    hint="errored or exhausted"
                    help={RUN_CLASS_HELP.failed}
                    testId="outcomes-failed"
                />
            </div>

            <div className="flex flex-wrap gap-2 mb-3" data-testid="outcomes-classes">
                {breakdown.map(({ cls, runs, share }) => (
                    <span
                        key={cls}
                        className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm ${RUN_CLASS_CLASSES[cls]}`}
                        title={RUN_CLASS_HELP[cls]}
                        data-testid={`outcomes-class-${cls}`}
                    >
                        <span className="font-medium">{RUN_CLASS_LABELS[cls]}</span>
                        <span>
                            {runs} ({pct(share)})
                        </span>
                    </span>
                ))}
                {total === 0 && <span className="text-sm text-muted">No runs in this window.</span>}
            </div>

            {groups.length > 0 && (
                <div className="bg-surface border border-default rounded-lg overflow-x-auto mb-3">
                    <table className="min-w-full divide-y divide-default text-sm" data-testid="outcomes-mix">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <Th>Outcome · variant</Th>
                                <Th>Conformance</Th>
                                <Th>Run class</Th>
                                <Th>Runs</Th>
                                <Th>Share of runs</Th>
                                <Th>Cost</Th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {groups.map((g) => (
                                <OutcomeGroupRows key={g.outcome ?? '(none)'} group={g} catalogue={catalogue} />
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
            {outcomes === undefined && total > 0 && (
                <p className="text-xs text-muted mb-3">The outcome breakdown is not available for this range.</p>
            )}

            {/* Question 1 on its own: how the executions ended, whatever
                outcome they reported. */}
            {terminalStates.by_state.length > 0 && (
                <div className="mb-1" data-testid="signal-terminal-states">
                    <p className="flex items-center gap-1 text-xs text-muted mb-1">
                        Execution — how runs ended, whatever their outcome
                        <HelpTip
                            title="Execution"
                            text="The gateway's own account of each run: completed, errored, exhausted, abandoned or cancelled. A policy block is an errored run (cause: blocked by policy). A completed execution is a success only when it also ended in a declared outcome that counts toward the baseline."
                        />
                    </p>
                    <div className="flex flex-wrap gap-2">
                        {terminalStates.by_state.map((row) => (
                            <span
                                key={row.state}
                                className={`inline-flex items-center gap-2 px-2.5 py-1 rounded-lg text-xs ${
                                    TERMINAL_STATE_CLASSES[row.state] ?? 'bg-slate-100 text-slate-700'
                                }`}
                            >
                                <span className="font-medium">{TERMINAL_STATE_LABELS[row.state] ?? row.state}</span>
                                <span>
                                    {row.count} ({pct(row.share)})
                                </span>
                            </span>
                        ))}
                    </div>
                </div>
            )}
            {chart}
        </section>
    );
}

function OutcomeGroupRows({
    group,
    catalogue,
}: {
    group: ReturnType<typeof outcomeMixGroups>[number];
    catalogue?: OutcomeDecl[] | null;
}) {
    const single = group.rows.length === 1;
    return (
        <>
            {!single && (
                <tr className="bg-surface-subtle" data-testid={`outcomes-group-${group.outcome ?? 'none'}`}>
                    <td className="px-4 py-1.5 text-xs font-semibold text-default" colSpan={3}>
                        {group.outcome ?? <span className="italic text-muted">No outcome reported</span>}
                        {group.outcome && catalogue?.find((o) => o.id === group.outcome) && (
                            <span className="ml-2 font-normal text-muted">{catalogue.find((o) => o.id === group.outcome)?.label}</span>
                        )}
                    </td>
                    <td className="px-4 py-1.5 text-xs font-semibold text-default">{group.runs}</td>
                    <td className="px-4 py-1.5 text-xs text-secondary">{pct(group.share)}</td>
                    <td className="px-4 py-1.5 text-xs text-secondary">{money(group.cost_usd)}</td>
                </tr>
            )}
            {group.rows.map((row) => (
                <tr
                    key={`${row.outcome_id ?? ''}|${row.run_class}|${row.conformance ?? ''}`}
                    data-testid={`outcomes-row-${row.outcome_id ?? 'none'}-${row.run_class}`}
                >
                    <td className={`px-4 py-2 ${single ? '' : 'pl-8'}`}>
                        <OutcomeIdCell id={row.outcome_id} catalogue={catalogue} />
                    </td>
                    <td className="px-4 py-2">
                        <span className="flex flex-wrap items-center gap-1">
                            <ConformanceChip conformance={row.conformance} />
                            {row.conformance === 'declared' && <BaselineMarker counts={row.counts_toward_baseline} />}
                        </span>
                    </td>
                    <td className="px-4 py-2">
                        <RunClassChip cls={row.run_class} />
                    </td>
                    <td className="px-4 py-2 text-secondary">{row.runs}</td>
                    <td className="px-4 py-2 text-secondary">{pct(row.share)}</td>
                    <td className="px-4 py-2 text-secondary">{money(row.cost_usd)}</td>
                </tr>
            ))}
        </>
    );
}

/** Runs by error cause — why executions failed, beside the call table. */
export function ErrorCausesSection({ signal }: { signal?: ErrorCauseSignal }) {
    if (!signal) return null;
    const rows = [...(signal.by_cause ?? [])].sort((a, b) => (b.runs ?? 0) - (a.runs ?? 0) || a.cause.localeCompare(b.cause));
    return (
        <div className="mb-3" data-testid="errors-by-cause">
            <p className="flex items-center gap-1 text-xs text-muted mb-1">
                Failed runs by error cause — {signal.failed_runs} failed run{signal.failed_runs === 1 ? '' : 's'}
                <HelpTip
                    title="Error cause"
                    text="Why a run that errored or ran out of iterations failed, from its own calls: the last unsuccessful call before the run closed decides the cause. A policy block is a failed run with cause 'blocked by policy' — it counts against the success rate like any other failure, but points at governance configuration rather than an outage."
                />
            </p>
            {rows.length === 0 ? (
                <p className="text-sm text-muted" data-testid="errors-by-cause-empty">
                    No failed runs in this range.
                </p>
            ) : (
                <div className="bg-surface border border-default rounded-lg overflow-hidden">
                    <table className="min-w-full divide-y divide-default text-sm">
                        <thead className={TABLE_THEAD}>
                            <tr>
                                <Th>Error cause</Th>
                                <Th>Runs</Th>
                                <Th>Share of failed runs</Th>
                            </tr>
                        </thead>
                        <tbody className={TABLE_TBODY}>
                            {rows.map((row) => (
                                <tr key={row.cause} data-testid={`errors-cause-${row.cause}`}>
                                    <td className="px-4 py-2 text-default">
                                        <span className="inline-flex items-center gap-1">
                                            {errorCauseLabel(row.cause)}
                                            {isErrorCause(row.cause) && (
                                                <HelpTip title={errorCauseLabel(row.cause)} text={ERROR_CAUSE_HELP[row.cause]} />
                                            )}
                                        </span>
                                    </td>
                                    <td className="px-4 py-2 text-red-700 dark:text-red-300 font-medium">{row.runs}</td>
                                    <td className="px-4 py-2 text-secondary">{pct(row.share)}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}

/** Cost per processed outcome, per outcome / variant — never one blended
 *  average across different kinds of work. */
export function CostByOutcomeTable({ cost, catalogue }: { cost: CostPerRunSignal; catalogue?: OutcomeDecl[] | null }) {
    const rows = [...(cost.by_outcome ?? [])].sort((a, b) => (b.runs ?? 0) - (a.runs ?? 0) || a.outcome_id.localeCompare(b.outcome_id));
    if (rows.length === 0) return null;
    return (
        <div className="bg-surface border border-default rounded-lg overflow-x-auto mt-3">
            <table className="min-w-full divide-y divide-default text-sm" data-testid="cost-by-outcome">
                <thead className={TABLE_THEAD}>
                    <tr>
                        <Th>Processed outcome</Th>
                        <Th>Runs</Th>
                        <Th>Cost / run</Th>
                        <Th>Median</Th>
                        <Th>p95</Th>
                        <Th>Total</Th>
                    </tr>
                </thead>
                <tbody className={TABLE_TBODY}>
                    {rows.map((row) => (
                        <tr key={row.outcome_id} data-testid={`cost-outcome-${row.outcome_id}`}>
                            <td className="px-4 py-2">
                                <OutcomeIdCell id={row.outcome_id} catalogue={catalogue} />
                            </td>
                            <td className="px-4 py-2 text-secondary">{row.runs}</td>
                            <td className="px-4 py-2 text-default font-medium">{money(row.cost_per_run)}</td>
                            <td className="px-4 py-2 text-secondary">{money(row.median_cost_usd)}</td>
                            <td className="px-4 py-2 text-secondary">{money(row.p95_cost_usd)}</td>
                            <td className="px-4 py-2 text-secondary">{money(row.cost_usd)}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

/** Run length per processed outcome / variant. */
export function ActionsByOutcomeTable({ actions, catalogue }: { actions: ActionCountSignal; catalogue?: OutcomeDecl[] | null }) {
    const rows = [...(actions.by_outcome ?? [])].sort((a, b) => (b.runs ?? 0) - (a.runs ?? 0) || a.outcome_id.localeCompare(b.outcome_id));
    if (rows.length === 0) return null;
    return (
        <div className="bg-surface border border-default rounded-lg overflow-x-auto mb-3">
            <table className="min-w-full divide-y divide-default text-sm" data-testid="actions-by-outcome">
                <thead className={TABLE_THEAD}>
                    <tr>
                        <Th>Processed outcome</Th>
                        <Th>Runs</Th>
                        <Th>Median actions</Th>
                        <Th>p95</Th>
                        <Th>Max</Th>
                    </tr>
                </thead>
                <tbody className={TABLE_TBODY}>
                    {rows.map((row) => (
                        <tr key={row.outcome_id} data-testid={`actions-outcome-${row.outcome_id}`}>
                            <td className="px-4 py-2">
                                <OutcomeIdCell id={row.outcome_id} catalogue={catalogue} />
                            </td>
                            <td className="px-4 py-2 text-secondary">{row.runs}</td>
                            <td className="px-4 py-2 text-default font-medium">{num(row.median_actions)}</td>
                            <td className="px-4 py-2 text-secondary">{num(row.p95_actions)}</td>
                            <td className="px-4 py-2 text-secondary">{num(row.max_actions)}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

/** The no-work overhead — its own card, never averaged into the work. */
export function NoWorkOverheadTile({ cost }: { cost: CostPerRunSignal }) {
    return (
        <Tile
            label="No-work overhead"
            value={money(cost.no_work_cost_usd)}
            tone="neutral"
            hint={`${cost.no_work_runs} no-work run${cost.no_work_runs === 1 ? '' : 's'}`}
            help={NO_WORK_HELP}
            testId="cost-no-work"
        />
    );
}

/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control × AI System lifecycle — the assurance surfaces.
 *
 * Mission Control's traffic view is requests, tokens, dollars, blocks.
 * The assurance loop (health, liveness, drift, contracts, the run
 * ledger) has its home under Governance → AI Systems; these components
 * put the lifecycle at the operator's landing surface as well:
 *
 *   * `AssuranceBand`  — pinned under the header whenever one AI System
 *     is selected: identity (kind, stage, owner) plus the StatusStrip
 *     verdicts (health / liveness / drift / coverage).
 *   * `RunSignalsSection` — the run ledger's FinOps numbers (cost per
 *     PROCESSED outcome, spend that produced nothing, no-work overhead,
 *     the run-class mix) for the Cost tab.
 *

 * Honesty rules inherited from the assurance engine and enforced here:
 * a system with no computed health renders `unknown` (amber), never
 * healthy; a no-work run (an empty poll) is grey, never green, and never
 * averaged into the cost of the work; and every run figure carries its
 * coverage qualifier.
 */

import {
    ArrowUpRight,
    Bot,
    CircleDollarSign,
    Loader2,
} from 'lucide-react';

import { StatusStrip } from '../ai-systems/StatusStrip';
import {
    AISystemSummary,
    RunStatsResponse,
    } from '../../lib/aiSystemTypes';
import {
    RUN_CLASS_CLASSES,
    RUN_CLASS_COLORS,
    RUN_CLASS_HELP,
    RUN_CLASS_LABELS,
    runClassBreakdown,
} from '../../lib/runOutcomes';
import {
    LIFECYCLE_STAGE_LABELS,
    LifecycleStage,
    SYSTEM_KIND_LABELS,
    } from '../../lib/groupTypes';

// ─────────────────────────────────────────────────────────────────────────
// Shared bits
// ─────────────────────────────────────────────────────────────────────────

const fmtInt = (n: number | null | undefined) =>
    n === null || n === undefined ? '—' : n.toLocaleString();

/** Per-run money: enough decimals that $0.0106 doesn't collapse to $0.01. */
const fmtRunUsd = (v: number | null | undefined) =>
    v === null || v === undefined ? '—' : `$${v.toFixed(v >= 1 ? 2 : 4)}`;

const fmtUsd = (v: number | null | undefined) =>
    v === null || v === undefined ? '—' : `$${v.toFixed(2)}`;

const fmtPct = (v: number | null | undefined) =>
    v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`;

const STAGE_CHIP_CLASSES: Record<string, string> = {
    proposed: 'bg-slate-100 text-slate-700 border-slate-200',
    approved: 'bg-sky-100 text-sky-800 border-sky-200',
    active: 'bg-emerald-100 text-emerald-800 border-emerald-200',
    deprecated: 'bg-amber-100 text-amber-900 border-amber-200',
    retired: 'bg-slate-100 text-slate-500 border-slate-200',
};

function StageChip({ stage }: { stage?: string | null }) {
    if (!stage) {
        return (
            <span className="inline-flex items-center px-2 py-0.5 rounded-full border text-xs bg-amber-50 text-amber-800 border-amber-200">
                no stage
            </span>
        );
    }
    return (
        <span
            className={`inline-flex items-center px-2 py-0.5 rounded-full border text-xs font-medium ${
                STAGE_CHIP_CLASSES[stage] || STAGE_CHIP_CLASSES.proposed
            }`}
        >
            {LIFECYCLE_STAGE_LABELS[stage as LifecycleStage] ?? stage}
        </span>
    );
}

// ─────────────────────────────────────────────────────────────────────────
// AssuranceBand — pinned identity + verdicts for the selected AI System
// ─────────────────────────────────────────────────────────────────────────

interface AssuranceBandProps {
    system?: AISystemSummary | null;
    systemId: string;
    /** Run stats the parent already loaded for the current period — shared
     *  with the StatusStrip so the coverage chip and the run cards can
     *  never disagree about the same window. */
    stats?: RunStatsResponse | null;
    onOpenAiSystems?: () => void;
    reloadKey?: number;
}

export function AssuranceBand({
    system,
    systemId,
    stats,
    onOpenAiSystems,
    reloadKey = 0,
}: AssuranceBandProps) {
    const name = system?.display_name || system?.name || systemId;
    const kind = system?.system_kind
        ? (SYSTEM_KIND_LABELS[system.system_kind] ?? system.system_kind)
        : null;

    return (
        <div
            className="mb-4 rounded-lg border border-default bg-surface-raised p-3"
            data-testid="mc-assurance-band"
        >
            <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                <div className="flex items-center gap-2 min-w-0">
                    <Bot className="w-4 h-4 text-primary flex-shrink-0" />
                    <span className="font-semibold text-default truncate">{name}</span>
                    {kind && (
                        <span className="text-xs text-muted border border-default rounded px-1.5 py-0.5">
                            {kind}
                        </span>
                    )}
                    <StageChip stage={system?.lifecycle_stage ?? null} />
                    {system?.owner ? (
                        <span className="text-xs text-secondary">owner: {system.owner}</span>
                    ) : (
                        <span
                            className="text-xs text-amber-700"
                            title="No accountable owner — a governance gap the lifecycle gate will refuse on"
                        >
                            no owner
                        </span>
                    )}
                </div>
                {onOpenAiSystems && (
                    <button
                        onClick={onOpenAiSystems}
                        className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                        title="Runs, drift, contracts, replay and the assurance report"
                    >
                        Open in AI Systems <ArrowUpRight className="w-3 h-3" />
                    </button>
                )}
            </div>
            <StatusStrip aiSystemId={systemId} stats={stats ?? undefined} reloadKey={reloadKey} />
        </div>
    );
}

// ─────────────────────────────────────────────────────────────────────────
// RunSignalsSection — the run ledger's FinOps numbers
// ─────────────────────────────────────────────────────────────────────────

interface RunSignalsSectionProps {
    stats: RunStatsResponse | null;
    loading: boolean;
    /** 'cost' leads with money; 'overview' leads with outcomes. */
    variant: 'cost' | 'overview';
}

/** The run classes as one bar — success green, no work grey (never
 *  green), deviations amber, failures red. */
function OutcomeBar({ stats }: { stats: RunStatsResponse }) {
    const rows = runClassBreakdown(stats.outcomes?.by_class).filter((r) => r.runs > 0);
    if (!rows.length) return null;
    return (
        <div data-testid="mc-run-class-bar">
            <div className="flex h-2 rounded-full overflow-hidden border border-default">
                {rows.map((row) => (
                    <div
                        key={row.cls}
                        style={{ width: `${Math.max(0.75, row.share * 100)}%`, backgroundColor: RUN_CLASS_COLORS[row.cls] }}
                        title={`${RUN_CLASS_LABELS[row.cls]}: ${fmtInt(row.runs)} (${fmtPct(row.share)})`}
                    />
                ))}
            </div>
            <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1.5 text-xs">
                {rows.map((row) => (
                    <span key={row.cls} className="inline-flex items-center gap-1" title={RUN_CLASS_HELP[row.cls]}>
                        <span className={`px-1.5 py-0.5 rounded ${RUN_CLASS_CLASSES[row.cls]}`}>
                            {RUN_CLASS_LABELS[row.cls]}
                        </span>
                        <span className="text-muted">{fmtInt(row.runs)}</span>
                    </span>
                ))}
            </div>
        </div>
    );
}

export function RunSignalsSection({ stats, loading, variant }: RunSignalsSectionProps) {
    if (loading && !stats) {
        return (
            <div className="mb-6 rounded-lg border border-default bg-surface-raised p-4 flex items-center gap-2 text-secondary text-sm">
                <Loader2 className="w-4 h-4 animate-spin" /> Loading run ledger…
            </div>
        );
    }
    if (!stats) return null;

    const cost = stats.cost;
    const noRuns = stats.terminal_states.total_runs === 0;

    return (
        <div className="mb-6" data-testid={`mc-run-signals-${variant}`}>
            <h3 className="text-sm font-semibold text-default flex items-center gap-2 mb-2">
                <CircleDollarSign className="w-4 h-4 text-primary" />
                {variant === 'cost' ? 'Cost per processed outcome' : 'Task outcomes (run ledger)'}
                <span
                    className="text-xs font-normal text-muted"
                    title="A run is one end-to-end task — every step the system made to finish one job"
                >
                    {fmtInt(stats.terminal_states.total_runs)} runs in period
                </span>
            </h3>

            {noRuns ? (
                <div className="rounded-lg border border-default bg-surface-raised p-4 text-sm text-secondary">
                    No runs recorded in this period. If the system is receiving traffic, its
                    client may not be sending <span className="font-mono">x-brutor-run-id</span> —
                    without it, requests never assemble into tasks.
                </div>
            ) : variant === 'cost' ? (
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                    <div className="rounded-lg border border-default bg-surface-raised p-3">
                        <div className="text-xs text-muted mb-1">Avg cost / processed outcome</div>
                        <div className="text-2xl font-bold text-default">
                            {fmtRunUsd(cost.cost_per_processed_outcome)}
                        </div>
                        <div className="text-xs text-secondary mt-0.5" title="Runs that completed in a declared outcome that counts toward the baseline — the work, never an empty poll">
                            {fmtInt(cost.processed_runs)} processed outcomes
                        </div>
                    </div>
                    <div className="rounded-lg border border-default bg-surface-raised p-3">
                        <div className="text-xs text-muted mb-1">Median · P95</div>
                        <div className="text-2xl font-bold text-default">
                            {fmtRunUsd(cost.median_cost_per_processed_outcome)}
                            <span className="text-sm font-medium text-secondary">
                                {' '}· {fmtRunUsd(cost.p95_cost_per_processed_outcome)}
                            </span>
                        </div>
                        <div className="text-xs text-secondary mt-0.5">spread across processed outcomes</div>
                    </div>
                    <div className="rounded-lg border border-default bg-surface-raised p-3">
                        <div className="text-xs text-muted mb-1">Spent, nothing produced</div>
                        <div className="text-2xl font-bold text-default">
                            {fmtUsd(cost.wasted_cost_usd)}
                        </div>
                        <div className="text-xs text-secondary mt-0.5" title="Spend on failed runs (errored or exhausted, policy blocks included) and deviating runs (completed with an undeclared or no outcome).">
                            on failed and deviating runs
                        </div>
                    </div>
                    <div className="rounded-lg border border-default bg-surface-raised p-3" data-testid="mc-no-work">
                        <div className="text-xs text-muted mb-1">No-work overhead</div>
                        <div className="text-2xl font-bold text-slate-600 dark:text-slate-300">
                            {fmtUsd(cost.no_work_cost_usd)}
                        </div>
                        <div className="text-xs text-secondary mt-0.5" title="Runs that ended in a declared outcome that does not count toward the baseline (e.g. nothing to do) — reported apart, never averaged into the work">
                            {fmtInt(cost.no_work_runs)} no-work runs
                        </div>
                    </div>
                    <div className="rounded-lg border border-default bg-surface-raised p-3">
                        <div className="text-xs text-muted mb-1">Tokens / processed outcome</div>
                        <div className="text-2xl font-bold text-default">
                            {cost.tokens_per_processed_outcome
                                ? Math.round(cost.tokens_per_processed_outcome).toLocaleString()
                                : '—'}
                        </div>
                        <div className="text-xs text-secondary mt-0.5">the task-level unit cost</div>
                    </div>
                </div>
            ) : (
                <div className="rounded-lg border border-default bg-surface-raised p-3">
                    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 mb-2 text-sm">
                        <span>
                            <span className="text-muted text-xs">Success</span>{' '}
                            <span className="font-semibold text-default">
                                {fmtPct(stats.terminal_states.success_rate)}
                            </span>
                        </span>
                        <span title={RUN_CLASS_HELP.no_work}>
                            <span className="text-muted text-xs">No work</span>{' '}
                            <span className="font-semibold text-slate-600 dark:text-slate-300">
                                {fmtInt(cost.no_work_runs)}
                            </span>
                        </span>
                        <span>
                            <span className="text-muted text-xs">Cost / processed outcome</span>{' '}
                            <span className="font-semibold text-default">
                                {fmtRunUsd(cost.cost_per_processed_outcome)}
                            </span>
                        </span>
                        {stats.observed_pct !== null && stats.observed_pct !== undefined && (
                            <span
                                className="text-xs text-muted"
                                title="Share of runs observed completely. The remainder are partial or broken records — excluded from baselines."
                            >
                                coverage {fmtPct(stats.observed_pct)}
                            </span>
                        )}
                    </div>
                    <OutcomeBar stats={stats} />
                </div>
            )}
        </div>
    );
}

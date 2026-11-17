/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Mission Control card dashboard — the Admin Console landing page.
 *
 * Everything you do goes through a Resource Group, so the RG is the
 * unit on the board: org → team → AI System, drilled by clicking group
 * cards. The component renders what the cards API asserts and adds no
 * judgment of its own:
 *
 *  - the window pills scale MEASUREMENTS (spend, runs, cost/task,
 *    trend); health, verdict and Big-T class are pinned judgments from
 *    the materialized health row and deliberately do not move;
 *  - group cards show worst-of judgments over their subtree
 *    ("contains T(n·k)"), with sums for the additive metrics;
 *  - the one pseudo-card (unattributed runs) is rendered distinctly —
 *    an honesty device, not a group. Imported spend is NOT a card of its
 *    own: it belongs to the Resource Group it was attributed to, and any
 *    card carrying it says how much of its spend was merely observed.
 *
 * Every tokenomics concept has an info dot backed by the server-side
 * explainers endpoint — one definition, every surface.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle,
    ArrowRight,
    Bell,
    BookOpen,
    ChevronRight,
    FolderTree,
    Gauge,
    Home,
    Info,
    Loader2,
    Search,
    TrendingUp,
    X,
    SlidersHorizontal,
    GraduationCap,
} from 'lucide-react';
import { useGateway } from '../../hooks/useGateway';
import {
    HEALTH_STYLE,
    VERDICT_STYLE,
    assuranceTone,
    toneBorder,
    type AssuranceTone,
    type DriftEvent,
} from '../../lib/aiSystemTypes';
import { isMirroredInInbox } from '../../lib/systemAlerts';
import type {
    CardSort,
    CardWindow,
    MissionControlCard,
    MissionControlCardsResponse,
    CardExplainer,
    CardExplainerSection,
    WorkloadGuide,
    UnattributedGroup,
} from '../../lib/missionControlCardTypes';
import type { SystemAlert, UsageAlertInfo } from '../../lib/groupTypes';
import MissionControlAlertsDrawer from './MissionControlAlertsDrawer';
import WorkloadGuidePage from './WorkloadGuidePage';
import HowBrutorLearnsPage from './HowBrutorLearnsPage';
import { useTimespanPreference } from '../../hooks/useTimespanPreference';
import { asyncHandler } from '../../lib/asyncSafe';
import { errorKind, failureSentence } from '../../lib/narrow';
import toast from 'react-hot-toast';
import { useReadFailures } from '../../hooks/useRead';
import type { ReadFailure } from '../../hooks/useRead';
import { ALERT_FEEDS, ALERT_FEED_FAILED, type AlertFeed } from '../../lib/alertFeeds';

interface MissionControlDashboardProps {
    setActiveTab?: (tab: string) => void;
}

const WINDOW_OPTIONS: { value: CardWindow; label: string; title?: string }[] = [
    { value: '24h', label: '24h' },
    { value: '7d', label: '7d' },
    { value: '30d', label: '30d' },
    { value: '90d', label: '90d' },
    { value: 'all', label: 'All time', title: 'Blended across eras — judgments stay pinned to the 30-day standard' },
];

/** Windows the shared timespan preference may take on this surface.
 *  Derived from the options above so the two can't drift apart. */
const CARD_WINDOW_VALUES = WINDOW_OPTIONS.map((o) => o.value);

const SORT_OPTIONS: { value: CardSort; label: string }[] = [
    { value: 'attention', label: 'Needs attention' },
    { value: 'spend', label: 'Spend' },
    { value: 'cpt', label: 'Cost per task' },
    { value: 'name', label: 'Name' },
];

const fmtCost = (n: number | null | undefined): string => {
    if (n == null || n === 0) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(6)}`;
    if (n < 1) return `$${n.toFixed(4)}`;
    if (n >= 1000) return `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
    return `$${n.toFixed(2)}`;
};


/** Card outline severity: the worst of health + verdict, drawn all the
 *  way round the card so the grid reads at a glance without decoding
 *  chips. One ladder shared with the fleet board (`assuranceTone`) — two
 *  surfaces colour-coding the same judgments must never disagree. */
const cardTone = (card: MissionControlCard): AssuranceTone =>
    card.pseudo ? 'none' : assuranceTone(card.health_state, card.verdict_status);

/** Overlaid trend lines. Each series is normalized to ITS OWN max —
 *  dollars, tokens and bytes share no unit, so the overlay shows shape
 *  divergence (the interesting signal: tokens climbing while cost is
 *  flat = model mix moved cheaper), never magnitude comparison. */
const Sparkline: React.FC<{
    series: Array<{ points: number[]; className: string; testId?: string }>;
}> = ({ series }) => {
    const drawn = series.filter((s) => s.points.length > 0);
    if (!drawn.length) return <div className="h-8" />;
    const w = 120;
    const h = 28;
    return (
        <svg width={w} height={h} aria-hidden="true">
            {drawn.map((s, idx) => {
                const max = Math.max(...s.points, 1e-9);
                const step = s.points.length > 1 ? w / (s.points.length - 1) : w;
                const coords = s.points
                    .map((v, i) => `${(i * step).toFixed(1)},${(h - (v / max) * (h - 2) - 1).toFixed(1)}`)
                    .join(' ');
                return (
                    <polyline
                        key={idx}
                        data-testid={s.testId}
                        points={coords}
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        className={s.className}
                    />
                );
            })}
        </svg>
    );
};

/** Which optional metrics the operator shows on cards. Persisted —
 *  a dashboard you tuned once should stay tuned. */
interface CardMetricPrefs {
    tokens: boolean;
    data: boolean;
}

const CARD_METRICS_KEY = 'mc-card-metrics';

const loadCardMetricPrefs = (): CardMetricPrefs => {
    try {
        const raw = localStorage.getItem(CARD_METRICS_KEY);
        if (raw) return { tokens: false, data: false, ...JSON.parse(raw) };
    } catch { /* storage unavailable */ }
    return { tokens: false, data: false };
};

const fmtTokens = (n: number): string => {
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B tok`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M tok`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k tok`;
    return `${n} tok`;
};

const fmtBytes = (n: number): string => {
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(1)} kB`;
    return `${n} B`;
};

/** One explainer section: optional heading, paragraph, bullets. */
const SectionBlock: React.FC<{ sec: CardExplainerSection; divider?: boolean }> = ({ sec, divider = true }) => (
    <div className={divider ? 'mt-2.5 pt-2 border-t border-default' : 'mt-2.5'}>
        {sec.title && <p className="text-xs font-semibold text-default">{sec.title}</p>}
        {sec.body && <p className="text-xs text-muted mt-1 leading-relaxed">{sec.body}</p>}
        {sec.points && (
            <ul className="text-xs text-muted mt-1.5 leading-relaxed list-disc pl-4 space-y-1">
                {sec.points.map((pt) => (
                    <li key={pt.slice(0, 40)}>{pt}</li>
                ))}
            </ul>
        )}
    </div>
);

/**
 * The tokenomics info dot. Every concept an operator might not know
 * (Big-T class, cost per task, worst-of rollups…) carries one; the text
 * comes from the server-side explainers so a dashboard tooltip and an
 * Omni chat answer can never disagree.
 */
const InfoDot: React.FC<{
    k: string;
    /** EXPLAINERS lookup key when it differs from k — the T-class chip
     *  explains the card's OWN class (`t_class::T(n·k)`), while k stays
     *  the stable concept id used in testids. */
    entryKey?: string;
    /** Disambiguates dots for the same concept across cards — only the
     *  clicked dot opens, not every card's copy of it. */
    scope?: string;
    explainers: Record<string, CardExplainer> | null;
    openKey: string | null;
    onToggle: (key: string | null) => void;
}> = ({ k, entryKey, scope, explainers, openKey, onToggle }) => {
    const entry = explainers?.[entryKey ?? k] ?? explainers?.[k];
    const selfKey = scope ? `${scope}:${k}` : k;
    const open = openKey === selfKey;
    const btnRef = useRef<HTMLButtonElement | null>(null);
    const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

    // Fixed positioning, clamped to the viewport: the dot can sit at a
    // card's right edge (the T-class chip does), where an absolutely
    // positioned popover would spill off-card or off-screen.
    const POP_WIDTH = 360;
    const toggle = (e: React.MouseEvent) => {
        e.stopPropagation();
        if (!open && btnRef.current) {
            const r = btnRef.current.getBoundingClientRect();
            setPos({
                top: r.bottom + 6,
                left: Math.max(
                    8,
                    Math.min(
                        r.left + r.width / 2 - POP_WIDTH / 2,
                        globalThis.innerWidth - POP_WIDTH - 8,
                    ),
                ),
            });
        }
        onToggle(open ? null : selfKey);
    };

    return (
        <span className="relative inline-flex">
            <button
                ref={btnRef}
                type="button"
                aria-label={`Explain ${entry?.title ?? k}`}
                data-testid={`info-dot-${k}`}
                className="text-muted hover:text-default"
                onClick={toggle}
            >
                <Info className="w-3.5 h-3.5" />
            </button>
            {open && entry && (
                <div
                    role="tooltip"
                    data-testid={`info-pop-${k}`}
                    style={{ position: 'fixed', top: pos?.top ?? 0, left: pos?.left ?? 0, width: POP_WIDTH, maxHeight: '70vh' }}
                    className="z-50 p-3 rounded-lg border border-default bg-surface shadow-lg text-left cursor-default font-sans font-normal normal-case overflow-y-auto"
                    onClick={(e) => e.stopPropagation()}
                >
                    <div className="flex items-start justify-between gap-2">
                        <span className="text-xs font-semibold text-default">{entry.title}</span>
                        <button
                            type="button"
                            aria-label="Close explainer"
                            className="text-muted hover:text-default"
                            onClick={(e) => {
                                e.stopPropagation();
                                onToggle(null);
                            }}
                        >
                            <X className="w-3 h-3" />
                        </button>
                    </div>
                    <p className="text-xs text-muted mt-1 leading-relaxed">{entry.body}</p>
                    {entry.points && (
                        <ul className="text-xs text-muted mt-1.5 leading-relaxed list-disc pl-4 space-y-1">
                            {entry.points.map((pt) => (
                                <li key={pt.slice(0, 40)}>{pt}</li>
                            ))}
                        </ul>
                    )}
                    {entry.sections?.map((sec, i) => (
                        <SectionBlock key={sec.title ?? i} sec={sec} />
                    ))}
                </div>
            )}
        </span>
    );
};

/** How many groups the Unattributed card names before "+N more". */
const UNATTRIBUTED_TOP = 3;

/**
 * The Unattributed card's to-do list: where its runs started, top groups
 * by runs, each linking to that group so "attribute it" has somewhere to go.
 */
const UnattributedBreakdown: React.FC<{
    rows: UnattributedGroup[];
    onOpenGroup: (groupId: string) => void;
}> = ({ rows, onOpenGroup }) => {
    const top = rows.slice(0, UNATTRIBUTED_TOP);
    const more = rows.length - top.length;
    return (
        <ul className="space-y-0.5 text-[11px]" data-testid="mc-unattributed-breakdown">
            {top.map((r) => (
                <li key={r.group_id ?? '__unknown'} className="flex items-center gap-2">
                    {r.group_id ? (
                        <button
                            type="button"
                            className="truncate text-primary-600 dark:text-primary-400 hover:underline"
                            title={`Open ${r.group_name ?? r.group_id}`}
                            data-testid={`mc-unattributed-group-${r.group_id}`}
                            onClick={(e) => {
                                e.stopPropagation();
                                onOpenGroup(r.group_id as string);
                            }}
                        >
                            {r.group_name ?? r.group_id}
                        </button>
                    ) : (
                        <span className="truncate text-muted italic">unknown group</span>
                    )}
                    <span className="ml-auto tabular-nums text-muted shrink-0">
                        {r.runs} run{r.runs === 1 ? '' : 's'} · {fmtCost(r.spend)}
                    </span>
                </li>
            ))}
            {more > 0 && <li className="text-muted" data-testid="mc-unattributed-more">+{more} more</li>}
        </ul>
    );
};

const CardTile: React.FC<{
    card: MissionControlCard;
    metricPrefs: CardMetricPrefs;
    explainers: Record<string, CardExplainer> | null;
    infoKey: string | null;
    setInfoKey: (k: string | null) => void;
    onOpen: (card: MissionControlCard) => void;
    onMetrics: (card: MissionControlCard) => void;
    onFindings: (card: MissionControlCard) => void;
    onUsage: (card: MissionControlCard) => void;
    onOpenGroup: (groupId: string) => void;
}> = ({ card, metricPrefs, explainers, infoKey, setInfoKey, onOpen, onMetrics, onFindings, onUsage, onOpenGroup }) => {
    const health = card.health_state ? HEALTH_STYLE[card.health_state] : null;
    const verdict = card.verdict_status ? VERDICT_STYLE[card.verdict_status] : null;
    const isPseudo = Boolean(card.pseudo);
    // A Resource Group that is not a system kind, with no AI System beneath
    // it, never had a run — imported usage is spend without one. Printing
    // "0 runs" there states a measurement that was never taken.
    const hasRuns =
        card.runs > 0 || card.group_type === 'ai_system' || (card.systems ?? 0) > 0;
    // How much of this card's spend was merely observed: imported
    // usage is costed in the same ledger, and enforced by nothing.
    const observed = card.observed_spend ?? 0;
    const allObserved = observed > 0 && observed >= card.spend - 1e-9;

    return (
        <div
            role="button"
            tabIndex={0}
            data-testid={`mc-card-${card.id}`}
            onClick={() => onOpen(card)}
            onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onOpen(card);
                }
            }}
            data-tone={cardTone(card)}
            className={`relative text-left rounded-lg border-2 bg-surface transition-shadow hover:shadow-md focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 cursor-pointer ${
                toneBorder(cardTone(card))
            }${isPseudo ? ' border-dashed' : ''}`}
        >
            <div className="p-4 space-y-3">
                {/* Identity row */}
                <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                        <div className="flex items-center gap-2">
                            <span className="font-medium text-default truncate" title={card.name}>
                                {card.name}
                            </span>
                            {card.live && (
                                <span
                                    className="w-2 h-2 rounded-full bg-green-500 animate-pulse shrink-0"
                                    title="Traffic in the last 24h"
                                    data-testid={`mc-live-${card.id}`}
                                />
                            )}
                        </div>
                        <div className="text-xs text-muted mt-0.5">
                            {isPseudo
                                ? 'Governed traffic, no AI System'
                                : card.is_group
                                    ? `${card.systems} system${card.systems === 1 ? '' : 's'}`
                                    : card.system_kind
                                        ? `AI System · ${card.system_kind}`
                                        : card.is_system
                                            ? 'system'
                                            : card.group_type}
                        </div>
                        {!isPseudo && card.purpose && (
                            <div className="text-[11px] text-muted mt-0.5 truncate" title={card.purpose} data-testid={`mc-purpose-${card.id}`}>
                                {card.purpose}
                            </div>
                        )}
                    </div>
                    {card.is_group && <FolderTree className="w-4 h-4 text-muted shrink-0 mt-1" />}
                </div>

                {/* Judgments row — pinned, never moves with the window */}
                {!isPseudo && (
                    <div className="flex flex-wrap items-center gap-1.5">
                        {health && (
                            <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${health.chip}`}>
                                {health.label}
                            </span>
                        )}
                        {verdict && (
                            <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${verdict.chip}`}>
                                {verdict.label}
                            </span>
                        )}
                        {card.t_class && (
                            <span
                                className="px-1.5 py-0.5 rounded text-[11px] font-mono bg-surface-subtle border border-default text-default inline-flex items-center gap-1"
                                data-testid={`mc-tclass-${card.id}`}
                            >
                                {card.t_class_contains ? `contains ${card.t_class}` : card.t_class}
                                <InfoDot k="t_class" entryKey={`t_class::${card.t_class}`} scope={card.id} explainers={explainers} openKey={infoKey} onToggle={setInfoKey} />
                            </span>
                        )}
                        {observed > 0 && (
                            <span
                                className="px-1.5 py-0.5 rounded text-[11px] font-medium bg-surface-subtle border border-dashed border-strong text-muted inline-flex items-center gap-1"
                                data-testid={`mc-observed-${card.id}`}
                                title={allObserved
                                    ? 'Every dollar here was imported, not routed: nothing was enforced on it'
                                    : `${fmtCost(observed)} of this spend was imported, not routed`}
                            >
                                {allObserved ? 'observed only' : `${fmtCost(observed)} observed`}
                                <InfoDot k="observed_plane" scope={card.id} explainers={explainers} openKey={infoKey} onToggle={setInfoKey} />
                            </span>
                        )}
                    </div>
                )}

                {/* Measurements row — scaled by the window */}
                <div className="flex items-end justify-between gap-2">
                    <div>
                        <div className="text-lg font-semibold text-default tabular-nums">{fmtCost(card.spend)}</div>
                        <div className="text-[11px] text-muted">
                            {hasRuns && (
                                <>
                                    {card.runs.toLocaleString()} run{card.runs === 1 ? '' : 's'}
                                </>
                            )}
                            {metricPrefs.tokens && (
                                <span className="text-violet-600 dark:text-violet-400" data-testid={`card-tokens-${card.id}`}>
                                    {hasRuns ? ' · ' : ''}{fmtTokens(card.tokens ?? 0)}
                                </span>
                            )}
                            {metricPrefs.data && (
                                <span className="text-teal-600 dark:text-teal-400" data-testid={`card-data-${card.id}`}>
                                    {hasRuns || metricPrefs.tokens ? ' · ' : ''}{fmtBytes(card.data_bytes ?? 0)}
                                </span>
                            )}
                        </div>
                    </div>
                    <Sparkline
                        series={[
                            { points: card.trend, className: 'text-primary-500 opacity-80' },
                            ...(metricPrefs.tokens
                                ? [{ points: card.trend_tokens ?? [], className: 'text-violet-500 opacity-70', testId: 'spark-tokens' }]
                                : []),
                            ...(metricPrefs.data
                                ? [{ points: card.trend_bytes ?? [], className: 'text-teal-500 opacity-70', testId: 'spark-bytes' }]
                                : []),
                        ]}
                    />
                </div>

                {/* Cost per task is a MEAN over completed runs. An AI System
                    with none in the window has no mean — say so rather than
                    dropping the row, so every system card has the same shape
                    and a missing figure never reads as a rendering bug. */}
                {!isPseudo && (card.cost_per_task != null || card.system_kind) && (
                    <div className="text-xs flex items-center gap-1" data-testid={`mc-cpt-${card.id}`}>
                        <span className={card.cpt_breached ? 'text-red-600 dark:text-red-400 font-medium' : 'text-muted'}>
                            {card.cost_per_task != null ? (
                                <>
                                    {fmtCost(card.cost_per_task)}/task
                                    {card.cost_per_task_blended ? ' (blended)' : ''}
                                </>
                            ) : (
                                '—/task · no completed runs in window'
                            )}
                            {card.cpt_bound != null && ` · bound ${fmtCost(card.cpt_bound)}`}
                        </span>
                        <InfoDot k="cost_per_task" scope={card.id} explainers={explainers} openKey={infoKey} onToggle={setInfoKey} />
                    </div>
                )}

                {/* Exclusions are shown, never hidden: an excused verdict
                    is a fact about the estate, and the count on a group card is
                    how a reader knows the rollup is smaller than the estate. */}
                {!isPseudo && card.excluded && (
                    <div
                        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300"
                        data-testid={`mc-excluded-${card.id}`}
                        title={card.exclusion_expires_at
                            ? `Excluded from the rollup until ${new Date(card.exclusion_expires_at).toLocaleDateString()}`
                            : 'Excluded from the rollup'}
                    >
                        Excluded from rollup · {String(card.exclusion_reason ?? '').replace(/_/g, ' ')}
                    </div>
                )}
                {!isPseudo && card.is_group && (card.excluded_systems ?? 0) > 0 && (
                    <div className="text-[11px] text-muted" data-testid={`mc-excluded-count-${card.id}`}>
                        {card.excluded_systems} system{card.excluded_systems === 1 ? '' : 's'} excluded from this rollup
                    </div>
                )}
                {/* Findings + gaps */}
                {!isPseudo && (card.findings > 0 || card.governance_gaps > 0) && (
                    <div className="flex items-center gap-2">
                        {card.findings > 0 && (
                            <span className="inline-flex items-center gap-1">
                                {/* The InfoDot is itself a button, so it lives
                                    beside the badge, not inside it \u2014 nested
                                    buttons are invalid and a dot click would
                                    also trigger the navigation. */}
                                <button
                                    type="button"
                                    className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-900/60 transition-colors cursor-pointer"
                                    data-testid={`mc-findings-${card.id}`}
                                    title={card.is_group
                                        ? 'Open findings across this subtree'
                                        : 'Triage this system\u2019s findings on its Drift tab'}
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        onFindings(card);
                                    }}
                                >
                                    <AlertTriangle className="w-3 h-3" />
                                    {card.findings} open finding{card.findings === 1 ? '' : 's'}
                                </button>
                                <InfoDot k="findings" scope={card.id} explainers={explainers} openKey={infoKey} onToggle={setInfoKey} />
                            </span>
                        )}
                        {card.governance_gaps > 0 && (
                            <span className="text-[11px] text-muted">
                                {card.governance_gaps} governance gap{card.governance_gaps === 1 ? '' : 's'}
                            </span>
                        )}
                    </div>
                )}

                {isPseudo && card.note && <p className="text-[11px] text-muted leading-relaxed">{card.note}</p>}

                {isPseudo && (card.unattributed_by_group?.length ?? 0) > 0 && (
                    <UnattributedBreakdown rows={card.unattributed_by_group ?? []} onOpenGroup={onOpenGroup} />
                )}

                {/* Actions */}
                {!isPseudo && (
                    <div className="flex items-center gap-3 pt-1 border-t border-default">
                        <span className="text-xs text-primary-600 dark:text-primary-400 inline-flex items-center gap-1">
                            {card.is_group
                                ? 'Drill down'
                                : card.group_type === 'ai_system'
                                    ? 'Open system'
                                    // Custom leaf groups have no AI System
                                    // drilldown — the card opens their
                                    // Usage tab (see openCard).
                                    : 'Open group'}
                            <ArrowRight className="w-3 h-3" />
                        </span>
                        <button
                            type="button"
                            data-testid={`mc-usage-${card.id}`}
                            title={card.is_group
                                ? 'Resource Group usage, descendants included'
                                : 'This system’s Resource Group usage'}
                            className="text-xs text-muted hover:text-default inline-flex items-center gap-1 ml-auto"
                            onClick={(e) => {
                                e.stopPropagation();
                                onUsage(card);
                            }}
                        >
                            <TrendingUp className="w-3 h-3" />
                            Usage details
                        </button>
                        <button
                            type="button"
                            data-testid={`mc-metrics-${card.id}`}
                            className="text-xs text-muted hover:text-default inline-flex items-center gap-1"
                            onClick={(e) => {
                                e.stopPropagation();
                                onMetrics(card);
                            }}
                        >
                            <Gauge className="w-3 h-3" />
                            Metrics
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
};

export const MissionControlDashboard: React.FC<MissionControlDashboardProps> = ({ setActiveTab }) => {
    const {
        fetchMissionControlCards,
        fetchMissionControlExplainers,
        fetchSystemAlerts,
        fetchTenantDriftFeed,
        fetchAllUsageAlerts,
        acknowledgeUsageAlert,
        acknowledgeSystemAlert,
        resolveSystemAlert,
        fetchAssuranceInboxCounts,
    } = useGateway();

    const [parentId, setParentId] = useState<string | null>(null);
    // Shared with Mission Control / AI Systems / Resource Groups. '30d'
    // is the fallback on a first visit;
    // Mission Control's MTD / last-month options aren't offered here, so
    // they render as 30d without overwriting the stored choice.
    const [cardWindow, setCardWindow] = useTimespanPreference(CARD_WINDOW_VALUES, '30d');
    const [searchInput, setSearchInput] = useState('');
    const [query, setQuery] = useState('');
    const [sort, setSort] = useState<CardSort>('attention');
    const [typeFilter, setTypeFilter] = useState('');
    const [data, setData] = useState<MissionControlCardsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [explainers, setExplainers] = useState<Record<string, CardExplainer> | null>(null);
    const [guide, setGuide] = useState<WorkloadGuide | null>(null);
    const [guideOpen, setGuideOpen] = useState(false);
    const [learnOpen, setLearnOpen] = useState(false);
    const [metricPrefs, setMetricPrefs] = useState<CardMetricPrefs>(loadCardMetricPrefs);
    const [metricsMenuOpen, setMetricsMenuOpen] = useState(false);

    const updateMetricPrefs = (patch: Partial<CardMetricPrefs>) => {
        setMetricPrefs((prev) => {
            const next = { ...prev, ...patch };
            try { localStorage.setItem(CARD_METRICS_KEY, JSON.stringify(next)); }
            catch { /* storage unavailable */ }
            return next;
        });
    };
    const [infoKey, setInfoKey] = useState<string | null>(null);
    const [alertsOpen, setAlertsOpen] = useState(false);
    const [systemAlerts, setSystemAlerts] = useState<SystemAlert[]>([]);
    // Unresolved assurance findings — the drawer points at the Inbox for
    // these instead of listing the system alerts that mirror them.
    const [inboxOpen, setInboxOpen] = useState<number | null>(null);
    const [usageAlerts, setUsageAlerts] = useState<UsageAlertInfo[]>([]);
    const [driftFeed, setDriftFeed] = useState<DriftEvent[]>([]);
    const [busyAlertActions, setBusyAlertActions] = useState<ReadonlySet<string>>(() => new Set());
    const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Debounced search — searching flattens the hierarchy server-side.
    useEffect(() => {
        if (searchTimer.current) clearTimeout(searchTimer.current);
        searchTimer.current = setTimeout(() => setQuery(searchInput.trim()), 300);
        return () => {
            if (searchTimer.current) clearTimeout(searchTimer.current);
        };
    }, [searchInput]);

    const load = useCallback(async () => {
        try {
            setLoading(true);
            setError('');
            const response = await fetchMissionControlCards({
                parent_id: query ? undefined : parentId ?? undefined,
                window: cardWindow,
                q: query || undefined,
                group_type: typeFilter || undefined,
                sort,
            });
            setData(response);
        } catch (err: unknown) {
            console.error('Failed to load Mission Control cards:', errorKind(err));
            setError(err instanceof Error ? err.message : 'Failed to load dashboard');
            setData(null);
        } finally {
            setLoading(false);
        }
    }, [fetchMissionControlCards, parentId, cardWindow, query, typeFilter, sort]);

    useEffect(() => {
        void load();
    }, [load]);

    // Explainers load once, best-effort: an operator who cannot reach the
    // endpoint still gets a working dashboard, just without the info dots'
    // content.
    useEffect(() => {
        let cancelled = false;
        void (async () => {
            try {
                const r = await fetchMissionControlExplainers();
                if (!cancelled) {
                    setExplainers(r.explainers);
                    setGuide(r.guide ?? null);
                }
            } catch {
                // The info dots have no text; the dashboard is whole without it.
                if (!cancelled) setExplainers(null);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [fetchMissionControlExplainers]);

    // The alert feeds: independent of any card, so they live in the header
    // bell. This is THE alerts surface, so it carries all three feeds:
    // system alerts, usage (quota) alerts and open drift findings.
    //
    // Each feed fails on its own. A feed that could not be read keeps what
    // it held: an emptied list would say there are no alerts. The bell and
    // the drawer say which feed is missing or stale.
    const [feedFailure, feeds] = useReadFailures<AlertFeed>();
    const loadAlerts = useCallback(async () => {
        const [sys, usage, drift, inbox] = await Promise.allSettled([
            // Active AND acknowledged — acknowledged rows show ownership.
            fetchSystemAlerts({ limit: 100 }),
            fetchAllUsageAlerts({ status: 'active' }),
            fetchTenantDriftFeed({ status: 'active', limit: 50 }),
            fetchAssuranceInboxCounts(),
        ]);
        if (sys.status === 'fulfilled') {
            // Ops-only: a liveness miss is raised here for paging AND as an
            // inbox finding for disposition. The person reading the drawer
            // gets it once — from the Inbox — not twice.
            setSystemAlerts(
                (sys.value?.alerts ?? []).filter(
                    (a: SystemAlert) => a.status !== 'resolved' && !isMirroredInInbox(a),
                ),
            );
            feeds.loaded('system');
        } else feeds.failed('system', ALERT_FEED_FAILED.system, sys.reason);
        if (usage.status === 'fulfilled') {
            setUsageAlerts(usage.value?.alerts ?? []);
            feeds.loaded('usage');
        } else feeds.failed('usage', ALERT_FEED_FAILED.usage, usage.reason);
        if (drift.status === 'fulfilled') {
            setDriftFeed(drift.value?.events ?? []);
            feeds.loaded('drift');
        } else feeds.failed('drift', ALERT_FEED_FAILED.drift, drift.reason);
        if (inbox.status === 'fulfilled') {
            setInboxOpen(inbox.value ? (inbox.value.open ?? 0) + (inbox.value.acknowledged ?? 0) : null);
            feeds.loaded('inbox');
        } else feeds.failed('inbox', ALERT_FEED_FAILED.inbox, inbox.reason);
    }, [fetchSystemAlerts, fetchAllUsageAlerts, fetchTenantDriftFeed, fetchAssuranceInboxCounts, feeds]);
    const feedFailures: Partial<Record<AlertFeed, ReadFailure>> = {};
    for (const feed of ALERT_FEEDS) {
        const failure = feedFailure(feed);
        if (failure) feedFailures[feed] = failure;
    }
    const feedsFailed = Object.keys(feedFailures).length > 0;

    useEffect(() => {
        void loadAlerts();
    }, [loadAlerts]);

    // The bell: ops items plus the findings waiting in the Inbox. Drift
    // findings are inbox findings, so they are counted once, there.
    const alertCount =
        usageAlerts.length + systemAlerts.length + (inboxOpen ?? driftFeed.length);

    // ── Alert actions ──
    const handleAcknowledgeUsage = useCallback(async (alertId: string) => {
        try {
            await acknowledgeUsageAlert(alertId);
            setUsageAlerts((prev) => prev.filter((a) => a.id !== alertId));
        } catch (err) {
            console.error('Failed to acknowledge usage alert:', errorKind(err));
            toast.error(failureSentence('The alert was not acknowledged', err));
        }
    }, [acknowledgeUsageAlert]);

    const handleAcknowledgeSystem = useCallback(async (alertId: string) => {
        try {
            const res = await acknowledgeSystemAlert(alertId);
            setSystemAlerts((prev) => prev.map((a) =>
                a.id === alertId
                    ? { ...a, status: res.status, acknowledged_by: res.acknowledged_by, acknowledged_at: res.acknowledged_at }
                    : a,
            ));
        } catch (err) {
            console.error('Failed to acknowledge system alert:', errorKind(err));
        }
    }, [acknowledgeSystemAlert]);

    const handleResolveSystem = useCallback(async (alertId: string) => {
        try {
            await resolveSystemAlert(alertId);
            setSystemAlerts((prev) => prev.filter((a) => a.id !== alertId));
        } catch (err) {
            console.error('Failed to resolve system alert:', errorKind(err));
        }
    }, [resolveSystemAlert]);

    // Alert actions contributed to the drawer's slot report through these
    // two: which of them are in flight (kept here so it outlives the drawer
    // being closed and reopened), and that one of them resolved an alert.
    const setAlertActionBusy = useCallback((key: string, busy: boolean) => {
        setBusyAlertActions((prev) => {
            const next = new Set(prev);
            if (busy) next.add(key);
            else next.delete(key);
            return next;
        });
    }, []);
    const dropSystemAlert = useCallback((alertId: string) => {
        setSystemAlerts((prev) => prev.filter((a) => a.id !== alertId));
    }, []);

    const openCard = useCallback(
        (card: MissionControlCard) => {
            if (card.pseudo === 'unattributed') {
                setActiveTab?.('ai-systems');
                return;
            }
            if (card.is_group) {
                setSearchInput('');
                setParentId(card.id);
                return;
            }
            // A leaf card that is NOT an AI System (group_type='custom' —
            // e.g. the trial's Default group) has no Signals/Runs/Contract
            // to show; the AI Systems drilldown would just render its
            // "not an AI System" error. Open the group's Usage tab instead.
            if (card.group_type !== 'ai_system') {
                setActiveTab?.(`groups:${card.id}:usage`);
                return;
            }
            // "Open system" means the AI Systems drilldown (Signals first),
            // not the resource-group form — the RG detail stays reachable
            // via "Usage details" and the drilldown's own header button.
            setActiveTab?.(`ai-systems:${card.id}:signals`);
        },
        [setActiveTab],
    );

    const openUsage = useCallback(
        (card: MissionControlCard) => {
            // Both card kinds land on Resource Groups → {group} → Usage
            // (an AI System is a resource group). Organizational
            // groups open with descendants included, so the page shows the
            // aggregated spend the card was summarizing.
            setActiveTab?.(`groups:${card.id}:usage`);
        },
        [setActiveTab],
    );

    const openMetrics = useCallback(
        (card: MissionControlCard) => {
            setActiveTab?.(`mission-control-metrics:${card.id}`);
        },
        [setActiveTab],
    );

    const cards = useMemo(() => data?.cards ?? [], [data]);
    const path = data?.path ?? [];
    const searching = Boolean(query);

    const groupTypes = useMemo(() => {
        const set = new Set(cards.filter((c) => !c.pseudo).map((c) => c.group_type));
        return Array.from(set).sort();
    }, [cards]);


    // The guide is a sub-page, not a modal: full width for the growth
    // chart, browser scroll, Escape or the back link returns.
    if (guideOpen && guide) {
        return (
            <WorkloadGuidePage guide={guide} onBack={() => setGuideOpen(false)} />
        );
    }

    if (learnOpen) {
        return <HowBrutorLearnsPage onBack={() => setLearnOpen(false)} />;
    }

    return (
        <div className="p-6 space-y-4" data-testid="mission-control-dashboard" onClick={() => setInfoKey(null)}>
            {/* Header */}
            <div className="flex flex-wrap items-center gap-3">
                <div className="mr-auto">
                    <h1 className="text-xl font-semibold text-default">Overview</h1>
                    <p className="text-sm text-muted mt-0.5">
                        Your AI estate by Resource Group — spend and runs follow the window; health, verdict and
                        workload class are pinned judgments.
                        <InfoDot k="worst_of" explainers={explainers} openKey={infoKey} onToggle={setInfoKey} />
                    </p>
                </div>
                <button
                    type="button"
                    data-testid="mc-alerts-bell"
                    className="relative p-2 rounded-lg border border-default hover:bg-surface-hover"
                    aria-label={feedsFailed ? 'Alerts (some could not be loaded)' : 'Alerts'}
                    title={feedsFailed ? 'Some alerts could not be loaded' : undefined}
                    onClick={(e) => {
                        e.stopPropagation();
                        setAlertsOpen(true);
                        void loadAlerts();
                    }}
                >
                    <Bell className="w-4 h-4 text-default" />
                    {alertCount > 0 && (
                        <span
                            className="absolute -top-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-red-600 text-white text-[10px] font-semibold flex items-center justify-center"
                            data-testid="mc-alerts-count"
                        >
                            {alertCount > 99 ? '99+' : alertCount}
                        </span>
                    )}
                    {feedsFailed && (
                        <span
                            className="absolute -bottom-1 -right-1 min-w-4 h-4 px-1 rounded-full bg-amber-500 text-white text-[10px] font-bold flex items-center justify-center"
                            data-testid="mc-alerts-unknown"
                            aria-hidden="true"
                        >
                            !
                        </span>
                    )}
                </button>
                <button
                    type="button"
                    data-testid="mc-learn-button"
                    className="flex items-center gap-2 px-3 py-2 text-sm border border-default rounded-lg hover:bg-surface-hover text-default"
                    onClick={(e) => {
                        e.stopPropagation();
                        setLearnOpen(true);
                    }}
                >
                    <GraduationCap className="w-4 h-4" />
                    How Brutor learns
                </button>
                <div className="relative">
                    <button
                        type="button"
                        data-testid="mc-display-button"
                        aria-expanded={metricsMenuOpen}
                        className="flex items-center gap-2 px-3 py-2 text-sm border border-default rounded-lg hover:bg-surface-hover text-default"
                        onClick={(e) => {
                            e.stopPropagation();
                            setMetricsMenuOpen((v) => !v);
                        }}
                    >
                        <SlidersHorizontal className="w-4 h-4" />
                        Card display
                    </button>
                    {metricsMenuOpen && (
                        <div
                            data-testid="mc-display-menu"
                            className="absolute right-0 top-full mt-1 z-30 w-64 bg-surface border border-default rounded-lg shadow-lg p-3 space-y-2"
                        >
                            <p className="text-xs font-semibold text-default">Card metrics</p>
                            <p className="text-[11px] text-muted">
                                Cost is always shown. Extra series overlay the
                                trend, each scaled to its own range.
                            </p>
                            <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
                                <input
                                    type="checkbox"
                                    data-testid="mc-toggle-tokens"
                                    checked={metricPrefs.tokens}
                                    onChange={(e) => updateMetricPrefs({ tokens: e.target.checked })}
                                />
                                <span className="w-2 h-2 rounded-full bg-violet-500 inline-block" aria-hidden="true" />
                                Tokens
                            </label>
                            <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
                                <input
                                    type="checkbox"
                                    data-testid="mc-toggle-data"
                                    checked={metricPrefs.data}
                                    onChange={(e) => updateMetricPrefs({ data: e.target.checked })}
                                />
                                <span className="w-2 h-2 rounded-full bg-teal-500 inline-block" aria-hidden="true" />
                                Data transfer
                            </label>
                        </div>
                    )}
                </div>
                {guide && (
                    <button
                        type="button"
                        data-testid="mc-guide-button"
                        className="flex items-center gap-2 px-3 py-2 text-sm border border-default rounded-lg hover:bg-surface-hover text-default"
                        onClick={(e) => {
                            e.stopPropagation();
                            setGuideOpen(true);
                        }}
                    >
                        <BookOpen className="w-4 h-4" />
                        Workload classes — T(n·k·a) guide
                    </button>
                )}
            </div>

            {/* Toolbar */}
            <div className="flex flex-wrap items-center gap-2">
                <div className="relative">
                    <Search className="w-4 h-4 text-muted absolute left-2.5 top-1/2 -translate-y-1/2" />
                    <input
                        type="search"
                        value={searchInput}
                        onChange={(e) => setSearchInput(e.target.value)}
                        placeholder="Search all systems…"
                        aria-label="Search resource groups"
                        className="pl-8 pr-3 py-1.5 text-sm rounded-lg border border-default bg-surface text-default w-56"
                    />
                </div>
                <div className="flex bg-surface-raised rounded-lg p-0.5" role="group" aria-label="Window">
                    {WINDOW_OPTIONS.map((w) => (
                        <button
                            key={w.value}
                            type="button"
                            title={w.title}
                            data-testid={`mc-window-${w.value}`}
                            aria-pressed={cardWindow === w.value}
                            className={`px-2.5 py-1.5 text-xs font-medium rounded-md transition-colors ${
                                cardWindow === w.value
                                    ? 'bg-surface text-default shadow-sm'
                                    : 'text-muted hover:text-secondary'
                            }`}
                            onClick={() => setCardWindow(w.value)}
                        >
                            {w.label}
                        </button>
                    ))}
                </div>
                <select
                    value={sort}
                    aria-label="Sort cards"
                    onChange={(e) => setSort(e.target.value as CardSort)}
                    className="px-2 py-1.5 text-xs rounded-lg border border-default bg-surface text-default"
                >
                    {SORT_OPTIONS.map((s) => (
                        <option key={s.value} value={s.value}>
                            Sort: {s.label}
                        </option>
                    ))}
                </select>
                <select
                    value={typeFilter}
                    aria-label="Filter by group type"
                    onChange={(e) => setTypeFilter(e.target.value)}
                    className="px-2 py-1.5 text-xs rounded-lg border border-default bg-surface text-default"
                >
                    <option value="">All types</option>
                    {groupTypes.map((t) => (
                        <option key={t} value={t}>
                            {t}
                        </option>
                    ))}
                    {typeFilter && !groupTypes.includes(typeFilter) && (
                        <option value={typeFilter}>{typeFilter}</option>
                    )}
                </select>
            </div>

            {/* The inbox, the checks and the weekly summary live on
                Mission Control → Inbox: the landing page is the overview,
                the queue is where the work is. */}

            {/* Breadcrumb */}
            {!searching && (
                <nav className="flex items-center gap-1 text-sm" aria-label="Hierarchy" data-testid="mc-breadcrumb">
                    <button
                        type="button"
                        className={`inline-flex items-center gap-1 ${
                            parentId ? 'text-primary-600 dark:text-primary-400 hover:underline' : 'text-default font-medium'
                        }`}
                        onClick={() => setParentId(null)}
                    >
                        <Home className="w-3.5 h-3.5" />
                        Estate
                    </button>
                    {path.map((p, i) => (
                        <React.Fragment key={p.id}>
                            <ChevronRight className="w-3.5 h-3.5 text-muted" />
                            {i === path.length - 1 ? (
                                <span className="text-default font-medium">{p.name}</span>
                            ) : (
                                <button
                                    type="button"
                                    className="text-primary-600 dark:text-primary-400 hover:underline"
                                    onClick={() => setParentId(p.id)}
                                >
                                    {p.name}
                                </button>
                            )}
                        </React.Fragment>
                    ))}
                </nav>
            )}
            {searching && (
                <p className="text-sm text-muted" data-testid="mc-search-note">
                    Search results across all levels — hierarchy flattened.
                </p>
            )}

            {/* Grid */}
            {error && (
                <div className="p-4 rounded-lg border border-red-300 bg-red-50 dark:bg-red-900/20 text-sm text-red-800 dark:text-red-300">
                    {error}
                </div>
            )}
            {loading && !data ? (
                <div className="flex items-center gap-2 text-muted text-sm py-12 justify-center">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Loading estate…
                </div>
            ) : cards.length === 0 && !error ? (
                <div className="text-sm text-muted py-12 text-center" data-testid="mc-empty">
                    {searching ? 'No groups match your search.' : 'No resource groups at this level.'}
                </div>
            ) : (
                <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                    {cards.map((card) => (
                        <CardTile
                            metricPrefs={metricPrefs}
                            onFindings={(c) => {
                                if (c.is_group || !setActiveTab) {
                                    setAlertsOpen(true);
                                } else {
                                    setActiveTab(`ai-systems:${c.id}:drift`);
                                }
                            }}
                            key={card.id}
                            card={card}
                            explainers={explainers}
                            infoKey={infoKey}
                            setInfoKey={setInfoKey}
                            onOpen={openCard}
                            onMetrics={openMetrics}
                            onUsage={openUsage}
                            onOpenGroup={(gid) => setActiveTab?.(`groups:${gid}`)}
                        />
                    ))}
                </div>
            )}

            {data && data.total > cards.length && (
                <p className="text-xs text-muted">
                    Showing {cards.length} of {data.total} groups — refine with search or filters.
                </p>
            )}

            {/* Alerts slide-over — THE alerts surface; there is no Alerts tab */}
            {alertsOpen && (
                <MissionControlAlertsDrawer
                    usageAlerts={usageAlerts}
                    systemAlerts={systemAlerts}
                    driftFeed={driftFeed}
                    feedFailures={feedFailures}
                    onRetryFeeds={() => void loadAlerts()}
                    busyActions={busyAlertActions}
                    onActionBusy={setAlertActionBusy}
                    onSystemAlertResolved={dropSystemAlert}
                    inboxOpen={inboxOpen}
                    onOpenInbox={setActiveTab ? () => setActiveTab('inbox') : undefined}
                    onClose={() => setAlertsOpen(false)}
                    onAcknowledgeUsage={asyncHandler(handleAcknowledgeUsage, 'acknowledge usage')}
                    onAcknowledgeSystem={asyncHandler(handleAcknowledgeSystem, 'acknowledge system')}
                    onResolveSystem={asyncHandler(handleResolveSystem, 'resolve system')}
                    setActiveTab={setActiveTab}
                />
            )}
        </div>
    );
};

export default MissionControlDashboard;
